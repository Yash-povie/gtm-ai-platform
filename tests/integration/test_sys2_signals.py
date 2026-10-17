"""M5.1 acceptance: signed webhooks -> idempotency -> time decay -> composite score -> System 1 draft."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from gtm_core.schemas import SignalEvent
from gtm_integrations import clay
from gtm_safety import audit as audit_mod
from sys1_autonomous_sdr.graph import SdrRuntime, set_runtime
from sys2_signal_engine import normalizer, scorer
from sys2_signal_engine.router import process_event, router
from sys3_command_center_mcp.services import build_services

SECRET = "local_n8n_secret"


@pytest.fixture
async def env(redis):
    audit_mod.set_sink(audit_mod.MemoryAuditSink())
    svc = await build_services(redis=redis, seed=True)
    rt = SdrRuntime(svc.deps, svc.runner)
    set_runtime(rt)
    app = FastAPI()
    app.include_router(router)
    # pick prospects that have no seeded signals (so composites start from zero)
    seeded = {s["account_id"] for s in svc.repo.signals.values()}
    fresh = [a for a in svc.repo.accounts.values() if not a["is_customer"] and a["id"] not in seeded
             and a["industry"] == "Fintech"]
    yield svc, rt, app, fresh
    set_runtime(None)


def signed(body: dict | list, secret=SECRET) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    return raw, {normalizer.N8N_SIGNATURE_HEADER: normalizer.sign_hmac(secret, raw), "Content-Type": "application/json"}


def event(domain, stype, payload=None, days_ago=0, email=None) -> dict:
    return {"domain": domain, "signal_type": stype, "payload": payload or {}, "contact_email": email,
            "occurred_at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()}


async def post(app, path, body, headers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(path, content=body, headers=headers)


def test_decay_and_composite_math():
    now = datetime.now(UTC)
    assert scorer.decay(80, now, now) == 80
    assert scorer.decay(80, now - timedelta(days=14), now) == 40
    assert scorer.decay(80, now + timedelta(days=5), now) == 80  # no boost for future-dated events
    assert scorer.composite([]) == 0 and scorer.composite([100]) == 100
    assert scorer.composite([65, 55]) == pytest.approx(84.25)
    assert scorer.base_strength("hiring_spike", {"open_roles": 4}) == 65


async def test_signed_webhook_triggers_draft_and_dedupes(env):
    svc, rt, app, fresh = env
    dom = fresh[0]["domain"]
    raw, hdr = signed(event(dom, "champion_moved", {"new_title": "VP of Engineering"}))
    r = await post(app, "/webhooks/signals/ingest", raw, hdr)
    assert r.status_code == 202 and r.json()["events"] == 1
    drafts = list(svc.repo.drafts.values())
    assert len(drafts) == 1 and drafts[0]["trigger_signal_id"] and drafts[0]["status"] == "pending_approval"
    assert "VP of Engineering" in drafts[0]["body_text"]
    assert svc.deps.sender.sent == []  # sandbox: still needs a human

    again = await post(app, "/webhooks/signals/ingest", raw, hdr)  # identical retry -> Redis idempotency
    assert again.json()["status"] == "duplicate_webhook" and len(svc.repo.drafts) == 1

    changed, hdr2 = signed(event(dom, "champion_moved", {"new_title": "VP of Engineering", "x": 1}))
    await post(app, "/webhooks/signals/ingest", changed, hdr2)  # new body, same logical event -> DB idempotency
    assert len(svc.repo.drafts) == 1


async def test_bad_signature_and_bad_payload_rejected(env):
    _, _, app, fresh = env
    raw, hdr = signed(event(fresh[0]["domain"], "job_change"), secret="wrong")
    assert (await post(app, "/webhooks/signals/ingest", raw, hdr)).status_code == 401
    raw, hdr = signed({"domain": "", "signal_type": "job_change"})
    assert (await post(app, "/webhooks/signals/ingest", raw, hdr)).status_code == 400
    raw, hdr = signed({"domain": fresh[0]["domain"], "signal_type": "not_a_signal"})
    assert (await post(app, "/webhooks/signals/ingest", raw, hdr)).status_code == 400


async def test_below_threshold_then_multi_signal_crosses_and_email_cites_both(env):
    svc, rt, _, fresh = env
    a = fresh[1]
    n = lambda e: normalizer.normalize_n8n(e)  # noqa: E731
    r1 = await process_event(rt, n(event(a["domain"], "tech_added", {"technology": "Kafka"})))
    assert r1["status"] == "below_threshold" and r1["composite_score"] == 55 and not svc.repo.drafts
    r2 = await process_event(rt, n(event(a["domain"], "hiring_spike", {"open_roles": 4})))
    assert r2["status"] == "triggered" and r2["composite_score"] == pytest.approx(84.25, abs=0.1)
    assert r2["latency_ms"] < 8000 and r2["signals_considered"] == 2
    body = next(iter(svc.repo.drafts.values()))["body_text"]
    assert "hiring 4 engineers" in body
    # consumed signals can't re-trigger the same account
    r3 = await process_event(rt, n(event(a["domain"], "tech_removed", {"technology": "Okta"})))
    assert r3["status"] == "below_threshold" and r3["signals_considered"] == 1
    dup = await process_event(rt, n(event(a["domain"], "tech_added", {"technology": "Okta"})))
    assert dup["status"] == "duplicate"  # same signal type + account + day collapses


async def test_champion_plus_hiring_email_references_both_signals(env):
    svc, rt, _, fresh = env
    a = fresh[2]
    await process_event(rt, normalizer.normalize_n8n(event(a["domain"], "hiring_spike", {"open_roles": 6})))
    out = await process_event(rt, normalizer.normalize_n8n(
        event(a["domain"], "champion_moved", {"new_title": "VP of Engineering"})))
    assert out["status"] == "triggered"
    body = next(iter(svc.repo.drafts.values()))["body_text"]
    assert "VP of Engineering" in body and "hiring 6 engineers" in body
    assert all(q["passed"] for q in svc.repo.qa)  # multi-signal copy still passes the hallucination check


async def test_stale_signals_decay_below_threshold(env):
    _, rt, _, fresh = env
    out = await process_event(rt, normalizer.normalize_n8n(event(fresh[3]["domain"], "champion_moved", days_ago=45)))
    assert out["status"] == "below_threshold" and out["composite_score"] < 15


async def test_customers_and_unknown_domains_are_ignored(env):
    svc, rt, _, _ = env
    cust = next(a for a in svc.repo.accounts.values() if a["is_customer"])
    assert (await process_event(rt, normalizer.normalize_n8n(event(cust["domain"], "funding_round"))))["status"] == "ignored_customer"
    assert (await process_event(rt, normalizer.normalize_n8n(event("nope.invalid", "funding_round"))))["status"] == "unknown_account"
    assert not svc.repo.drafts


async def test_sales_nav_csv_webhook(env):
    svc, _, app, fresh = env
    a, b = fresh[4]["domain"], fresh[5]["domain"]
    csv = (f"signal_type,company_domain,contact_email,occurred_at\n"
           f"champion_moved,{a},,{datetime.now(UTC).isoformat()}\n"
           f"job_change,,x@y.example,{datetime.now(UTC).isoformat()}\n"
           f"funding_round,{b},,{datetime.now(UTC).isoformat()}\n").encode()
    hdr = {normalizer.N8N_SIGNATURE_HEADER: normalizer.sign_hmac(SECRET, csv), "Content-Type": "text/csv"}
    r = await post(app, "/webhooks/signals/sales-nav", csv, hdr)
    body = r.json()
    assert r.status_code == 202 and body["events"] == 2 and len(body["row_errors"]) == 1
    assert {s["source"] for s in svc.repo.signals.values() if s["account_id"] in
            {x["id"] for x in svc.repo.accounts.values() if x["domain"] in (a, b)}} == {"sales_navigator"}
    unsigned = await post(app, "/api/v1/signals/import-csv", csv, {"Content-Type": "text/csv"})
    assert unsigned.status_code == 401


async def test_clay_callback_enriches_and_validates_signature(env):
    svc, _, app, _ = env
    payload = json.dumps(clay.mock_enrichment_payload("nimbuspay.example")).encode()
    good = {clay.SIGNATURE_HEADER: clay.sign("local_clay_secret", payload)}
    bad = {clay.SIGNATURE_HEADER: "0" * 64}
    assert (await post(app, "/webhooks/clay/enrichment-complete", payload, bad)).status_code == 401
    r = await post(app, "/webhooks/clay/enrichment-complete", payload, good)
    assert r.status_code == 200 and r.json()["status"] in ("enriched", "ignored")


async def test_event_model_roundtrip():
    e = normalizer.normalize_clay({"event": "funding", "company_domain": "A.example", "data": {"round": "Series A"},
                                   "occurred_at": "2026-01-02T00:00:00Z"})
    assert isinstance(e, SignalEvent) and e.signal_type == "funding_round" and e.domain == "a.example"
    assert e.idempotency_key.startswith("clay:")
    with pytest.raises(ValueError):
        normalizer.normalize_clay({"event": "weird", "domain": "a.example"})
