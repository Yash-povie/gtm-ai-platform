"""Maps raw n8n / Clay / Apollo webhook payloads to the unified SignalEvent and derives idempotency keys."""

import hashlib
import hmac
from datetime import UTC, datetime

from gtm_core.schemas import SignalEvent

N8N_SIGNATURE_HEADER = "X-N8N-Signature"
_CLAY_EVENT_MAP = {
    "job_change": "job_change", "champion_moved": "champion_moved", "funding": "funding_round",
    "funding_round": "funding_round", "hiring": "hiring_spike", "hiring_spike": "hiring_spike",
    "tech_added": "tech_added", "tech_removed": "tech_removed",
}


def sign_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, body: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign_hmac(secret, body), signature or "")


def _ts(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def idempotency_key(source: str, domain: str, signal_type: str, contact_email: str | None, occurred_at: datetime) -> str:
    raw = f"{source}|{domain.lower()}|{signal_type}|{(contact_email or '').lower()}|{occurred_at.date().isoformat()}"
    return f"{source}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _finish(source: str, domain: str, stype: str, email: str | None, occurred: datetime, payload: dict) -> SignalEvent:
    if not domain:
        raise ValueError("missing company domain")
    domain = domain.strip().lower()
    email = email.strip().lower() if email else None
    return SignalEvent(signal_type=stype, source=source, domain=domain, contact_email=email,  # type: ignore[arg-type]
                       occurred_at=occurred, payload=payload,
                       idempotency_key=idempotency_key(source, domain, stype, email, occurred))


def normalize_n8n(body: dict) -> SignalEvent:
    source = body.get("source", "n8n_scraper")
    return _finish(source, body.get("domain", ""), body.get("signal_type", ""), body.get("contact_email"),
                   _ts(body.get("occurred_at")), body.get("payload", {}))


def normalize_clay(body: dict) -> SignalEvent:
    stype = _CLAY_EVENT_MAP.get(str(body.get("event", "")).lower())
    if stype is None:
        raise ValueError(f"unsupported clay event {body.get('event')!r}")
    return _finish("clay", body.get("company_domain") or body.get("domain", ""), stype, body.get("email"),
                   _ts(body.get("occurred_at")), body.get("data", {}))


def normalize_apollo(body: dict) -> SignalEvent:
    if "open_roles" in body:
        return _finish("apollo", body.get("domain", ""), "hiring_spike", None, _ts(body.get("occurred_at")),
                       {"open_roles": body["open_roles"], "department": body.get("department", "engineering")})
    raise ValueError("unsupported apollo payload")


def normalize(source: str, body: dict) -> SignalEvent:
    return {"n8n": normalize_n8n, "clay": normalize_clay, "apollo": normalize_apollo}[source](body)
