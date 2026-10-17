"""Natural-language / voice orchestrator: plan tool calls, confirm writes, execute (LangGraph supervisor)."""

import re
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sys3_command_center_mcp import tools_read as r
from sys3_command_center_mcp import tools_write as w
from sys3_command_center_mcp.server import TOOL_PERMISSIONS
from sys3_command_center_mcp.services import Services


class CommandCenterState(TypedDict, total=False):
    session_id: str
    user_utterance: str
    input_modality: Literal["text", "voice"]
    planned_tool_calls: list[dict]
    requires_confirmation: bool
    confirmation_received: bool | None
    tool_outputs: list[dict]
    final_response: str


def plan(utterance: str) -> list[dict]:
    """Deterministic planner. Steps may reference earlier output via {"foreach": "lookalikes"}."""
    u = utterance.lower()
    steps: list[dict] = []
    if m := re.search(r"(?:top\s+(\d+)\s+)?lookalikes?\s+(?:to|of|for)\s+([\w.\-]+)", u):
        steps.append({"tool": "find_lookalike_accounts",
                      "args": {"seed_domain": m.group(2), "top_k": int(m.group(1) or 5), "min_shared_tech": 1}})
    if m := re.search(r"enrich\s+(?:their|the)\s+([a-z ]+?)(?:,|\band\b|$)", u):
        title = {"vp of engineering": "VP of Engineering", "head of data": "Head of Data", "cto": "CTO"}.get(
            m.group(1).strip(), m.group(1).strip().title())
        steps.append({"tool": "enrich_prospect_waterfall", "args": {"target_titles": [title]},
                      "foreach": "lookalikes" if steps else None})
    if re.search(r"(follow-?up\s+)?task", u):
        steps.append({"tool": "upsert_crm_deal_or_task", "args": {"object_type": "task", "title": "Follow up"},
                      "foreach": "lookalikes" if steps else None})
    if "signal" in u:
        steps.append({"tool": "query_active_signals", "args": {}})
    if m := re.search(r"(?:show|account|360)\s+(?:for\s+)?([\w.\-]+)", u):
        steps.append({"tool": "get_account_360", "args": {"domain": m.group(1)}})
    if re.search(r"\b(delete|purge|erase|remove)\b.*\b(account|prospect|contact)\b", u):
        target = re.search(r"(?:delete|purge|erase|remove)\s+(?:the\s+)?([\w.\- ]+?)\s+(?:account|prospect|contact)", u)
        steps.append({"tool": "purge_or_suppress_prospect",
                      "args": {"email_or_domain": (target.group(1) if target else "").replace(" ", ""),
                               "reason": "voice command"}})
    return steps


def build_command_graph(svc: Services):
    async def plan_node(state: CommandCenterState) -> dict:
        steps = plan(state["user_utterance"])
        needs = any(TOOL_PERMISSIONS[s["tool"]] != "read" for s in steps)
        return {"planned_tool_calls": steps, "requires_confirmation": needs, "tool_outputs": []}

    async def confirm_node(state: CommandCenterState) -> dict:
        summary = [f"{s['tool']}({s['args']})" for s in state["planned_tool_calls"]]
        ok = interrupt({"type": "confirm_writes", "plan": summary})
        return {"confirmation_received": bool(ok)}

    async def execute_node(state: CommandCenterState) -> dict:
        outputs: list[dict] = []
        carried: dict[str, Any] = {}
        for step in state["planned_tool_calls"]:
            args = dict(step["args"])
            items = [None]
            if step.get("foreach") and carried.get(step["foreach"]):
                items = carried[step["foreach"]]
            for item in items:
                call = dict(args)
                if item is not None:
                    call["domain"] = item["domain"]
                    if step["tool"] == "upsert_crm_deal_or_task":
                        call["title"] = f"Follow up with {item['domain']}"
                out = await _call(svc, step["tool"], call)
                outputs.append({"tool": step["tool"], "args": call, "result": out})
                if step["tool"] == "find_lookalike_accounts":
                    carried["lookalikes"] = out.get("lookalikes", [])
        return {"tool_outputs": outputs}

    async def respond_node(state: CommandCenterState) -> dict:
        if not state.get("planned_tool_calls"):
            return {"final_response": "I couldn't map that to a GTM action."}
        if state.get("requires_confirmation") and not state.get("confirmation_received"):
            return {"final_response": "Cancelled: write actions were not confirmed. Nothing was changed."}
        done = ", ".join(f"{o['tool']}" for o in state.get("tool_outputs", []))
        return {"final_response": f"Done. Ran: {done}."}

    g = StateGraph(CommandCenterState)
    g.add_node("plan", plan_node)
    g.add_node("confirm", confirm_node)
    g.add_node("execute", execute_node)
    g.add_node("respond", respond_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: "confirm" if s["requires_confirmation"] else
                            ("execute" if s["planned_tool_calls"] else "respond"),
                            {"confirm": "confirm", "execute": "execute", "respond": "respond"})
    g.add_conditional_edges("confirm", lambda s: "execute" if s["confirmation_received"] else "respond",
                            {"execute": "execute", "respond": "respond"})
    g.add_edge("execute", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=InMemorySaver())


async def _call(svc: Services, tool: str, args: dict) -> dict:
    table = {
        "find_lookalike_accounts": r.find_lookalike_accounts, "query_active_signals": r.query_active_signals,
        "get_account_360": r.get_account_360, "enrich_prospect_waterfall": w.enrich_prospect_waterfall,
        "upsert_crm_deal_or_task": w.upsert_crm_deal_or_task, "purge_or_suppress_prospect": w.purge_or_suppress_prospect,
    }
    return await table[tool](svc, **args)


class VoiceSession:
    """Drive the supervisor: `say()` plans (and pauses on writes); `confirm()` resumes."""

    def __init__(self, svc: Services):
        self.graph = build_command_graph(svc)

    async def say(self, session_id: str, utterance: str, modality: Literal["text", "voice"] = "text") -> dict:
        cfg = {"configurable": {"thread_id": session_id}}
        await self.graph.ainvoke({"session_id": session_id, "user_utterance": utterance, "input_modality": modality}, cfg)
        return await self._state(cfg)

    async def confirm(self, session_id: str, yes: bool) -> dict:
        cfg = {"configurable": {"thread_id": session_id}}
        await self.graph.ainvoke(Command(resume=yes), cfg)
        return await self._state(cfg)

    async def _state(self, cfg: dict) -> dict:
        snap = await self.graph.aget_state(cfg)
        v = snap.values
        pending = [i.value for t in snap.tasks for i in t.interrupts]
        return {"awaiting_confirmation": bool(pending), "plan": pending[0]["plan"] if pending else None,
                "response": v.get("final_response"), "tool_outputs": v.get("tool_outputs", [])}


def websocket_router(session: VoiceSession):
    """Optional WebSocket relay: send {"session_id","utterance"} or {"session_id","confirm":true|false}."""
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect

    router = APIRouter()

    @router.websocket("/voice/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        try:
            while True:
                msg = await sock.receive_json()
                if "confirm" in msg:
                    await sock.send_json(await session.confirm(msg["session_id"], bool(msg["confirm"])))
                else:
                    await sock.send_json(await session.say(msg["session_id"], msg["utterance"],
                                                           msg.get("modality", "voice")))
        except WebSocketDisconnect:
            return

    return router
