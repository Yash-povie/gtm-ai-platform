"""ARQ worker: signal processing, long-running campaigns and approval expiry."""

import asyncio
import os
import sys
from contextlib import AsyncExitStack

from arq import cron
from arq.connections import RedisSettings
from gtm_core.config import get_settings
from gtm_core.redis_client import get_redis
from gtm_core.repo import SqlRepo
from gtm_core.schemas import SignalEvent
from gtm_safety import audit as audit_mod
from sys1_autonomous_sdr.graph import SdrRunner, SdrRuntime, postgres_checkpointer, set_runtime
from sys2_signal_engine.router import process_event
from sys3_command_center_mcp.services import build_services

if sys.platform == "win32":  # psycopg (LangGraph checkpointer) cannot run on the default Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def startup(ctx: dict) -> None:
    s = get_settings()
    stack = AsyncExitStack()
    if os.environ.get("GTM_REPO", "sql") == "memory":
        svc = await build_services(redis=get_redis())
    else:
        audit_mod.set_sink(audit_mod.DbAuditSink())
        svc = await build_services(repo=SqlRepo(), redis=get_redis())
        svc.runner = SdrRunner(svc.deps, await stack.enter_async_context(postgres_checkpointer(s.database_url)))
    set_runtime(SdrRuntime(svc.deps, svc.runner))
    ctx.update(svc=svc, stack=stack)


async def shutdown(ctx: dict) -> None:
    if "stack" in ctx:  # startup may have failed before the stack existed
        await ctx["stack"].aclose()


async def process_signal_job(ctx: dict, events: list[dict]) -> list[dict]:
    from sys1_autonomous_sdr.graph import get_runtime

    rt = get_runtime()
    return [await process_event(rt, SignalEvent.model_validate(e)) for e in events]


async def run_sdr_campaign_job(ctx: dict, icp_prompt: str, campaign_id: str, max_accounts: int = 10) -> dict:
    return await ctx["svc"].runner.start(icp_prompt, campaign_id, max_accounts)


async def expire_approvals_job(ctx: dict) -> int:
    """Approval requests expire after 48h; expired drafts must never be sent."""
    return await ctx["svc"].deps.gate.expire_stale()


class WorkerSettings:
    functions = [process_signal_job, run_sdr_campaign_job]
    cron_jobs = [cron(expire_approvals_job, minute={0, 15, 30, 45})]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 300
