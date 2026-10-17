"""FastAPI gateway: mounts every system's router, health checks, unsubscribe + Slack webhooks, MCP over SSE."""

import json
import logging
import os
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from gtm_core.config import get_settings
from gtm_core.db import dispose_engine
from gtm_core.observability import setup_tracing
from gtm_core.redis_client import get_redis
from gtm_core.repo import SqlRepo
from gtm_integrations.slack import verify_signature
from gtm_safety import audit as audit_mod
from gtm_safety import compliance
from gtm_safety.gate import ApprovalError
from sys1_autonomous_sdr.graph import SdrRunner, SdrRuntime, postgres_checkpointer, set_runtime
from sys1_autonomous_sdr.router import router as sdr_router
from sys2_signal_engine.router import router as signals_router
from sys2_signal_engine.router import set_enqueue
from sys3_command_center_mcp.server import create_server
from sys3_command_center_mcp.services import build_services
from sys3_command_center_mcp.voice_bridge import VoiceSession, websocket_router
from sys4_outbound_qa.router import router as qa_router
from sys5_lookalike_finder.router import router as lookalike_router

log = logging.getLogger("gtm.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    setup_tracing("gtm-api")
    async with AsyncExitStack() as stack:
        if os.environ.get("GTM_REPO", "sql") == "memory":
            svc = await build_services(redis=get_redis())  # seeded sandbox, no database needed
        else:
            audit_mod.set_sink(audit_mod.DbAuditSink())
            svc = await build_services(repo=SqlRepo(), redis=get_redis())
            svc.runner = SdrRunner(svc.deps, await stack.enter_async_context(postgres_checkpointer(s.database_url)))
        set_runtime(SdrRuntime(svc.deps, svc.runner))
        app.state.services = svc
        try:
            if os.environ.get("GTM_REPO", "sql") == "memory":
                raise RuntimeError("in-memory dev mode")
            from arq import create_pool
            from arq.connections import RedisSettings

            rs = RedisSettings.from_dsn(s.redis_url)
            rs.conn_retries = 1
            pool = await create_pool(rs)

            async def enqueue(events: list[dict]) -> None:
                await pool.enqueue_job("process_signal_job", events)

            set_enqueue(enqueue)
        except Exception:
            log.warning("ARQ unavailable; signals are processed in-process")
        mcp = create_server(svc)
        app.mount("/mcp", mcp.sse_app())
        app.include_router(websocket_router(VoiceSession(svc)))
        yield
        set_enqueue(None)
        set_runtime(None)
    await dispose_engine()


app = FastAPI(title="GTM AI Platform", version="0.1.0", lifespan=lifespan)
for r in (sdr_router, signals_router, qa_router, lookalike_router):
    app.include_router(r)


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    log.info(json.dumps({"cid": cid, "path": request.url.path, "status": response.status_code,
                         "ms": round((time.perf_counter() - t0) * 1000, 1)}))
    return response


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"fail: {e.__class__.__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


async def _unsubscribe(token: str) -> bool:
    email = compliance.verify_unsubscribe_token(token)
    if not email:
        return False
    from gtm_core.redis_client import check_and_set_idempotency  # noqa: F401  (token replays are harmless: idempotent)

    repo = request_runtime().deps.repo
    contact = await repo.get_contact_by_email(email)
    if contact:
        await repo.update_contact(contact["id"], is_unsubscribed=True, unsubscribed_at=datetime.now(UTC))
    await repo.add_suppression(email, "email", "one-click unsubscribe")
    await repo.cancel_drafts_for(email)
    await audit_mod.audit("unsubscribe_link", "unsubscribe", "contact", email, "auto")
    return True


def request_runtime():
    from sys1_autonomous_sdr.graph import get_runtime

    return get_runtime()


@app.get("/webhooks/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(token: str) -> HTMLResponse:
    if not await _unsubscribe(token):
        raise HTTPException(400, "invalid or expired unsubscribe link")
    return HTMLResponse("<h3>You have been unsubscribed.</h3><p>You will not receive further emails from us.</p>")


@app.post("/webhooks/unsubscribe")
async def unsubscribe_post(token: str) -> dict:  # RFC 8058 one-click (List-Unsubscribe-Post)
    if not await _unsubscribe(token):
        raise HTTPException(400, "invalid or expired unsubscribe link")
    return {"status": "unsubscribed"}


@app.post("/webhooks/slack/interactions")
async def slack_interactions(request: Request) -> dict:
    body = await request.body()
    if not verify_signature(get_settings().slack_signing_secret, request.headers.get("x-slack-request-timestamp", ""),
                            body, request.headers.get("x-slack-signature", "")):
        raise HTTPException(401, "invalid slack signature")
    payload = json.loads(parse_qs(body.decode())["payload"][0])
    action = payload["actions"][0]
    reviewer = payload.get("user", {}).get("id", "slack")
    verdict = {"approve": "approved", "reject": "rejected"}.get(action["action_id"])
    if verdict is None:
        return {"text": "Editing is available via POST /api/v1/sdr/approvals/{id}/resolve with edited_body."}
    rt = request_runtime()
    try:
        st = await rt.runner.resume_by_approval(action["value"], {"decision": verdict, "reviewer": reviewer})
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ApprovalError:
        return {"text": "This approval was already handled; nothing was changed."}
    return {"text": f"{verdict}. Thread {st['thread_id']} {'finished' if st['finished'] else 'continuing'}."}
