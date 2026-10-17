"""Sales Navigator bridge. There is no public API, so signals arrive via Clay/CSV only.

We never scrape LinkedIn. This module only normalizes rows that a human or Clay exported.
"""

import csv
import hashlib
import io
from datetime import UTC, datetime

from gtm_core.schemas import SignalEvent

REQUIRED = {"signal_type", "company_domain", "occurred_at"}
ALLOWED = {"job_change", "champion_moved", "hiring_spike", "funding_round"}


class SalesNavCsvError(ValueError):
    pass


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def parse_csv(text: str) -> tuple[list[SignalEvent], list[str]]:
    """Returns (events, row_errors). Rows missing a domain (only a LinkedIn URL) are rejected."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED.issubset(set(reader.fieldnames)):
        raise SalesNavCsvError(f"CSV must contain columns {sorted(REQUIRED)}")
    events: list[SignalEvent] = []
    errors: list[str] = []
    for n, row in enumerate(reader, start=2):
        try:
            domain = (row.get("company_domain") or "").strip().lower()
            if not domain:
                raise ValueError("missing company_domain (LinkedIn URL alone is not enough)")
            stype = row["signal_type"].strip()
            if stype not in ALLOWED:
                raise ValueError(f"unsupported signal_type {stype!r}")
            occurred = _parse_ts(row["occurred_at"])
            email = (row.get("contact_email") or "").strip().lower() or None
            key = hashlib.sha256(f"sales_nav|{domain}|{stype}|{email}|{occurred.date()}".encode()).hexdigest()[:32]
            events.append(
                SignalEvent(
                    signal_type=stype,  # type: ignore[arg-type]
                    source="sales_navigator",
                    domain=domain,
                    contact_email=email,
                    occurred_at=occurred,
                    payload={k: v for k, v in row.items() if k not in REQUIRED and v},
                    idempotency_key=f"sales_nav:{key}",
                )
            )
        except (ValueError, KeyError) as e:
            errors.append(f"row {n}: {e}")
    return events, errors
