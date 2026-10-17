"""Shared Pydantic domain models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AccountTier = Literal["tier_1", "tier_2", "tier_3"]
SafetyTier = Literal["auto", "approve", "block"]
SignalType = Literal[
    "job_change", "funding_round", "hiring_spike", "tech_added", "tech_removed", "champion_moved"
]
SignalSource = Literal["sales_navigator", "clay", "apollo", "n8n_scraper"]
EmailStatus = Literal["verified", "unverified", "catch_all", "bounced"]


class AccountData(BaseModel):
    domain: str
    name: str
    industry: str | None = None
    employee_count: int | None = None
    annual_revenue_usd: int | None = None
    funding_stage: str | None = None
    hq_country: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    description: str | None = None
    investors: list[str] = Field(default_factory=list)
    funding_amount_usd: int | None = None
    is_customer: bool = False
    ltv_usd: float | None = None
    icp_score: float = 0.0
    account_tier: AccountTier = "tier_3"


class ContactData(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    department: str | None = None
    linkedin_url: str | None = None
    apollo_person_id: str | None = None
    email_status: EmailStatus = "unverified"
    company_domain: str | None = None


class SignalEvent(BaseModel):
    signal_type: SignalType
    source: SignalSource
    domain: str
    contact_email: str | None = None
    occurred_at: datetime
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class QAResult(BaseModel):
    """Output of the Outbound QA judge (System 4)."""

    hallucination_score: float = Field(ge=0.0, le=1.0)
    tone_score: float = Field(ge=0.0, le=1.0)
    relevance_score: float = Field(ge=0.0, le=1.0)
    composite_score: float = Field(ge=0.0, le=1.0)
    passed: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    critique: str = ""
    rewrite_suggestion: str | None = None


class EmailDraftData(BaseModel):
    account_domain: str
    contact_email: str
    subject: str
    body_text: str
    facts_used: list[str] = Field(default_factory=list)
    campaign_id: str = "default"
    prompt_version_tag: str = "v1"


class PolicyDecision(BaseModel):
    tier: SafetyTier
    rule_triggered: str
    reasons: list[str] = Field(default_factory=list)

    @property
    def allowed_without_human(self) -> bool:
        return self.tier == "auto"
