"""Append-only audit log writer. The DB also enforces immutability with a trigger."""

from typing import Any, Protocol

from gtm_core.models import AuditLog
from gtm_core.observability import new_trace_id


class AuditSink(Protocol):
    async def write(self, record: dict[str, Any]) -> int: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write(self, record: dict[str, Any]) -> int:
        self.records.append(record)
        return len(self.records)


class DbAuditSink:
    async def write(self, record: dict[str, Any]) -> int:
        from gtm_core.db import session_scope

        async with session_scope() as session:
            row = AuditLog(**record)
            session.add(row)
            await session.flush()
            return int(row.id)


_sink: AuditSink = MemoryAuditSink()


def set_sink(sink: AuditSink) -> None:
    global _sink
    _sink = sink


def get_sink() -> AuditSink:
    return _sink


async def audit(
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    safety_tier: str,
    before: dict | None = None,
    after: dict | None = None,
    trace_id: str | None = None,
) -> int:
    return await _sink.write(
        {
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "safety_tier": safety_tier,
            "before_state": before,
            "after_state": after,
            "trace_id": trace_id or new_trace_id(),
        }
    )
