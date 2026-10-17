"""SDR graph state + dependency container + email sender abstraction."""

import operator
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict

from gtm_core.config import Settings
from gtm_core.repo import Repo


class AccountCandidate(TypedDict):
    account_id: str
    domain: str
    name: str
    icp_score: float
    account_tier: Literal["tier_1", "tier_2", "tier_3"]
    rationale: str


class DraftBundle(TypedDict, total=False):
    draft_id: str
    account_id: str
    contact_id: str
    contact_email: str
    subject: str
    body_text: str
    facts_used: list[str]
    qa_passed: bool
    qa_score: float
    qa_critique: str | None
    attempt_count: int
    safety_tier: Literal["auto", "approve", "block"]
    approval_status: Literal["pending", "approved", "rejected", "auto_approved"]
    # extensions
    account_domain: str
    qa_hallucination: float
    approval_id: str | None
    block_rule: str | None
    sent: bool
    trigger_signal_id: str | None


class SDRGraphState(TypedDict, total=False):
    campaign_id: str
    raw_icp_prompt: str
    parsed_icp_filters: dict
    candidate_accounts: list[AccountCandidate]
    current_draft: DraftBundle | None
    completed_drafts: Annotated[list[DraftBundle], operator.add]
    errors: Annotated[list[str], operator.add]
    # extensions
    value_prop: str
    max_accounts: int
    queue_index: int
    pick_status: Literal["ok", "skip", "done"]
    preselected: bool
    trigger_signal_id: str | None


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, text: str, html: str, headers: dict[str, str]) -> str: ...


class SandboxSender:
    """Records sends instead of delivering. Used whenever GTM_ENV=sandbox or live send is disabled."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, to, subject, text, html, headers) -> str:
        mid = f"sandbox-{uuid.uuid4().hex[:12]}"
        self.sent.append({"id": mid, "to": to, "subject": subject, "text": text, "html": html, "headers": headers})
        return mid


class UnconfiguredLiveSender:
    """Live mode needs a real provider (SES/SendGrid). Fail loudly instead of silently dropping mail."""

    async def send(self, *, to, subject, text, html, headers) -> str:
        raise RuntimeError("GTM_LIVE_SEND_ENABLED=true but no email provider is configured")


@dataclass
class SdrDeps:
    repo: Repo
    apollo: Any
    clay: Any
    hubspot: Any
    salesforce: Any
    gate: Any  # gtm_safety.gate.SafetyGate
    judge: Any  # sys4 QAJudge
    drafter: Any
    icp_parser: Any
    sender: EmailSender
    settings: Settings

    @property
    def redis(self):
        return self.gate.limiter.r
