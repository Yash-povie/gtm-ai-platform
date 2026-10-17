"""Read-only MCP tools: CRM search, account 360, lookalikes, signals, draft evaluation."""

from datetime import UTC, datetime, timedelta

from sys1_autonomous_sdr.prompts import DEFAULT_VALUE_PROP, build_source_facts
from sys2_signal_engine.scorer import decay
from sys4_outbound_qa.rubrics import JudgeInput
from sys5_lookalike_finder.similarity import SeedNotFoundError, rank

from sys3_command_center_mcp.services import Services


def _acct_view(a: dict) -> dict:
    return {"id": a["id"], "domain": a["domain"], "name": a["name"], "icp_score": float(a["icp_score"]),
            "tier": a["account_tier"], "hubspot_id": a.get("hubspot_company_id") or "",
            "sfdc_id": a.get("salesforce_account_id") or ""}


async def search_crm_accounts(svc: Services, query: str | None = None, tier: str | None = None,
                              min_icp_score: float | None = None, limit: int = 10) -> dict:
    rows = await svc.repo.search_accounts(query=query, tier=tier, min_icp=min_icp_score, limit=min(limit, 100))
    return {"accounts": [_acct_view(a) for a in rows]}


async def get_account_360(svc: Services, domain: str) -> dict:
    acct = await svc.repo.get_account(domain)
    if acct is None:
        return {"error": f"unknown domain {domain}"}
    contacts = await svc.repo.list_contacts(acct["id"])
    signals = await svc.repo.list_signals(account_id=acct["id"])
    drafts = [d for d in svc.repo.drafts.values() if d["account_id"] == acct["id"]] if hasattr(svc.repo, "drafts") else []
    return {
        "account": {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in acct.items()
                    if k not in ("firmographic_raw",)},
        "contacts": [{k: c.get(k) for k in ("email", "first_name", "last_name", "title", "seniority", "email_status",
                                              "is_unsubscribed")} for c in contacts],
        "signals": [{"type": s["signal_type"], "source": s["source"], "occurred_at": s["occurred_at"].isoformat(),
                     "decayed_score": float(s["decayed_score"])} for s in signals[:20]],
        "recent_drafts": [{"draft_id": d["id"], "subject": d["subject"], "status": d["status"],
                           "safety_tier": d["safety_tier"]} for d in drafts[-5:]],
    }


async def find_lookalike_accounts(svc: Services, seed_domain: str, top_k: int = 10, min_shared_tech: int = 2) -> dict:
    profiles = await svc.store.all_profiles()
    seed = profiles.get(seed_domain)
    if seed is None:
        # allow friendly names ("stripe") by matching name/domain substrings
        seed = next((p for p in profiles.values() if seed_domain.lower() in p.domain or seed_domain.lower() in p.name.lower()), None)
        if seed is None:
            raise SeedNotFoundError(seed_domain)
    results = rank(seed, profiles.values(), top_k=top_k, min_shared_tech=min_shared_tech)
    return {"seed": seed.domain, "lookalikes": [
        {"domain": r.domain, "similarity_score": r.similarity_score, "shared_tech": r.shared_tech,
         "shared_industry": r.shared_industry} for r in results]}


async def query_active_signals(svc: Services, signal_types: list[str] | None = None, min_score: float = 70.0,
                               days_back: int = 7) -> dict:
    now = datetime.now(UTC)
    rows = await svc.repo.list_signals(types=signal_types, since=now - timedelta(days=days_back))
    out = []
    for s in rows:
        score = decay(float(s["signal_strength"]), s["occurred_at"], now)  # decay to *now*, not ingestion time
        if score < min_score:
            continue
        acct = await svc.repo.get_account_by_id(s["account_id"])
        out.append({"signal_id": s["id"], "domain": acct["domain"], "type": s["signal_type"],
                    "decayed_score": score, "occurred_at": s["occurred_at"].isoformat()})
    out.sort(key=lambda r: -r["decayed_score"])
    return {"signals": out}


async def evaluate_email_draft(svc: Services, draft_id: str | None = None, subject: str | None = None,
                               body: str | None = None, facts: list[str] | None = None) -> dict:
    if draft_id:
        draft = await svc.repo.get_draft(draft_id)
        if draft is None:
            return {"error": f"unknown draft {draft_id}"}
        acct = await svc.repo.get_account_by_id(draft["account_id"])
        contact = next(c for c in await svc.repo.list_contacts(draft["account_id"]) if c["id"] == draft["contact_id"])
        signals = [s for s in await svc.repo.list_signals(account_id=draft["account_id"])][:2]
        inp = JudgeInput(source_facts=build_source_facts(acct, contact, signals), value_prop=DEFAULT_VALUE_PROP,
                         subject=draft["subject"], body=draft["body_text"], draft_id=draft_id)
    else:
        if not body:
            return {"error": "provide draft_id or body"}
        inp = JudgeInput(source_facts={"facts": facts or []}, value_prop=DEFAULT_VALUE_PROP,
                         subject=subject or "", body=body)
    r = await svc.deps.judge.evaluate(inp)
    return {"passed": r.passed, "hallucination_score": r.hallucination_score, "tone_score": r.tone_score,
            "relevance_score": r.relevance_score, "unsupported_claims": r.unsupported_claims, "critique": r.critique}
