"""Compiled SDR StateGraph, checkpointer helpers, dependency factory and the campaign runner."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from gtm_core.config import Settings, get_settings
from gtm_core.redis_client import get_redis
from gtm_core.repo import MemoryRepo, Repo
from gtm_integrations.apollo import ApolloClient
from gtm_integrations.clay import ClayClient
from gtm_integrations.hubspot import HubSpotClient
from gtm_integrations.salesforce import SalesforceClient
from gtm_integrations.slack import SlackClient
from gtm_safety.gate import ApprovalError, SafetyGate
from gtm_safety.rate_limiter import RateLimiter
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sys4_outbound_qa.judge import QAJudge

from sys1_autonomous_sdr.nodes import make_nodes
from sys1_autonomous_sdr.prompts import DEFAULT_VALUE_PROP, Drafter, IcpParser
from sys1_autonomous_sdr.state import (
    SandboxSender,
    SdrDeps,
    SDRGraphState,
    UnconfiguredLiveSender,
)

RECURSION_LIMIT = 1000  # ~8 steps per account


def build_graph(d: SdrDeps, checkpointer=None):
    n = make_nodes(d)
    g = StateGraph(SDRGraphState)
    g.add_node("parse_icp", n["parse_icp_node"])
    g.add_node("discover", n["discover_accounts_node"])
    g.add_node("enrich", n["enrich_waterfall_node"])
    g.add_node("score", n["score_and_tier_node"])
    g.add_node("pick_next", n["pick_next_node"])
    g.add_node("draft", n["draft_email_node"])
    g.add_node("qa", n["outbound_qa_node"])
    g.add_node("qa_failed", n["qa_failed_node"])
    g.add_node("safety_gate", n["safety_gate_node"])
    g.add_node("human_approval", n["human_approval_node"])
    g.add_node("abort", n["abort_and_audit_node"])
    g.add_node("dispatch", n["dispatch_email_node"])
    g.add_node("log_crm", n["log_crm_node"])

    g.add_conditional_edges(START, lambda s: "pick_next" if s.get("preselected") else "parse_icp",
                            {"pick_next": "pick_next", "parse_icp": "parse_icp"})
    g.add_edge("parse_icp", "discover")
    g.add_edge("discover", "enrich")
    g.add_edge("enrich", "score")
    g.add_edge("score", "pick_next")
    g.add_conditional_edges("pick_next", lambda s: {"ok": "draft", "skip": "pick_next", "done": END}[s["pick_status"]],
                            {"draft": "draft", "pick_next": "pick_next", END: END})
    g.add_edge("draft", "qa")
    g.add_conditional_edges("qa", n["route_after_qa"],
                            {"safety_gate": "safety_gate", "draft": "draft", "qa_failed": "qa_failed"})
    g.add_edge("qa_failed", "log_crm")
    g.add_conditional_edges("safety_gate", n["route_after_gate"],
                            {"abort": "abort", "human_approval": "human_approval", "dispatch": "dispatch"})
    g.add_conditional_edges("human_approval", n["route_after_approval"],
                            {"dispatch": "dispatch", "qa": "qa", "log_crm": "log_crm"})
    g.add_edge("abort", "log_crm")
    g.add_edge("dispatch", "log_crm")
    g.add_edge("log_crm", "pick_next")
    return g.compile(checkpointer=checkpointer or InMemorySaver())


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator:
    """Durable checkpointer so campaigns survive restarts and 48h approval waits."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    conn = database_url.replace("postgresql+asyncpg://", "postgresql://")
    async with AsyncPostgresSaver.from_conn_string(conn) as saver:
        await saver.setup()
        yield saver


def build_deps(
    repo: Repo | None = None,
    settings: Settings | None = None,
    redis=None,
    judge=None,
    drafter=None,
    icp_parser=None,
    sender=None,
    slack: SlackClient | None = None,
) -> SdrDeps:
    s = settings or get_settings()
    repo = repo or MemoryRepo()
    limiter = RateLimiter(redis or get_redis())
    limiter.s = s
    gate = SafetyGate(repo, limiter, slack or SlackClient(), s)
    live = (not s.is_sandbox) and s.gtm_live_send_enabled
    return SdrDeps(
        repo=repo, apollo=ApolloClient(), clay=ClayClient(), hubspot=HubSpotClient("mock" if s.hubspot_private_app_token == "mock" else s.hubspot_private_app_token),
        salesforce=SalesforceClient(), gate=gate, judge=judge or QAJudge(), drafter=drafter or Drafter(),
        icp_parser=icp_parser or IcpParser(), sender=sender or (UnconfiguredLiveSender() if live else SandboxSender()),
        settings=s,
    )


class SdrRunner:
    def __init__(self, deps: SdrDeps, checkpointer=None):
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)

    @staticmethod
    def _cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}

    async def start(self, icp_prompt: str, campaign_id: str, max_accounts: int = 10,
                    value_prop: str = DEFAULT_VALUE_PROP, thread_id: str | None = None) -> dict:
        thread_id = thread_id or f"{campaign_id}:{uuid.uuid4().hex[:8]}"
        await self.graph.ainvoke(
            {"campaign_id": campaign_id, "raw_icp_prompt": icp_prompt, "value_prop": value_prop,
             "max_accounts": max_accounts, "completed_drafts": [], "errors": []}, self._cfg(thread_id))
        return await self.status(thread_id)

    async def start_for_signal(self, account_id: str, domain: str, name: str, signal_id: str | None,
                               campaign_id: str = "signal-triggered", value_prop: str = DEFAULT_VALUE_PROP) -> dict:
        thread_id = f"{campaign_id}:{domain}:{uuid.uuid4().hex[:8]}"
        cand = {"account_id": account_id, "domain": domain, "name": name, "icp_score": 0.0,
                "account_tier": "tier_2", "rationale": "signal-triggered"}
        await self.graph.ainvoke(
            {"campaign_id": campaign_id, "preselected": True, "candidate_accounts": [cand], "queue_index": 0,
             "value_prop": value_prop, "trigger_signal_id": signal_id, "completed_drafts": [], "errors": []},
            self._cfg(thread_id))
        return await self.status(thread_id)

    async def resume(self, thread_id: str, decision: dict) -> dict:
        await self.graph.ainvoke(Command(resume=decision), self._cfg(thread_id))
        return await self.status(thread_id)

    async def resume_by_approval(self, approval_id: str, decision: dict) -> dict:
        approval = await self.deps.repo.get_approval(approval_id)
        if approval is None or "thread_id" not in approval["payload_snapshot"]:
            raise LookupError(f"no resumable thread for approval {approval_id}")
        thread_id = approval["payload_snapshot"]["thread_id"]
        # A stale/replayed approval id must never approve a *different* paused email on the same thread.
        if approval_id not in (await self.status(thread_id))["pending_approval_ids"]:
            raise ApprovalError(f"approval {approval_id} is not awaiting a decision")
        return await self.resume(thread_id, decision)

    async def status(self, thread_id: str) -> dict:
        snap = await self.graph.aget_state(self._cfg(thread_id))
        v = snap.values or {}
        interrupts = [i for t in snap.tasks for i in t.interrupts]
        return {
            "thread_id": thread_id,
            "campaign_id": v.get("campaign_id"),
            "accounts_queued": len(v.get("candidate_accounts", [])),
            "completed": len(v.get("completed_drafts", [])),
            "pending_approvals": len(interrupts),
            "pending_approval_ids": [i.value.get("approval_id") for i in interrupts],
            "finished": bool(v) and not snap.next,  # an unknown / not-yet-started thread is not "finished"
            "errors": v.get("errors", []),
            "drafts": [
                {k: x.get(k) for k in ("draft_id", "account_domain", "contact_email", "safety_tier", "approval_status",
                                       "qa_passed", "qa_score", "sent", "block_rule", "attempt_count")}
                for x in v.get("completed_drafts", [])
            ],
        }


@dataclass
class SdrRuntime:
    deps: SdrDeps
    runner: SdrRunner


_runtime: SdrRuntime | None = None


def set_runtime(rt: SdrRuntime | None) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> SdrRuntime:
    global _runtime
    if _runtime is None:
        deps = build_deps()
        _runtime = SdrRuntime(deps, SdrRunner(deps))
    return _runtime
