"""SQLAlchemy 2.0 ORM declarations (mirrors migrations/versions/0001_initial_schema.py)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1536


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("account_tier IN ('tier_1','tier_2','tier_3')", name="ck_accounts_tier"),
        Index("idx_accounts_tier_score", "account_tier", text("icp_score DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(128))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    annual_revenue_usd: Mapped[int | None] = mapped_column(BigInteger)
    funding_stage: Mapped[str | None] = mapped_column(String(64))
    hq_country: Mapped[str | None] = mapped_column(String(64))
    tech_stack: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    firmographic_raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    icp_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default=text("0.00"))
    account_tier: Mapped[str] = mapped_column(String(16), nullable=False, server_default="tier_3")
    is_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ltv_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    hubspot_company_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    salesforce_account_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    description_embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "email_status IN ('verified','unverified','catch_all','bounced')", name="ck_contacts_email_status"
        ),
        Index("idx_contacts_outreach_eligible", "is_unsubscribed", "email_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    seniority: Mapped[str | None] = mapped_column(String(64))
    department: Mapped[str | None] = mapped_column(String(64))
    linkedin_url: Mapped[str | None] = mapped_column(String(512), unique=True)
    apollo_person_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    hubspot_contact_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    salesforce_contact_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    email_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unverified")
    is_unsubscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrichment_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint(
            "signal_type IN ('job_change','funding_round','hiring_spike','tech_added','tech_removed','champion_moved')",
            name="ck_signals_type",
        ),
        CheckConstraint(
            "source IN ('sales_navigator','clay','apollo','n8n_scraper')", name="ck_signals_source"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signal_strength: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    decayed_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_name", "version_tag"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    prompt_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, server_default=text("0.30"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _created()


class EmailDraft(Base):
    __tablename__ = "email_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','qa_failed','pending_approval','approved','rejected','sent','replied','bounced')",
            name="ck_email_drafts_status",
        ),
        CheckConstraint("safety_tier IN ('auto','approve','block')", name="ck_email_drafts_tier"),
        CheckConstraint(
            "reply_sentiment IN ('positive','neutral','negative','ooo','unsubscribe')",
            name="ck_email_drafts_sentiment",
        ),
        Index("idx_email_drafts_status", "status", text("created_at DESC")),
        Index("idx_email_drafts_prompt_perf", "prompt_version_id", "status", "reply_sentiment"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    trigger_signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    prompt_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    facts_used: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="draft")
    safety_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_sentiment: Mapped[str | None] = mapped_column(String(32))
    meeting_booked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created()


class QAEvaluation(Base):
    __tablename__ = "qa_evaluations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    judge_model: Mapped[str] = mapped_column(String(64), nullable=False)
    hallucination_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    tone_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    relevance_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    composite_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unsupported_claims: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    critique_reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    rewrite_suggestion: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('send_email','crm_bulk_update','crm_delete','sequence_enroll')",
            name="ck_approvals_action",
        ),
        CheckConstraint("status IN ('pending','approved','rejected','expired')", name="ck_approvals_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    safety_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    reviewer_id: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slack_message_ts: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    safety_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created()


class SuppressionEntry(Base):
    __tablename__ = "suppression_list"
    __table_args__ = (
        CheckConstraint(
            "type IN ('email','domain','competitor','current_customer')", name="ck_suppression_type"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_or_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()
