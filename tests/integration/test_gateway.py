"""Gateway wiring: health, correlation ids, one-click unsubscribe, Slack-signed approvals, MCP mount."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from gtm_safety import audit as audit_mod
from gtm_safety import compliance


@pytest.fixture
async def gw(redis, monkeypatch):
    monkeypatch.setenv("GTM_REPO", "memory")
    audit_mod.set_sink(audit_mod.MemoryAuditSink())
    from apps.api_gateway.main import app

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield app, c


async def test_health_ready_and_correlation_id(gw):
    _, c = gw
    r = await c.get("/healthz", headers={"x-correlation-id": "abc123"})
    assert r.status_code == 200 and r.headers["x-correlation-id"] == "abc123"
    assert (await c.get("/readyz")).json()["checks"]["redis"] == "ok"
    assert (await c.get("/healthz")).headers["x-correlation-id"]  # generated when absent


async def test_openapi_exposes_all_system_routes(gw):
    _, c = gw
    paths = (await c.get("/openapi.json")).json()["paths"]
    for p in ("/api/v1/sdr/campaigns", "/api/v1/qa/evaluate", "/api/v1/qa/attribution", "/api/v1/lookalike/rank",
              "/webhooks/signals/ingest", "/webhooks/unsubscribe", "/webhooks/slack/interactions"):
        assert p in paths, p


async def test_qa_and_lookalike_endpoints(gw):
    _, c = gw
    r = await c.post("/api/v1/qa/evaluate", json={"subject": "s", "body": "Hi Sam, congrats on the $40M Series B!",
                                                  "facts": ["Series A", "$15M"]})
    assert r.status_code == 200 and r.json()["passed"] is False and r.json()["unsupported_claims"]
    import os

    os.environ["LOOKALIKE_STORE"] = "memory"
    try:
        r = await c.get("/api/v1/lookalike/rank", params={"seed_domain": "stripelike.example", "top_k": 3})
        assert r.status_code == 200 and len(r.json()) == 3 and "breakdown" in r.json()[0]
        assert (await c.get("/api/v1/lookalike/rank", params={"seed_domain": "nope.example"})).status_code == 404
    finally:
        os.environ.pop("LOOKALIKE_STORE")


async def test_one_click_unsubscribe_suppresses_and_cancels(gw):
    app, c = gw
    repo = app.state.services.repo
    acct = next(a for a in repo.accounts.values() if not a["is_customer"])
    contact = (await repo.list_contacts(acct["id"]))[0]
    token = compliance.build_unsubscribe_token(contact["email"])
    assert (await c.get("/webhooks/unsubscribe", params={"token": "garbage"})).status_code == 400
    page = await c.get("/webhooks/unsubscribe", params={"token": token})
    assert page.status_code == 200 and "unsubscribed" in page.text.lower()
    assert (await repo.get_contact_by_email(contact["email"]))["is_unsubscribed"] is True
    assert contact["email"] in {e for e, _ in await repo.suppression_entries()}
    assert (await c.post("/webhooks/unsubscribe", params={"token": token})).json() == {"status": "unsubscribed"}


def slack_headers(body: bytes, secret="mock_slack_secret") -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded"}


async def test_campaign_api_then_slack_approval_sends(gw):
    app, c = gw
    svc = app.state.services
    r = await c.post("/api/v1/sdr/campaigns", json={"icp_prompt": "Fintech companies", "campaign_id": "gw",
                                                     "max_accounts": 1})
    assert r.status_code == 202
    status = (await c.get(r.json()["status_url"])).json()
    assert status["pending_approvals"] == 1 and svc.deps.sender.sent == []
    approval_id = status["pending_approval_ids"][0]

    body = urlencode({"payload": json.dumps({"user": {"id": "U1"},
                                             "actions": [{"action_id": "approve", "value": approval_id}]})}).encode()
    bad = await c.post("/webhooks/slack/interactions", content=body,
                       headers={**slack_headers(body), "x-slack-signature": "v0=bad"})
    assert bad.status_code == 401 and svc.deps.sender.sent == []
    ok = await c.post("/webhooks/slack/interactions", content=body, headers=slack_headers(body))
    assert ok.status_code == 200 and len(svc.deps.sender.sent) == 1
    approval = await svc.repo.get_approval(approval_id)
    assert approval["status"] == "approved" and approval["reviewer_id"] == "U1"


async def test_mcp_sse_app_is_mounted(gw):
    app, _ = gw
    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)
