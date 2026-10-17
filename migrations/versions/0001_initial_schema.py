"""initial schema

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
    'CREATE EXTENSION IF NOT EXISTS "vector"',
    """CREATE TABLE accounts (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        domain VARCHAR(255) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL,
        industry VARCHAR(128),
        employee_count INT,
        annual_revenue_usd BIGINT,
        funding_stage VARCHAR(64),
        hq_country VARCHAR(64),
        tech_stack JSONB NOT NULL DEFAULT '[]'::jsonb,
        firmographic_raw JSONB NOT NULL DEFAULT '{}'::jsonb,
        icp_score NUMERIC(5,2) DEFAULT 0.00,
        account_tier VARCHAR(16) NOT NULL DEFAULT 'tier_3'
            CONSTRAINT ck_accounts_tier CHECK (account_tier IN ('tier_1','tier_2','tier_3')),
        is_customer BOOLEAN NOT NULL DEFAULT FALSE,
        ltv_usd NUMERIC(12,2),
        hubspot_company_id VARCHAR(64) UNIQUE,
        salesforce_account_id VARCHAR(64) UNIQUE,
        description_embedding vector(1536),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX idx_accounts_tier_score ON accounts(account_tier, icp_score DESC)",
    "CREATE INDEX idx_accounts_tech_stack_gin ON accounts USING GIN (tech_stack)",
    "CREATE INDEX idx_accounts_embedding ON accounts USING hnsw (description_embedding vector_cosine_ops)",
    """CREATE TABLE contacts (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        email VARCHAR(255) NOT NULL UNIQUE,
        first_name VARCHAR(128),
        last_name VARCHAR(128),
        title VARCHAR(255),
        seniority VARCHAR(64),
        department VARCHAR(64),
        linkedin_url VARCHAR(512) UNIQUE,
        apollo_person_id VARCHAR(64) UNIQUE,
        hubspot_contact_id VARCHAR(64) UNIQUE,
        salesforce_contact_id VARCHAR(64) UNIQUE,
        email_status VARCHAR(32) NOT NULL DEFAULT 'unverified'
            CONSTRAINT ck_contacts_email_status CHECK (email_status IN ('verified','unverified','catch_all','bounced')),
        is_unsubscribed BOOLEAN NOT NULL DEFAULT FALSE,
        unsubscribed_at TIMESTAMPTZ,
        enrichment_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_contacted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX ix_contacts_account_id ON contacts(account_id)",
    "CREATE INDEX idx_contacts_outreach_eligible ON contacts(is_unsubscribed, email_status)",
    """CREATE TABLE signals (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
        signal_type VARCHAR(64) NOT NULL
            CONSTRAINT ck_signals_type CHECK (signal_type IN ('job_change','funding_round','hiring_spike','tech_added','tech_removed','champion_moved')),
        source VARCHAR(64) NOT NULL
            CONSTRAINT ck_signals_source CHECK (source IN ('sales_navigator','clay','apollo','n8n_scraper')),
        raw_payload JSONB NOT NULL,
        signal_strength NUMERIC(5,2) NOT NULL,
        decayed_score NUMERIC(5,2) NOT NULL,
        idempotency_key VARCHAR(255) NOT NULL UNIQUE,
        occurred_at TIMESTAMPTZ NOT NULL,
        processed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX idx_signals_account_unprocessed ON signals(account_id, occurred_at DESC) WHERE processed_at IS NULL",
    """CREATE TABLE prompt_versions (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        prompt_name VARCHAR(128) NOT NULL,
        version_tag VARCHAR(64) NOT NULL,
        system_prompt TEXT NOT NULL,
        user_template TEXT NOT NULL,
        model_name VARCHAR(64) NOT NULL,
        temperature NUMERIC(3,2) NOT NULL DEFAULT 0.30,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(prompt_name, version_tag)
    )""",
    """CREATE TABLE email_drafts (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        account_id UUID NOT NULL REFERENCES accounts(id),
        contact_id UUID NOT NULL REFERENCES contacts(id),
        trigger_signal_id UUID REFERENCES signals(id),
        prompt_version_id UUID NOT NULL REFERENCES prompt_versions(id),
        campaign_id VARCHAR(128) NOT NULL,
        subject VARCHAR(512) NOT NULL,
        body_text TEXT NOT NULL,
        body_html TEXT NOT NULL,
        facts_used JSONB NOT NULL DEFAULT '[]'::jsonb,
        status VARCHAR(32) NOT NULL DEFAULT 'draft'
            CONSTRAINT ck_email_drafts_status CHECK (status IN ('draft','qa_failed','pending_approval','approved','rejected','sent','replied','bounced')),
        safety_tier VARCHAR(16) NOT NULL
            CONSTRAINT ck_email_drafts_tier CHECK (safety_tier IN ('auto','approve','block')),
        sent_at TIMESTAMPTZ,
        replied_at TIMESTAMPTZ,
        reply_sentiment VARCHAR(32)
            CONSTRAINT ck_email_drafts_sentiment CHECK (reply_sentiment IN ('positive','neutral','negative','ooo','unsubscribe')),
        meeting_booked BOOLEAN NOT NULL DEFAULT FALSE,
        langgraph_thread_id VARCHAR(128),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX idx_email_drafts_status ON email_drafts(status, created_at DESC)",
    "CREATE INDEX idx_email_drafts_prompt_perf ON email_drafts(prompt_version_id, status, reply_sentiment)",
    """CREATE TABLE qa_evaluations (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        email_draft_id UUID NOT NULL REFERENCES email_drafts(id) ON DELETE CASCADE,
        judge_model VARCHAR(64) NOT NULL,
        hallucination_score NUMERIC(4,3) NOT NULL,
        tone_score NUMERIC(4,3) NOT NULL,
        relevance_score NUMERIC(4,3) NOT NULL,
        composite_score NUMERIC(4,3) NOT NULL,
        passed BOOLEAN NOT NULL,
        unsupported_claims JSONB NOT NULL DEFAULT '[]'::jsonb,
        critique_reasoning TEXT NOT NULL,
        rewrite_suggestion TEXT,
        latency_ms INT NOT NULL,
        trace_id VARCHAR(128) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX ix_qa_evaluations_email_draft_id ON qa_evaluations(email_draft_id)",
    """CREATE TABLE approval_requests (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        action_type VARCHAR(64) NOT NULL
            CONSTRAINT ck_approvals_action CHECK (action_type IN ('send_email','crm_bulk_update','crm_delete','sequence_enroll')),
        target_entity_id UUID NOT NULL,
        payload_snapshot JSONB NOT NULL,
        safety_tier VARCHAR(16) NOT NULL,
        reason VARCHAR(512) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_approvals_status CHECK (status IN ('pending','approved','rejected','expired')),
        reviewer_id VARCHAR(128),
        reviewed_at TIMESTAMPTZ,
        slack_message_ts VARCHAR(64),
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX idx_approvals_pending ON approval_requests(status, expires_at) WHERE status = 'pending'",
    """CREATE TABLE audit_logs (
        id BIGSERIAL PRIMARY KEY,
        actor VARCHAR(128) NOT NULL,
        action VARCHAR(128) NOT NULL,
        target_type VARCHAR(64) NOT NULL,
        target_id VARCHAR(128) NOT NULL,
        safety_tier VARCHAR(16) NOT NULL,
        before_state JSONB,
        after_state JSONB,
        trace_id VARCHAR(128),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    # Immutability: audit rows can be inserted but never updated or deleted.
    """CREATE FUNCTION audit_logs_immutable() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END; $$ LANGUAGE plpgsql""",
    """CREATE TRIGGER audit_logs_no_mutate BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable()""",
    """CREATE TABLE suppression_list (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        email_or_domain VARCHAR(255) NOT NULL UNIQUE,
        type VARCHAR(16) NOT NULL
            CONSTRAINT ck_suppression_type CHECK (type IN ('email','domain','competitor','current_customer')),
        reason VARCHAR(255) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]

TABLES = [
    "suppression_list",
    "audit_logs",
    "approval_requests",
    "qa_evaluations",
    "email_drafts",
    "prompt_versions",
    "signals",
    "contacts",
    "accounts",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_immutable()")
