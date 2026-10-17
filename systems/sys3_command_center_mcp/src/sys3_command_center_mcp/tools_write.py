"""Mutating MCP tools. Every one passes through gtm_safety before touching state."""

import hmac
import json
import sys

from gtm_core.schemas import ContactData
from gtm_safety.audit import audit
from gtm_safety.gate import ApprovalError
from gtm_safety.policy_engine import ActionContext

from sys3_command_center_mcp.services import Services

ENRICH_TTL = 604800  # 7 days


async def enrich_prospect_waterfall(svc: Services, domain: str, target_titles: list[str] | None = None,
                                    use_clay: bool = True) -> dict:
    d = svc.deps
    decision = await d.gate.guard_action(ActionContext(action="enrich"), actor="mcp_user", target_type="account",
                                         target_id=domain)
    r = d.redis
    if decision.tier != "auto":
        return {"status": "blocked", "account_id": "", "contacts_found": 0, "cache_hit": False}
    key = f"cache:enrichment:domain:{domain}"
    cached = await r.get(key)
    if cached:
        data = json.loads(cached)
        return {"status": "completed", "account_id": data["account_id"], "contacts_found": data["contacts"],
                "cache_hit": True}
    acct = await d.repo.get_account(domain)
    if acct is None:
        company = await d.apollo.get_company(domain)
        if company is None:
            return {"status": "completed", "account_id": "", "contacts_found": 0, "cache_hit": False,
                    "error": f"company {domain} not found"}
        acct = await d.repo.upsert_account(company)
    people = await d.apollo.search_people(domain, target_titles, limit=5)
    n = 0
    for p in people:
        if p.email:
            await d.repo.upsert_contact(ContactData(**p.model_dump()), acct["id"])
            n += 1
    status = "completed"
    if use_clay:
        await d.clay.push_row({"domain": domain})
        status = "completed" if d.clay.mock else "queued"
    await r.set(key, json.dumps({"account_id": acct["id"], "contacts": n}), ex=ENRICH_TTL)
    return {"status": status, "account_id": acct["id"], "contacts_found": n, "cache_hit": False}


async def launch_sdr_campaign(svc: Services, icp_prompt: str, campaign_id: str, max_accounts: int = 10) -> dict:
    await svc.deps.gate.guard_action(ActionContext(action="crm_write"), actor="mcp_user", target_type="campaign",
                                     target_id=campaign_id, after={"icp": icp_prompt})
    st = await svc.runner.start(icp_prompt, campaign_id, max_accounts)
    return {"thread_id": st["thread_id"], "campaign_id": campaign_id, "accounts_queued": st["accounts_queued"],
            "pending_approvals": st["pending_approvals"]}


async def upsert_crm_deal_or_task(svc: Services, domain: str, object_type: str, title: str,
                                  amount_usd: float | None = None, due_date: str | None = None) -> dict:
    d = svc.deps
    if object_type not in ("deal", "task"):
        return {"error": "object_type must be deal|task"}
    decision = await d.gate.guard_action(ActionContext(action="crm_write", record_count=1), actor="mcp_user",
                                         target_type=object_type, target_id=domain, after={"title": title})
    if decision.tier != "auto":
        approval = await d.gate.create_approval(
            action_type="crm_bulk_update", target_id=(await d.repo.get_account(domain) or {"id": domain})["id"],
            payload={"domain": domain, "object_type": object_type, "title": title}, decision=decision,
            title=f"Approve CRM {object_type}", details=title)
        return {"safety_tier": decision.tier, "hubspot_object_id": None, "salesforce_object_id": None,
                "approval_request_id": approval["id"]}
    acct = await d.repo.get_account(domain)
    if acct is None:
        return {"error": f"unknown account {domain}"}
    if object_type == "task":
        hs = await d.hubspot.create_task(title, due_date, acct.get("hubspot_company_id"))
        sf = await d.salesforce.create_task(title, due_date, acct.get("salesforce_account_id"))
    else:
        hs = await d.hubspot.create_deal(title, amount_usd, acct.get("hubspot_company_id"))
        sf = None  # Salesforce Opportunity mapping is account-specific; deals sync to HubSpot only
    return {"safety_tier": decision.tier, "hubspot_object_id": hs, "salesforce_object_id": sf,
            "approval_request_id": None}


async def resolve_approval_request(svc: Services, approval_request_id: str, decision: str,
                                   reviewer_note: str | None = None) -> dict:
    d = svc.deps
    try:
        a = await d.gate.resolve_approval(approval_request_id, decision, "mcp_user", reviewer_note)
    except ApprovalError as e:
        return {"approval_request_id": approval_request_id, "new_status": f"error: {e}", "resumed_thread_id": ""}
    resumed = ""
    if a["action_type"] == "send_email":
        try:
            st = await svc.runner.resume_by_approval(approval_request_id, {"decision": decision,
                                                                           "reviewer": "mcp_user", "note": reviewer_note})
            resumed = st["thread_id"]
        except LookupError:
            pass
    return {"approval_request_id": approval_request_id, "new_status": a["status"], "resumed_thread_id": resumed}


async def purge_or_suppress_prospect(svc: Services, email_or_domain: str, reason: str, confirm_token: str = "") -> dict:
    """GDPR/CAN-SPAM erasure. Destructive: blocked unless the operator-issued confirm_token matches."""
    d = svc.deps
    expected = d.gate.confirm_token("purge", email_or_domain)
    valid = bool(confirm_token) and hmac.compare_digest(expected, confirm_token)
    decision = await d.gate.guard_action(ActionContext(action="purge", confirm_token_valid=valid), actor="mcp_user",
                                         target_type="prospect", target_id=email_or_domain, after={"reason": reason})
    if decision.rule_triggered != "destructive_confirmed":
        # The token goes to the operator's console/audit trail, never back to the (possibly LLM) caller.
        print(f"[gtm-safety] operator confirm_token for purge of {email_or_domain}: {expected}", file=sys.stderr)
        return {"suppressed": False, "contacts_redacted": 0, "drafts_cancelled": 0, "audit_log_id": 0,
                "blocked": True, "message": "Destructive action blocked: a valid operator confirm_token is required."}
    typ = "email" if "@" in email_or_domain else "domain"
    await d.repo.add_suppression(email_or_domain, typ, reason)
    cancelled = await d.repo.cancel_drafts_for(email_or_domain)  # before redaction: needs the real address
    redacted = await d.repo.redact_contacts(email_or_domain)
    log_id = await audit("mcp_user", "purge_prospect", "prospect", email_or_domain, "block",
                         after={"contacts_redacted": redacted, "drafts_cancelled": cancelled, "reason": reason})
    return {"suppressed": True, "contacts_redacted": redacted, "drafts_cancelled": cancelled, "audit_log_id": log_id}
