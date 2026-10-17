"""Data-access layer. `MemoryRepo` (sandbox/tests, no Docker) and `SqlRepo` (Postgres) share one interface.

Records are plain dicts with string UUIDs so agents and tools stay storage-agnostic.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from gtm_core.schemas import AccountData, ContactData, QAResult


def _now() -> datetime:
    return datetime.now(UTC)


def _id() -> str:
    return str(uuid.uuid4())


class Repo(Protocol):
    async def upsert_account(self, a: AccountData) -> dict: ...
    async def get_account(self, domain: str) -> dict | None: ...
    async def get_account_by_id(self, account_id: str) -> dict | None: ...
    async def update_account(self, account_id: str, **fields: Any) -> dict: ...
    async def search_accounts(self, query: str | None = None, tier: str | None = None,
                              min_icp: float | None = None, limit: int = 10) -> list[dict]: ...
    async def upsert_contact(self, c: ContactData, account_id: str) -> dict: ...
    async def list_contacts(self, account_id: str) -> list[dict]: ...
    async def get_contact_by_email(self, email: str) -> dict | None: ...
    async def update_contact(self, contact_id: str, **fields: Any) -> dict: ...
    async def add_signal(self, *, account_id: str, contact_id: str | None, signal_type: str, source: str,
                         payload: dict, strength: float, decayed: float, key: str,
                         occurred_at: datetime) -> tuple[dict, bool]: ...
    async def list_signals(self, account_id: str | None = None, types: list[str] | None = None,
                           min_score: float = 0.0, since: datetime | None = None,
                           unprocessed_only: bool = False) -> list[dict]: ...
    async def update_signal(self, signal_id: str, **fields: Any) -> dict: ...
    async def ensure_prompt_version(self, name: str, tag: str, system: str, user_template: str, model: str) -> dict: ...
    async def create_draft(self, **fields: Any) -> dict: ...
    async def update_draft(self, draft_id: str, **fields: Any) -> dict: ...
    async def get_draft(self, draft_id: str) -> dict | None: ...
    async def add_qa_evaluation(self, draft_id: str, result: QAResult, model: str, latency_ms: int,
                                trace_id: str) -> dict: ...
    async def create_approval(self, **fields: Any) -> dict: ...
    async def get_approval(self, approval_id: str) -> dict | None: ...
    async def update_approval(self, approval_id: str, **fields: Any) -> dict: ...
    async def pending_approvals(self) -> list[dict]: ...
    async def suppression_entries(self) -> list[tuple[str, str]]: ...
    async def add_suppression(self, value: str, typ: str, reason: str) -> None: ...
    async def cancel_drafts_for(self, email_or_domain: str) -> int: ...
    async def redact_contacts(self, email_or_domain: str) -> int: ...


_ACCOUNT_DEFAULTS = {"icp_score": 0.0, "account_tier": "tier_3", "hubspot_company_id": None,
                     "salesforce_account_id": None}
_OPEN_DRAFT = ("draft", "pending_approval", "approved", "qa_failed")


class MemoryRepo:
    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.signals: dict[str, dict] = {}
        self.prompt_versions: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.qa: list[dict] = []
        self.approvals: dict[str, dict] = {}
        self.suppression: dict[str, tuple[str, str]] = {}

    # accounts
    async def upsert_account(self, a: AccountData) -> dict:
        existing = await self.get_account(a.domain)
        rec = existing or {"id": _id(), **_ACCOUNT_DEFAULTS, "created_at": _now()}
        rec.update(
            domain=a.domain, name=a.name, industry=a.industry, employee_count=a.employee_count,
            funding_stage=a.funding_stage, hq_country=a.hq_country, tech_stack=list(a.tech_stack),
            annual_revenue_usd=a.annual_revenue_usd, is_customer=a.is_customer, ltv_usd=a.ltv_usd,
            firmographic_raw={"investors": a.investors, "funding_amount_usd": a.funding_amount_usd,
                              "description": a.description},
        )
        self.accounts[rec["id"]] = rec
        return dict(rec)

    async def get_account(self, domain: str) -> dict | None:
        for r in self.accounts.values():
            if r["domain"] == domain:
                return dict(r)
        return None

    async def get_account_by_id(self, account_id: str) -> dict | None:
        r = self.accounts.get(str(account_id))
        return dict(r) if r else None

    async def update_account(self, account_id: str, **fields: Any) -> dict:
        self.accounts[str(account_id)].update(fields)
        return dict(self.accounts[str(account_id)])

    async def search_accounts(self, query=None, tier=None, min_icp=None, limit=10) -> list[dict]:
        out = []
        for r in self.accounts.values():
            if query and query.lower() not in r["name"].lower() and query.lower() not in r["domain"].lower():
                continue
            if tier and r["account_tier"] != tier:
                continue
            if min_icp is not None and float(r["icp_score"]) < min_icp:
                continue
            out.append(dict(r))
        out.sort(key=lambda r: (-float(r["icp_score"]), r["domain"]))
        return out[:limit]

    # contacts
    async def upsert_contact(self, c: ContactData, account_id: str) -> dict:
        assert c.email
        existing = await self.get_contact_by_email(c.email)
        rec = existing or {"id": _id(), "is_unsubscribed": False, "last_contacted_at": None,
                           "hubspot_contact_id": None, "salesforce_contact_id": None, "created_at": _now()}
        rec.update(account_id=str(account_id), email=c.email, first_name=c.first_name, last_name=c.last_name,
                   title=c.title, seniority=c.seniority, department=c.department, linkedin_url=c.linkedin_url,
                   apollo_person_id=c.apollo_person_id)
        if not existing:
            rec["email_status"] = c.email_status
        self.contacts[rec["id"]] = rec
        return dict(rec)

    async def list_contacts(self, account_id: str) -> list[dict]:
        return [dict(c) for c in self.contacts.values() if c["account_id"] == str(account_id)]

    async def get_contact_by_email(self, email: str) -> dict | None:
        for c in self.contacts.values():
            if c["email"].lower() == email.lower():
                return dict(c)
        return None

    async def update_contact(self, contact_id: str, **fields: Any) -> dict:
        self.contacts[str(contact_id)].update(fields)
        return dict(self.contacts[str(contact_id)])

    # signals
    async def add_signal(self, *, account_id, contact_id, signal_type, source, payload, strength, decayed, key,
                         occurred_at) -> tuple[dict, bool]:
        for s in self.signals.values():
            if s["idempotency_key"] == key:
                return dict(s), False
        rec = {"id": _id(), "account_id": str(account_id), "contact_id": contact_id, "signal_type": signal_type,
               "source": source, "raw_payload": payload, "signal_strength": strength, "decayed_score": decayed,
               "idempotency_key": key, "occurred_at": occurred_at, "processed_at": None, "created_at": _now()}
        self.signals[rec["id"]] = rec
        return dict(rec), True

    async def list_signals(self, account_id=None, types=None, min_score=0.0, since=None,
                           unprocessed_only=False) -> list[dict]:
        out = []
        for s in self.signals.values():
            if account_id and s["account_id"] != str(account_id):
                continue
            if types and s["signal_type"] not in types:
                continue
            if float(s["decayed_score"]) < min_score:
                continue
            if since and s["occurred_at"] < since:
                continue
            if unprocessed_only and s["processed_at"] is not None:
                continue
            out.append(dict(s))
        out.sort(key=lambda s: s["occurred_at"], reverse=True)
        return out

    async def update_signal(self, signal_id: str, **fields: Any) -> dict:
        self.signals[str(signal_id)].update(fields)
        return dict(self.signals[str(signal_id)])

    # prompts
    async def ensure_prompt_version(self, name, tag, system, user_template, model) -> dict:
        for p in self.prompt_versions.values():
            if p["prompt_name"] == name and p["version_tag"] == tag:
                return dict(p)
        rec = {"id": _id(), "prompt_name": name, "version_tag": tag, "system_prompt": system,
               "user_template": user_template, "model_name": model, "is_active": True}
        self.prompt_versions[rec["id"]] = rec
        return dict(rec)

    # drafts
    async def create_draft(self, **fields: Any) -> dict:
        rec = {"id": _id(), "status": "draft", "facts_used": [], "meeting_booked": False, "sent_at": None,
               "replied_at": None, "reply_sentiment": None, "trigger_signal_id": None,
               "langgraph_thread_id": None, "body_html": "", "created_at": _now(), **fields}
        self.drafts[rec["id"]] = rec
        return dict(rec)

    async def update_draft(self, draft_id: str, **fields: Any) -> dict:
        self.drafts[str(draft_id)].update(fields)
        return dict(self.drafts[str(draft_id)])

    async def get_draft(self, draft_id: str) -> dict | None:
        r = self.drafts.get(str(draft_id))
        return dict(r) if r else None

    async def add_qa_evaluation(self, draft_id, result: QAResult, model, latency_ms, trace_id) -> dict:
        rec = {"id": _id(), "email_draft_id": str(draft_id), "judge_model": model, "latency_ms": latency_ms,
               "trace_id": trace_id, **result.model_dump()}
        self.qa.append(rec)
        return rec

    # approvals
    async def create_approval(self, **fields: Any) -> dict:
        rec = {"id": _id(), "status": "pending", "reviewer_id": None, "reviewed_at": None,
               "slack_message_ts": None, "created_at": _now(), **fields}
        self.approvals[rec["id"]] = rec
        return dict(rec)

    async def get_approval(self, approval_id: str) -> dict | None:
        r = self.approvals.get(str(approval_id))
        return dict(r) if r else None

    async def update_approval(self, approval_id: str, **fields: Any) -> dict:
        self.approvals[str(approval_id)].update(fields)
        return dict(self.approvals[str(approval_id)])

    async def pending_approvals(self) -> list[dict]:
        return [dict(a) for a in self.approvals.values() if a["status"] == "pending"]

    # suppression / compliance
    async def suppression_entries(self) -> list[tuple[str, str]]:
        return list(self.suppression.values())

    async def add_suppression(self, value: str, typ: str, reason: str) -> None:
        self.suppression[value.lower()] = (value.lower(), typ)

    def _matches(self, email: str, target: str) -> bool:
        t = target.lower()
        return email.lower() == t or email.lower().endswith("@" + t)

    async def cancel_drafts_for(self, email_or_domain: str) -> int:
        n = 0
        for d in self.drafts.values():
            c = self.contacts.get(str(d.get("contact_id")))
            if c and self._matches(c["email"], email_or_domain) and d["status"] in _OPEN_DRAFT:
                d["status"] = "rejected"
                n += 1
        return n

    async def redact_contacts(self, email_or_domain: str) -> int:
        n = 0
        for c in self.contacts.values():
            if self._matches(c["email"], email_or_domain):
                c.update(first_name=None, last_name=None, title=None, linkedin_url=None, is_unsubscribed=True,
                         email=f"redacted-{c['id']}@redacted.invalid")
                n += 1
        return n


class SqlRepo:
    """Postgres implementation (SQLAlchemy 2.0 async). Exercised by the docker-backed integration tests."""

    @staticmethod
    def _d(obj) -> dict:
        d = {c.key: getattr(obj, c.key) for c in obj.__mapper__.column_attrs}
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in d.items()}

    async def upsert_account(self, a: AccountData) -> dict:
        from sqlalchemy.dialects.postgresql import insert

        from gtm_core.db import session_scope
        from gtm_core.embeddings import embed_text
        from gtm_core.models import Account

        vals = dict(domain=a.domain, name=a.name, industry=a.industry, employee_count=a.employee_count,
                    annual_revenue_usd=a.annual_revenue_usd, funding_stage=a.funding_stage, hq_country=a.hq_country,
                    tech_stack=list(a.tech_stack), is_customer=a.is_customer, ltv_usd=a.ltv_usd,
                    firmographic_raw={"investors": a.investors, "funding_amount_usd": a.funding_amount_usd,
                                      "description": a.description},
                    description_embedding=embed_text(a.description or a.name))
        async with session_scope() as s:
            stmt = insert(Account).values(**vals).on_conflict_do_update(
                index_elements=["domain"], set_={k: v for k, v in vals.items() if k != "domain"}).returning(Account)
            row = (await s.execute(stmt)).scalar_one()
            return self._d(row)

    async def _one(self, model, **where) -> dict | None:
        from sqlalchemy import select

        from gtm_core.db import session_scope

        async with session_scope() as s:
            row = (await s.execute(select(model).filter_by(**where))).scalars().first()
            return self._d(row) if row else None

    async def get_account(self, domain: str) -> dict | None:
        from gtm_core.models import Account

        return await self._one(Account, domain=domain)

    async def get_account_by_id(self, account_id: str) -> dict | None:
        from gtm_core.models import Account

        return await self._one(Account, id=uuid.UUID(str(account_id)))

    async def _update(self, model, rid: str, **fields: Any) -> dict:
        from gtm_core.db import session_scope

        async with session_scope() as s:
            row = await s.get(model, uuid.UUID(str(rid)))
            for k, v in fields.items():
                setattr(row, k, v)
            await s.flush()
            return self._d(row)

    async def update_account(self, account_id: str, **fields: Any) -> dict:
        from gtm_core.models import Account

        return await self._update(Account, account_id, **fields)

    async def search_accounts(self, query=None, tier=None, min_icp=None, limit=10) -> list[dict]:
        from sqlalchemy import or_, select

        from gtm_core.db import session_scope
        from gtm_core.models import Account

        q = select(Account)
        if query:
            q = q.where(or_(Account.name.ilike(f"%{query}%"), Account.domain.ilike(f"%{query}%")))
        if tier:
            q = q.where(Account.account_tier == tier)
        if min_icp is not None:
            q = q.where(Account.icp_score >= min_icp)
        async with session_scope() as s:
            rows = (await s.execute(q.order_by(Account.icp_score.desc(), Account.domain).limit(limit))).scalars().all()
            return [self._d(r) for r in rows]

    async def upsert_contact(self, c: ContactData, account_id: str) -> dict:
        from sqlalchemy.dialects.postgresql import insert

        from gtm_core.db import session_scope
        from gtm_core.models import Contact

        vals = dict(account_id=uuid.UUID(str(account_id)), email=c.email, first_name=c.first_name,
                    last_name=c.last_name, title=c.title, seniority=c.seniority, department=c.department,
                    linkedin_url=c.linkedin_url, apollo_person_id=c.apollo_person_id, email_status=c.email_status)
        update = {k: v for k, v in vals.items() if k not in ("email", "email_status")}
        async with session_scope() as s:
            stmt = insert(Contact).values(**vals).on_conflict_do_update(
                index_elements=["email"], set_=update).returning(Contact)
            return self._d((await s.execute(stmt)).scalar_one())

    async def list_contacts(self, account_id: str) -> list[dict]:
        from sqlalchemy import select

        from gtm_core.db import session_scope
        from gtm_core.models import Contact

        async with session_scope() as s:
            rows = (await s.execute(select(Contact).where(Contact.account_id == uuid.UUID(str(account_id))))).scalars()
            return [self._d(r) for r in rows]

    async def get_contact_by_email(self, email: str) -> dict | None:
        from sqlalchemy import func, select

        from gtm_core.db import session_scope
        from gtm_core.models import Contact

        async with session_scope() as s:
            row = (await s.execute(select(Contact).where(func.lower(Contact.email) == email.lower()))).scalars().first()
            return self._d(row) if row else None

    async def update_contact(self, contact_id: str, **fields: Any) -> dict:
        from gtm_core.models import Contact

        return await self._update(Contact, contact_id, **fields)

    async def add_signal(self, *, account_id, contact_id, signal_type, source, payload, strength, decayed, key,
                         occurred_at) -> tuple[dict, bool]:
        from sqlalchemy.dialects.postgresql import insert

        from gtm_core.db import session_scope
        from gtm_core.models import Signal

        vals = dict(account_id=uuid.UUID(str(account_id)), contact_id=uuid.UUID(contact_id) if contact_id else None,
                    signal_type=signal_type, source=source, raw_payload=payload, signal_strength=strength,
                    decayed_score=decayed, idempotency_key=key, occurred_at=occurred_at)
        async with session_scope() as s:
            row = (await s.execute(insert(Signal).values(**vals).on_conflict_do_nothing(
                index_elements=["idempotency_key"]).returning(Signal))).scalars().first()
            if row is not None:
                return self._d(row), True
        existing = await self._one(Signal, idempotency_key=key)
        assert existing is not None
        return existing, False

    async def list_signals(self, account_id=None, types=None, min_score=0.0, since=None,
                           unprocessed_only=False) -> list[dict]:
        from sqlalchemy import select

        from gtm_core.db import session_scope
        from gtm_core.models import Signal

        q = select(Signal).where(Signal.decayed_score >= min_score)
        if account_id:
            q = q.where(Signal.account_id == uuid.UUID(str(account_id)))
        if types:
            q = q.where(Signal.signal_type.in_(types))
        if since:
            q = q.where(Signal.occurred_at >= since)
        if unprocessed_only:
            q = q.where(Signal.processed_at.is_(None))
        async with session_scope() as s:
            return [self._d(r) for r in (await s.execute(q.order_by(Signal.occurred_at.desc()))).scalars()]

    async def update_signal(self, signal_id: str, **fields: Any) -> dict:
        from gtm_core.models import Signal

        return await self._update(Signal, signal_id, **fields)

    async def ensure_prompt_version(self, name, tag, system, user_template, model) -> dict:
        from sqlalchemy.dialects.postgresql import insert

        from gtm_core.db import session_scope
        from gtm_core.models import PromptVersion

        async with session_scope() as s:
            await s.execute(insert(PromptVersion).values(
                prompt_name=name, version_tag=tag, system_prompt=system, user_template=user_template,
                model_name=model).on_conflict_do_nothing(index_elements=["prompt_name", "version_tag"]))
        rec = await self._one(PromptVersion, prompt_name=name, version_tag=tag)
        assert rec is not None
        return rec

    async def create_draft(self, **fields: Any) -> dict:
        from gtm_core.db import session_scope
        from gtm_core.models import EmailDraft

        for k in ("account_id", "contact_id", "prompt_version_id", "trigger_signal_id"):
            if fields.get(k):
                fields[k] = uuid.UUID(str(fields[k]))
        async with session_scope() as s:
            row = EmailDraft(**fields)
            s.add(row)
            await s.flush()
            return self._d(row)

    async def update_draft(self, draft_id: str, **fields: Any) -> dict:
        from gtm_core.models import EmailDraft

        return await self._update(EmailDraft, draft_id, **fields)

    async def get_draft(self, draft_id: str) -> dict | None:
        from gtm_core.models import EmailDraft

        return await self._one(EmailDraft, id=uuid.UUID(str(draft_id)))

    async def add_qa_evaluation(self, draft_id, result: QAResult, model, latency_ms, trace_id) -> dict:
        from gtm_core.db import session_scope
        from gtm_core.models import QAEvaluation

        async with session_scope() as s:
            row = QAEvaluation(
                email_draft_id=uuid.UUID(str(draft_id)), judge_model=model, hallucination_score=result.hallucination_score,
                tone_score=result.tone_score, relevance_score=result.relevance_score,
                composite_score=result.composite_score, passed=result.passed,
                unsupported_claims=result.unsupported_claims, critique_reasoning=result.critique,
                rewrite_suggestion=result.rewrite_suggestion, latency_ms=latency_ms, trace_id=trace_id)
            s.add(row)
            await s.flush()
            return self._d(row)

    async def create_approval(self, **fields: Any) -> dict:
        from gtm_core.db import session_scope
        from gtm_core.models import ApprovalRequest

        fields["target_entity_id"] = uuid.UUID(str(fields["target_entity_id"]))
        async with session_scope() as s:
            row = ApprovalRequest(**fields)
            s.add(row)
            await s.flush()
            return self._d(row)

    async def get_approval(self, approval_id: str) -> dict | None:
        from gtm_core.models import ApprovalRequest

        return await self._one(ApprovalRequest, id=uuid.UUID(str(approval_id)))

    async def update_approval(self, approval_id: str, **fields: Any) -> dict:
        from gtm_core.models import ApprovalRequest

        return await self._update(ApprovalRequest, approval_id, **fields)

    async def pending_approvals(self) -> list[dict]:
        from sqlalchemy import select

        from gtm_core.db import session_scope
        from gtm_core.models import ApprovalRequest

        async with session_scope() as s:
            rows = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalars()
            return [self._d(r) for r in rows]

    async def suppression_entries(self) -> list[tuple[str, str]]:
        from sqlalchemy import select

        from gtm_core.db import session_scope
        from gtm_core.models import SuppressionEntry

        async with session_scope() as s:
            rows = (await s.execute(select(SuppressionEntry.email_or_domain, SuppressionEntry.type))).all()
            return [(a, b) for a, b in rows]

    async def add_suppression(self, value: str, typ: str, reason: str) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from gtm_core.db import session_scope
        from gtm_core.models import SuppressionEntry

        async with session_scope() as s:
            await s.execute(insert(SuppressionEntry).values(email_or_domain=value.lower(), type=typ, reason=reason)
                            .on_conflict_do_nothing(index_elements=["email_or_domain"]))

    async def cancel_drafts_for(self, email_or_domain: str) -> int:
        from sqlalchemy import func, or_, update

        from gtm_core.db import session_scope
        from gtm_core.models import Contact, EmailDraft

        t = email_or_domain.lower()
        ids = (Contact.__table__.select().with_only_columns(Contact.id).where(
            or_(func.lower(Contact.email) == t, func.lower(Contact.email).like(f"%@{t}"))))
        async with session_scope() as s:
            res = await s.execute(update(EmailDraft).where(
                EmailDraft.contact_id.in_(ids), EmailDraft.status.in_(_OPEN_DRAFT)).values(status="rejected"))
            return res.rowcount or 0

    async def redact_contacts(self, email_or_domain: str) -> int:
        from sqlalchemy import func, or_, update

        from gtm_core.db import session_scope
        from gtm_core.models import Contact

        t = email_or_domain.lower()
        async with session_scope() as s:
            res = await s.execute(update(Contact).where(or_(
                func.lower(Contact.email) == t, func.lower(Contact.email).like(f"%@{t}"))).values(
                first_name=None, last_name=None, title=None, linkedin_url=None, is_unsubscribed=True,
                email=func.concat("redacted-", Contact.id.cast(__import__("sqlalchemy").String), "@redacted.invalid")))
            return res.rowcount or 0
