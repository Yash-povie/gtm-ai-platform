"""LangGraph node implementations for the Autonomous SDR (System 1)."""

import re
from datetime import UTC, datetime

from gtm_core.observability import new_trace_id, span
from gtm_safety import compliance
from gtm_safety.audit import audit
from gtm_safety.gate import ApprovalError
from gtm_safety.policy_engine import ActionContext
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from sys4_outbound_qa.rubrics import JudgeInput

from sys1_autonomous_sdr.prompts import (
    DEFAULT_VALUE_PROP,
    DRAFT_SYSTEM,
    DRAFT_USER,
    PROMPT_NAME,
    PROMPT_VERSION,
    build_source_facts,
)
from sys1_autonomous_sdr.state import DraftBundle, SdrDeps, SDRGraphState

MAX_ATTEMPTS = 3
DECISION_MAKERS = ("c_suite", "vp", "head", "director")


def icp_score(account: dict, filters: dict, contacts: list[dict]) -> float:
    s = 0.0
    inds = filters.get("industries") or []
    s += (25 if account.get("industry") in inds else 0) if inds else 15
    kws = filters.get("tech_keywords") or []
    stack = set(account.get("tech_stack") or [])
    s += 30 * len(stack & set(kws)) / len(kws) if kws else 15
    lo, hi = filters.get("headcount_min", 0), filters.get("headcount_max", 10**9)
    emp = account.get("employee_count") or 0
    s += 15 if lo <= emp <= hi and (lo > 0 or hi < 10**9) else (7 if lo == 0 and hi == 10**9 else 0)
    stages = filters.get("funding_stages") or []
    s += (15 if account.get("funding_stage") in stages else 0) if stages else 7
    s += 15 if any(c.get("seniority") in DECISION_MAKERS for c in contacts) else 0
    return round(min(100.0, s), 2)


def tier_for(score: float, employee_count: int | None) -> str:
    if score >= 80 or (employee_count or 0) >= 500:
        return "tier_1"
    return "tier_2" if score >= 60 else "tier_3"


def pick_contact(contacts: list[dict]) -> dict | None:
    eligible = [c for c in contacts if not c.get("is_unsubscribed") and c.get("email_status") != "bounced"]
    rank = {"c_suite": 0, "vp": 1, "head": 2, "director": 3}
    eligible.sort(key=lambda c: (rank.get(c.get("seniority"), 9), c["email"]))
    return eligible[0] if eligible else None


def classify_reply(text: str) -> str:
    t = text.lower()
    if compliance.is_opt_out_text(text):
        return "unsubscribe"
    if re.search(r"out of (the )?office|away until|on (vacation|leave)|auto[- ]?reply", t):
        return "ooo"
    if re.search(r"not interested|no thanks|wrong person|not a fit|don'?t need|no longer", t):
        return "negative"
    if re.search(r"interested|let'?s (talk|chat|connect)|sounds good|send (over )?(some )?times|book|happy to|yes", t):
        return "positive"
    return "neutral"


def make_nodes(d: SdrDeps) -> dict:  # noqa: C901 - one closure per node keeps deps explicit
    async def parse_icp_node(state: SDRGraphState) -> dict:
        with span("sdr.parse_icp", campaign_id=state["campaign_id"]):
            return {"parsed_icp_filters": await d.icp_parser.parse(state["raw_icp_prompt"]), "queue_index": 0}

    async def discover_accounts_node(state: SDRGraphState) -> dict:
        limit = state.get("max_accounts", 10)
        # Over-fetch: customers and low-scoring (tier_3) accounts are dropped after this step.
        found = await d.apollo.search_companies(state["parsed_icp_filters"], limit=max(50, limit * 10))
        cands = []
        for a in found:
            if a.is_customer:
                continue  # never prospect existing customers
            rec = await d.repo.upsert_account(a)
            cands.append({"account_id": rec["id"], "domain": rec["domain"], "name": rec["name"], "icp_score": 0.0,
                          "account_tier": "tier_3", "rationale": ""})
        return {"candidate_accounts": cands}

    async def enrich_waterfall_node(state: SDRGraphState) -> dict:
        titles = state["parsed_icp_filters"].get("titles")
        errors = []
        for c in state["candidate_accounts"]:
            try:
                await d.clay.push_row({"domain": c["domain"]})
                for p in await d.apollo.search_people(c["domain"], titles, limit=3):
                    if p.email:
                        await d.repo.upsert_contact(p, c["account_id"])
            except Exception as e:  # one bad account must not stop the campaign
                errors.append(f"enrich {c['domain']}: {e}")
        return {"errors": errors}

    async def score_and_tier_node(state: SDRGraphState) -> dict:
        filters, scored = state["parsed_icp_filters"], []
        for c in state["candidate_accounts"]:
            acct = await d.repo.get_account_by_id(c["account_id"])
            contacts = await d.repo.list_contacts(c["account_id"])
            score = icp_score(acct, filters, contacts)
            tier = tier_for(score, acct.get("employee_count"))
            await d.repo.update_account(c["account_id"], icp_score=score, account_tier=tier)
            if tier == "tier_3":
                continue
            scored.append({**c, "icp_score": score, "account_tier": tier,
                           "rationale": f"score {score}; {len(contacts)} contacts; {acct.get('industry')}"})
        scored.sort(key=lambda c: (-c["icp_score"], c["domain"]))
        return {"candidate_accounts": scored[: state.get("max_accounts", 10)], "queue_index": 0}

    async def pick_next_node(state: SDRGraphState) -> dict:
        idx, cands = state.get("queue_index", 0), state.get("candidate_accounts", [])
        if idx >= len(cands):
            return {"pick_status": "done", "current_draft": None}
        cand = cands[idx]
        contacts = await d.repo.list_contacts(cand["account_id"])
        contact = pick_contact(contacts)
        if contact is None:
            return {"queue_index": idx + 1, "pick_status": "skip", "current_draft": None,
                    "errors": [f"no eligible contact for {cand['domain']}"]}
        return {
            "queue_index": idx + 1,
            "pick_status": "ok",
            "current_draft": DraftBundle(
                account_id=cand["account_id"], account_domain=cand["domain"], contact_id=contact["id"],
                contact_email=contact["email"], attempt_count=0, qa_critique=None, sent=False,
                trigger_signal_id=state.get("trigger_signal_id")),
        }

    async def draft_email_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        acct = await d.repo.get_account_by_id(cur["account_id"])
        contact = next(c for c in await d.repo.list_contacts(cur["account_id"]) if c["id"] == cur["contact_id"])
        signals = await d.repo.list_signals(account_id=cur["account_id"])
        signal = next((s for s in signals if s["id"] == cur.get("trigger_signal_id")), signals[0] if signals else None)
        facts = build_source_facts(acct, contact, signal)
        value_prop = state.get("value_prop") or DEFAULT_VALUE_PROP
        out = await d.drafter.draft(facts=facts, value_prop=value_prop, critique=cur.get("qa_critique"),
                                    attempt=cur["attempt_count"] + 1)
        pv = await d.repo.ensure_prompt_version(PROMPT_NAME, PROMPT_VERSION, DRAFT_SYSTEM, DRAFT_USER,
                                                d.settings.llm_primary_model)
        with span("sdr.generate_draft", prompt_version_id=pv["id"], model_name=d.settings.llm_primary_model,
                  facts_count=len(out.facts_used)):
            fields = dict(subject=out.subject, body_text=out.body, facts_used=out.facts_used, status="draft")
            if cur.get("draft_id"):
                await d.repo.update_draft(cur["draft_id"], **fields)
            else:
                row = await d.repo.create_draft(
                    account_id=cur["account_id"], contact_id=cur["contact_id"], prompt_version_id=pv["id"],
                    campaign_id=state["campaign_id"], safety_tier="block",  # fail-closed until the gate decides
                    trigger_signal_id=cur.get("trigger_signal_id"), **fields)
                cur["draft_id"] = row["id"]
        cur.update(subject=out.subject, body_text=out.body, facts_used=out.facts_used,
                   attempt_count=cur["attempt_count"] + 1, source_facts=facts)
        return {"current_draft": cur}

    async def outbound_qa_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        inp = JudgeInput(source_facts=cur["source_facts"], value_prop=state.get("value_prop") or DEFAULT_VALUE_PROP,
                         subject=cur["subject"], body=cur["body_text"], draft_id=cur.get("draft_id"),
                         attempt=cur["attempt_count"])
        result = await d.judge.evaluate(inp)
        meta = getattr(d.judge, "last_meta", {"judge_model": "unknown", "latency_ms": 0})
        await d.repo.add_qa_evaluation(cur["draft_id"], result, meta["judge_model"], meta["latency_ms"], new_trace_id())
        critique = result.critique
        if result.unsupported_claims:
            critique += " Unsupported: " + "; ".join(result.unsupported_claims)
        cur.update(qa_passed=result.passed, qa_score=result.composite_score, qa_hallucination=result.hallucination_score,
                   qa_critique=None if result.passed else critique)
        return {"current_draft": cur}

    def route_after_qa(state: SDRGraphState) -> str:
        cur = state["current_draft"]
        if cur["qa_passed"]:
            return "safety_gate"
        return "draft" if cur["attempt_count"] < MAX_ATTEMPTS else "qa_failed"

    async def qa_failed_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        await d.repo.update_draft(cur["draft_id"], status="qa_failed", safety_tier="block")
        cur.update(safety_tier="block", block_rule="qa_failed_after_retries")
        await audit("sys1_sdr", "qa_failed", "email_draft", cur["draft_id"], "block",
                    after={"attempts": cur["attempt_count"], "critique": cur.get("qa_critique")})
        return {"current_draft": cur}

    async def safety_gate_node(state: SDRGraphState, config: RunnableConfig) -> dict:
        cur = dict(state["current_draft"])
        acct = await d.repo.get_account_by_id(cur["account_id"])
        contact = next(c for c in await d.repo.list_contacts(cur["account_id"]) if c["id"] == cur["contact_id"])
        text, html, _ = d.gate.finalize_payload(cur["contact_email"], cur["body_text"])
        from gtm_core.schemas import QAResult

        qa = QAResult(hallucination_score=cur["qa_hallucination"], tone_score=1.0, relevance_score=1.0,
                      composite_score=cur["qa_score"], passed=cur["qa_passed"])
        decision = await d.gate.classify_email_draft(account=acct, contact=contact, final_text=text, qa=qa,
                                                     target_id=cur["draft_id"])
        cur.update(body_text=text, safety_tier=decision.tier, block_rule=decision.rule_triggered)
        await d.repo.update_draft(cur["draft_id"], body_text=text, body_html=html, safety_tier=decision.tier)
        if decision.tier == "approve":
            thread_id = config["configurable"]["thread_id"]
            approval = await d.gate.create_approval(
                action_type="send_email", target_id=cur["draft_id"], decision=decision,
                payload={"draft_id": cur["draft_id"], "thread_id": thread_id, "to": cur["contact_email"],
                         "subject": cur["subject"], "body": text, "account_tier": acct["account_tier"]},
                title=f"Approve email to {cur['contact_email']}",
                details=f"*Subject:* {cur['subject']}\n```{text[:1500]}```")
            await d.repo.update_draft(cur["draft_id"], status="pending_approval")
            cur.update(approval_id=approval["id"], approval_status="pending")
        elif decision.tier == "auto":
            await d.repo.update_draft(cur["draft_id"], status="approved")
            cur.update(approval_status="auto_approved", approval_id=None)
        return {"current_draft": cur}

    def route_after_gate(state: SDRGraphState) -> str:
        return {"block": "abort", "approve": "human_approval", "auto": "dispatch"}[state["current_draft"]["safety_tier"]]

    async def human_approval_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        decision = interrupt({"type": "awaiting_human_approval", "approval_id": cur["approval_id"],
                              "draft_id": cur["draft_id"]})
        verdict = decision.get("decision", "rejected")
        approval = await d.repo.get_approval(cur["approval_id"])
        if approval and approval["status"] == "pending":
            try:
                await d.gate.resolve_approval(cur["approval_id"], "approved" if verdict in ("approved", "edited")
                                              else "rejected", decision.get("reviewer", "slack"), decision.get("note"))
            except ApprovalError:
                verdict = "rejected"
        elif approval and approval["status"] != "approved" and verdict != "rejected":
            verdict = "rejected"  # resolved as rejected/expired elsewhere: never send
        if verdict == "edited" and decision.get("edited_body"):
            await d.repo.update_draft(cur["draft_id"], body_text=decision["edited_body"], status="draft")
            cur.update(body_text=decision["edited_body"], attempt_count=1, qa_critique=None)
            return {"current_draft": {**cur, "approval_status": "pending"}}
        if verdict == "approved":
            await d.repo.update_draft(cur["draft_id"], status="approved")
            cur["approval_status"] = "approved"
        else:
            await d.repo.update_draft(cur["draft_id"], status="rejected")
            cur["approval_status"] = "rejected"
        return {"current_draft": cur}

    def route_after_approval(state: SDRGraphState) -> str:
        cur = state["current_draft"]
        if cur["approval_status"] == "approved":
            return "dispatch"
        if cur["approval_status"] == "pending":  # edited by a human: must re-pass QA
            return "qa"
        return "log_crm"

    async def abort_and_audit_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        status = "qa_failed" if (cur.get("block_rule") or "").startswith("qa") else "rejected"
        await d.repo.update_draft(cur["draft_id"], status=status)
        await audit("sys1_sdr", "blocked", "email_draft", cur["draft_id"], "block",
                    after={"rule": cur.get("block_rule")})
        return {"current_draft": cur}

    async def dispatch_email_node(state: SDRGraphState) -> dict:
        from gtm_core.redis_client import account_lock

        cur = dict(state["current_draft"])
        acct = await d.repo.get_account_by_id(cur["account_id"])
        contact = await d.repo.get_contact_by_email(cur["contact_email"])
        async with account_lock(d.redis, cur["account_id"]) as got:
            if not got:
                return {"current_draft": cur, "errors": [f"account busy: {cur['account_domain']}"]}
            # Re-check at send time: approvals can wait 48h, so opt-outs / caps may have changed.
            from gtm_core.schemas import QAResult
            qa = QAResult(hallucination_score=cur["qa_hallucination"], tone_score=1.0, relevance_score=1.0,
                          composite_score=cur["qa_score"], passed=True)
            recheck = await d.gate.classify_email_draft(account=acct, contact=contact, final_text=cur["body_text"],
                                                        qa=qa, target_id=cur["draft_id"])
            if recheck.tier == "block":
                cur["block_rule"] = recheck.rule_triggered
                await d.repo.update_draft(cur["draft_id"], status="rejected", safety_tier="block")
                return {"current_draft": cur}
            ok, reason = await d.gate.limiter.consume_outbound(acct["domain"])
            if not ok:
                cur["block_rule"] = reason
                await d.repo.update_draft(cur["draft_id"], status="rejected", safety_tier="block")
                await audit("sys1_sdr", "rate_limited", "email_draft", cur["draft_id"], "block", after={"rule": reason})
                return {"current_draft": cur}
            draft = await d.repo.get_draft(cur["draft_id"])
            _, html, headers = d.gate.finalize_payload(contact["email"], cur["body_text"], draft.get("body_html") or None)
            msg_id = await d.sender.send(to=contact["email"], subject=cur["subject"], text=cur["body_text"],
                                         html=html or draft.get("body_html", ""), headers=headers)
            now = datetime.now(UTC)
            await d.repo.update_draft(cur["draft_id"], status="sent", sent_at=now)
            await d.repo.update_contact(contact["id"], last_contacted_at=now)
            await audit("sys1_sdr", "send_email", "email_draft", cur["draft_id"], cur["safety_tier"],
                        after={"message_id": msg_id, "to": contact["email"]})
            cur["sent"] = True
        return {"current_draft": cur}

    async def log_crm_node(state: SDRGraphState) -> dict:
        cur = dict(state["current_draft"])
        acct = await d.repo.get_account_by_id(cur["account_id"])
        contact = await d.repo.get_contact_by_email(cur["contact_email"])
        errors = []
        from gtm_core.schemas import AccountData, ContactData

        a = AccountData(domain=acct["domain"], name=acct["name"], industry=acct.get("industry"))
        c = ContactData(email=contact["email"], first_name=contact.get("first_name"),
                        last_name=contact.get("last_name"), title=contact.get("title"))
        for label, crm, field_a, field_c in (("hubspot", d.hubspot, "hubspot_company_id", "hubspot_contact_id"),
                                             ("salesforce", d.salesforce, "salesforce_account_id", "salesforce_contact_id")):
            try:
                acct_id = await (crm.upsert_company(a) if label == "hubspot" else crm.upsert_account(a))
                ct_id = await crm.upsert_contact(c, acct_id)
                await d.repo.update_account(acct["id"], **{field_a: acct_id})
                await d.repo.update_contact(contact["id"], **{field_c: ct_id})
                if cur.get("sent"):
                    await crm.log_email(ct_id, cur["subject"], cur["body_text"], datetime.now(UTC))
            except Exception as e:
                errors.append(f"crm {label}: {e}")
        if cur.get("trigger_signal_id"):
            await d.repo.update_signal(cur["trigger_signal_id"], processed_at=datetime.now(UTC))
        return {"completed_drafts": [cur], "current_draft": cur, "errors": errors}

    return {k: v for k, v in locals().items() if k.endswith("_node") or k.startswith("route_")}


async def handle_reply(d: SdrDeps, draft_id: str, reply_text: str) -> dict:
    """Inbound reply webhook handler (the spec's handle_reply_meeting_node)."""
    draft = await d.repo.get_draft(draft_id)
    if draft is None:
        raise LookupError(draft_id)
    sentiment = classify_reply(reply_text)
    await d.repo.update_draft(draft_id, status="replied", reply_sentiment=sentiment, replied_at=datetime.now(UTC))
    contact = await d.repo.get_contact_by_email(
        next(c["email"] for c in await d.repo.list_contacts(draft["account_id"]) if c["id"] == draft["contact_id"]))
    out: dict = {"draft_id": draft_id, "sentiment": sentiment}
    if sentiment == "unsubscribe":
        await d.repo.update_contact(contact["id"], is_unsubscribed=True, unsubscribed_at=datetime.now(UTC))
        await d.repo.add_suppression(contact["email"], "email", "reply opt-out")
        out["drafts_cancelled"] = await d.repo.cancel_drafts_for(contact["email"])
        await audit("sys1_sdr", "reply_unsubscribe", "contact", contact["id"], "auto", after={"email": contact["email"]})
    elif sentiment == "positive":
        acct = await d.repo.get_account_by_id(draft["account_id"])
        out["booking_link"] = f"https://cal.example/book/{draft_id[:8]}"
        out["task_id"] = await d.hubspot.create_task(
            f"Book meeting with {contact.get('first_name') or contact['email']} ({acct['name']})",
            company_id=acct.get("hubspot_company_id"))
    return out


def check_action(ctx: ActionContext):  # exposed for MCP tools / tests
    from gtm_safety.policy_engine import classify_action

    return classify_action(ctx)
