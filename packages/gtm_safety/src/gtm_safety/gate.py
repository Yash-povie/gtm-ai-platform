"""SafetyGate: ties policy engine + rate limiter + repo + audit + approvals + Slack together."""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from gtm_core.config import Settings, get_settings
from gtm_core.repo import Repo
from gtm_core.schemas import PolicyDecision, QAResult
from gtm_integrations.slack import SlackClient

from gtm_safety import compliance
from gtm_safety.audit import audit
from gtm_safety.policy_engine import ActionContext, EmailContext, classify_action, classify_email, expiry
from gtm_safety.rate_limiter import RateLimiter


def destructive_confirm_token(secret: str, action: str, target: str) -> str:
    """Operator-issued token. It is never returned by a tool result; it is logged for the operator."""
    return hmac.new(secret.encode(), f"{action}|{target.lower()}".encode(), hashlib.sha256).hexdigest()[:12]


class ApprovalError(Exception):
    pass


class SafetyGate:
    def __init__(self, repo: Repo, limiter: RateLimiter, slack: SlackClient, settings: Settings | None = None):
        self.repo, self.limiter, self.slack = repo, limiter, slack
        self.s = settings or get_settings()

    def confirm_token(self, action: str, target: str) -> str:
        return destructive_confirm_token(self.s.unsubscribe_jwt_secret, action, target)

    def finalize_payload(self, email: str, body_text: str, body_html: str | None = None) -> tuple[str, str, dict]:
        """Append the mandatory CAN-SPAM footer + List-Unsubscribe headers (idempotent)."""
        if compliance.has_required_footer(body_text):
            return body_text, body_html or "", compliance.unsubscribe_headers(email)
        return compliance.add_footer(email, body_text, body_html)

    async def classify_email_draft(self, *, account: dict, contact: dict, final_text: str, qa: QAResult | None,
                                   target_id: str) -> PolicyDecision:
        entries = await self.repo.suppression_entries()
        sup = compliance.suppression_match(contact["email"], entries) or compliance.suppression_match(
            f"x@{account['domain']}", entries)
        last = contact.get("last_contacted_at")
        ctx = EmailContext(
            account_tier=account["account_tier"],
            contact_is_unsubscribed=bool(contact.get("is_unsubscribed")),
            contact_email_status=contact.get("email_status", "verified"),
            last_contacted_at=last if isinstance(last, datetime) else None,
            suppression_match=sup,
            has_required_footer=compliance.has_required_footer(final_text),
            rate_limit_reason=await self.limiter.outbound_block_reason(account["domain"]),
            qa_hallucination_score=qa.hallucination_score if qa else None,
            qa_composite_score=qa.composite_score if qa else None,
            target_id=target_id,
        )
        decision = classify_email(ctx, self.s)
        await audit("safety_gate", "classify_email", "email_draft", target_id, decision.tier,
                    after={"rule": decision.rule_triggered, "reasons": decision.reasons})
        return decision

    async def create_approval(self, *, action_type: str, target_id: str, payload: dict[str, Any],
                              decision: PolicyDecision, title: str, details: str) -> dict:
        approval = await self.repo.create_approval(
            action_type=action_type, target_entity_id=target_id, payload_snapshot=payload,
            safety_tier=decision.tier, reason="; ".join(decision.reasons) or decision.rule_triggered,
            expires_at=expiry(hours=self.s.approval_ttl_hours))
        ts = await self.slack.post_approval(str(approval["id"]), title, details, approval["reason"])
        approval = await self.repo.update_approval(approval["id"], slack_message_ts=ts)
        await audit("safety_gate", "approval_requested", action_type, target_id, "approve",
                    after={"approval_id": str(approval["id"])})
        return approval

    async def resolve_approval(self, approval_id: str, decision: str, reviewer_id: str,
                               note: str | None = None) -> dict:
        if decision not in ("approved", "rejected"):
            raise ApprovalError("decision must be approved|rejected")
        a = await self.repo.get_approval(approval_id)
        if a is None:
            raise ApprovalError("unknown approval request")
        if a["status"] != "pending":
            raise ApprovalError(f"already {a['status']}")
        exp = a["expires_at"]
        if exp is not None and exp < datetime.now(UTC):
            await self.repo.update_approval(approval_id, status="expired")
            raise ApprovalError("approval request expired")
        a = await self.repo.update_approval(approval_id, status=decision, reviewer_id=reviewer_id,
                                            reviewed_at=datetime.now(UTC))
        await audit(reviewer_id, f"approval_{decision}", a["action_type"], str(a["target_entity_id"]), "approve",
                    before={"status": "pending"}, after={"status": decision, "note": note})
        return a

    async def expire_stale(self) -> int:
        n = 0
        for a in await self.repo.pending_approvals():
            if a["expires_at"] < datetime.now(UTC):
                await self.repo.update_approval(a["id"], status="expired")
                n += 1
        return n

    async def guard_action(self, ctx: ActionContext, *, actor: str, target_type: str, target_id: str,
                           before: dict | None = None, after: dict | None = None) -> PolicyDecision:
        decision = classify_action(ctx)
        await audit(actor, ctx.action, target_type, target_id, decision.tier, before=before,
                    after={**(after or {}), "rule": decision.rule_triggered})
        return decision
