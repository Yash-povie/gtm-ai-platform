"""FastAPI webhook ingress (/webhooks/signals/*) and the signal -> score -> draft pipeline."""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from gtm_core.config import get_settings
from gtm_core.observability import span
from gtm_core.redis_client import check_and_set_idempotency
from gtm_core.schemas import SignalEvent
from gtm_integrations import clay
from gtm_integrations.sales_nav import SalesNavCsvError, parse_csv
from sys1_autonomous_sdr.graph import SdrRuntime, get_runtime

from sys2_signal_engine import normalizer, scorer

log = logging.getLogger(__name__)
router = APIRouter(tags=["signals"])


async def process_event(rt: SdrRuntime, event: SignalEvent, now: datetime | None = None) -> dict:
    """Persist -> decay -> composite -> (if >= threshold) trigger a System 1 draft. Idempotent."""
    t0 = time.perf_counter()
    now = now or datetime.now(UTC)
    d = rt.deps
    acct = await d.repo.get_account(event.domain)
    if acct is None:
        company = await d.apollo.get_company(event.domain)
        if company is None:
            return {"status": "unknown_account", "domain": event.domain}
        acct = await d.repo.upsert_account(company)
    if acct.get("is_customer"):
        return {"status": "ignored_customer", "domain": event.domain}
    contact = await d.repo.get_contact_by_email(event.contact_email) if event.contact_email else None

    strength = scorer.base_strength(event.signal_type, event.payload)
    decayed = scorer.decay(strength, event.occurred_at, now)
    with span("signal.score", signal_type=event.signal_type, raw_score=strength, decayed_score=decayed):
        sig, created = await d.repo.add_signal(
            account_id=acct["id"], contact_id=contact["id"] if contact else None, signal_type=event.signal_type,
            source=event.source, payload=event.payload, strength=strength, decayed=decayed,
            key=event.idempotency_key or normalizer.idempotency_key(
                event.source, event.domain, event.signal_type, event.contact_email, event.occurred_at),
            occurred_at=event.occurred_at)
    if not created:
        return {"status": "duplicate", "signal_id": sig["id"]}

    window = await d.repo.list_signals(account_id=acct["id"], since=now - timedelta(days=scorer.WINDOW_DAYS),
                                       unprocessed_only=True)
    live = [(s, scorer.decay(float(s["signal_strength"]), s["occurred_at"], now)) for s in window]
    score = scorer.composite([x for _, x in live])
    result = {"signal_id": sig["id"], "composite_score": score, "decayed_score": decayed, "domain": event.domain,
              "signals_considered": len(live)}
    if not scorer.crosses_threshold(score):
        return {**result, "status": "below_threshold"}

    st = await rt.runner.start_for_signal(acct["id"], acct["domain"], acct["name"], sig["id"],
                                          campaign_id="signal-triggered")
    for s, _ in live:  # consumed: they must not re-trigger the same account
        await d.repo.update_signal(s["id"], processed_at=now)
    return {**result, "status": "triggered", "thread_id": st["thread_id"], "pending_approvals": st["pending_approvals"],
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


async def _accept(request: Request, secret: str, header: str) -> tuple[bytes, bool]:
    body = await request.body()
    if not normalizer.verify_hmac(secret, body, request.headers.get(header)):
        raise HTTPException(401, "invalid signature")
    from gtm_core.redis_client import get_redis

    fresh = await check_and_set_idempotency(get_redis(), "signals", hashlib.sha256(body).hexdigest())
    return body, fresh


_enqueue = None  # set by the gateway to hand events to the ARQ worker (webhooks must ack in <30s)


def set_enqueue(fn) -> None:
    global _enqueue
    _enqueue = fn


async def _dispatch(bg: BackgroundTasks, events: list[SignalEvent]) -> None:
    if _enqueue is not None:
        await _enqueue([e.model_dump(mode="json") for e in events])
    else:
        bg.add_task(_run, get_runtime(), events)


async def _run(rt: SdrRuntime, events: list[SignalEvent]) -> None:
    for e in events:
        try:
            log.info("signal processed: %s", await process_event(rt, e))
        except Exception:
            log.exception("signal processing failed for %s", e.domain)


@router.post("/webhooks/signals/ingest", status_code=202)
async def ingest(request: Request, bg: BackgroundTasks) -> dict:
    body, fresh = await _accept(request, get_settings().n8n_webhook_secret, normalizer.N8N_SIGNATURE_HEADER)
    if not fresh:
        return {"status": "duplicate_webhook"}
    try:
        payload = json.loads(body)
        items = payload if isinstance(payload, list) else [payload]
        events = [normalizer.normalize_n8n(p) for p in items]
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"bad payload: {e}") from e
    await _dispatch(bg, events)
    return {"status": "accepted", "events": len(events)}


async def _csv_response(request: Request, bg: BackgroundTasks) -> dict:
    body, fresh = await _accept(request, get_settings().n8n_webhook_secret, normalizer.N8N_SIGNATURE_HEADER)
    if not fresh:
        return {"status": "duplicate_webhook"}
    try:
        events, errors = parse_csv(body.decode())
    except SalesNavCsvError as e:
        raise HTTPException(400, str(e)) from e
    await _dispatch(bg, events)
    return {"status": "accepted", "events": len(events), "row_errors": errors}


@router.post("/webhooks/signals/sales-nav", status_code=202)
async def sales_nav(request: Request, bg: BackgroundTasks) -> dict:
    """Clay / PhantomBuster / manual CSV export of Sales Navigator alerts. No LinkedIn scraping."""
    return await _csv_response(request, bg)


@router.post("/api/v1/signals/import-csv", status_code=202)
async def import_csv(request: Request, bg: BackgroundTasks) -> dict:
    return await _csv_response(request, bg)


@router.post("/webhooks/clay/enrichment-complete")
async def clay_enrichment_complete(request: Request, bg: BackgroundTasks) -> dict:
    body, fresh = await _accept(request, get_settings().clay_signing_secret, clay.SIGNATURE_HEADER)
    if not fresh:
        return {"status": "duplicate_webhook"}
    data = json.loads(body)
    rt = get_runtime()
    if "event" in data:  # Clay table row describing a buying signal
        try:
            await _dispatch(bg, [normalizer.normalize_clay(data)])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"status": "accepted_signal"}
    if data.get("enriched") and data.get("company"):
        from gtm_core.schemas import AccountData, ContactData

        acct = await rt.deps.repo.upsert_account(AccountData(**data["company"]))
        n = 0
        for c in data.get("contacts", []):
            cd = ContactData(**c)
            if cd.email:
                await rt.deps.repo.upsert_contact(cd, acct["id"])
                n += 1
        return {"status": "enriched", "domain": acct["domain"], "contacts": n}
    return {"status": "ignored"}


@router.post("/webhooks/mock-clay")
async def mock_clay(request: Request) -> dict:
    """Local stand-in for a Clay inbound-webhook table (CLAY_WEBHOOK_URL default)."""
    return {"status": "queued", "mock": True}
