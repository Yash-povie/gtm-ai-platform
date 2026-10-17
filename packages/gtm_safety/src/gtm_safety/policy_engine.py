"""Deterministic 3-tier HITL classifier: AUTO / APPROVE / BLOCK.

Order of evaluation is BLOCK -> APPROVE -> AUTO, so a hard-stop can never be downgraded.
Safety tier (what we do) is distinct from account tier (tier_1..tier_3, ICP quality).
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from gtm_core.config import Settings, get_settings
from gtm_core.observability import span
from gtm_core.schemas import AccountTier, PolicyDecision
from pydantic import BaseModel, Field

HALLUCINATION_OK = 1.0
BLOCK_BELOW = 0.75
AUTO_AT_OR_ABOVE = 0.85


class EmailContext(BaseModel):
    account_tier: AccountTier
    contact_is_unsubscribed: bool = False
    contact_email_status: str = "verified"
    last_contacted_at: datetime | None = None
    suppression_match: str | None = None  # type of matching suppression entry, if any
    has_required_footer: bool = True
    rate_limit_reason: str | None = None  # from RateLimiter.outbound_block_reason
    qa_hallucination_score: float | None = None  # None => QA missing => BLOCK
    qa_composite_score: float | None = None
    target_id: str = ""


class ActionContext(BaseModel):
    action: Literal["read", "enrich", "log_activity", "crm_write", "crm_bulk_update", "crm_delete", "purge"]
    record_count: int = 1
    confirm_token_valid: bool = False


def _block(rule: str, *reasons: str) -> PolicyDecision:
    return PolicyDecision(tier="block", rule_triggered=rule, reasons=list(reasons))


def classify_email(ctx: EmailContext, settings: Settings | None = None, now: datetime | None = None) -> PolicyDecision:
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    with span("safety.policy_check", target_id=ctx.target_id) as sp:
        decision = _classify_email(ctx, s, now)
        sp.set_attribute("safety_tier", decision.tier)
        sp.set_attribute("rule_triggered", decision.rule_triggered)
        return decision


def _classify_email(ctx: EmailContext, s: Settings, now: datetime) -> PolicyDecision:
    # ---- BLOCK: hard stops --------------------------------------------------------------
    if ctx.contact_is_unsubscribed:
        return _block("unsubscribed", "contact opted out")
    if ctx.contact_email_status == "bounced":
        return _block("bounced", "email previously bounced")
    if ctx.suppression_match:
        return _block("suppressed", f"suppression list match: {ctx.suppression_match}")
    if ctx.qa_hallucination_score is None or ctx.qa_composite_score is None:
        return _block("qa_missing", "outbound QA has not scored this draft")
    if ctx.qa_hallucination_score < HALLUCINATION_OK:
        return _block("hallucination", "fabricated claim detected")
    if ctx.qa_composite_score < BLOCK_BELOW:
        return _block("qa_low", f"composite {ctx.qa_composite_score:.2f} < {BLOCK_BELOW}")
    if not ctx.has_required_footer:
        return _block("missing_footer", "unsubscribe footer or postal address missing")
    if ctx.rate_limit_reason:
        return _block("rate_limit", ctx.rate_limit_reason)
    if ctx.last_contacted_at and now - ctx.last_contacted_at < timedelta(days=s.contact_cooldown_days):
        return _block("cooldown", f"contacted within {s.contact_cooldown_days} days")

    # ---- APPROVE: needs a human --------------------------------------------------------
    reasons: list[str] = []
    rule = ""
    if ctx.account_tier == "tier_1":
        rule, reasons = "tier1_account", ["tier_1 account requires approval"]
    elif s.is_sandbox:
        rule, reasons = "sandbox", ["GTM_ENV=sandbox"]
    elif s.gtm_require_approval_all:
        rule, reasons = "require_approval_all", ["GTM_REQUIRE_APPROVAL_ALL=true"]
    elif not s.gtm_live_send_enabled:
        rule, reasons = "live_send_disabled", ["GTM_LIVE_SEND_ENABLED=false"]
    elif ctx.qa_composite_score < AUTO_AT_OR_ABOVE:
        rule, reasons = "qa_marginal", [f"composite {ctx.qa_composite_score:.2f} < {AUTO_AT_OR_ABOVE}"]
    if rule:
        return PolicyDecision(tier="approve", rule_triggered=rule, reasons=reasons)

    return PolicyDecision(tier="auto", rule_triggered="all_checks_passed", reasons=[])


def classify_action(ctx: ActionContext) -> PolicyDecision:
    """CRM/MCP mutations (non-email)."""
    if ctx.action in ("read", "enrich", "log_activity"):
        return PolicyDecision(tier="auto", rule_triggered="read_or_log")
    if ctx.action in ("crm_delete", "purge"):
        if not ctx.confirm_token_valid:
            return _block("destructive_needs_confirmation", "missing or invalid confirm_token")
        return PolicyDecision(tier="approve", rule_triggered="destructive_confirmed", reasons=["destructive action"])
    if ctx.action == "crm_bulk_update" or ctx.record_count > 5:
        return PolicyDecision(tier="approve", rule_triggered="bulk_update", reasons=[f"{ctx.record_count} records"])
    return PolicyDecision(tier="auto", rule_triggered="single_write")


def expiry(now: datetime | None = None, hours: int | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours or get_settings().approval_ttl_hours)


__all__ = ["ActionContext", "EmailContext", "Field", "classify_action", "classify_email", "expiry"]
