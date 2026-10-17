"""Time-decay + composite intent scoring."""

from datetime import UTC, datetime

HALF_LIFE_DAYS = 14.0
THRESHOLD = 75.0
WINDOW_DAYS = 30

_BASE = {"funding_round": 80.0, "champion_moved": 85.0, "job_change": 70.0, "tech_added": 55.0, "tech_removed": 40.0}


def base_strength(signal_type: str, payload: dict | None = None) -> float:
    payload = payload or {}
    if signal_type == "hiring_spike":
        return min(80.0, 55.0 + 2.5 * float(payload.get("open_roles", 0)))
    if signal_type == "funding_round":
        return 90.0 if float(payload.get("amount_usd", 0)) >= 40_000_000 else _BASE["funding_round"]
    return _BASE.get(signal_type, 50.0)


def decay(strength: float, occurred_at: datetime, now: datetime | None = None,
          half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Exponential decay: strength halves every `half_life_days`. Future-dated events are not boosted."""
    now = now or datetime.now(UTC)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
    return round(strength * 0.5 ** (age_days / half_life_days), 2)


def composite(decayed_scores: list[float]) -> float:
    """Noisy-OR over 0-100 scores: independent signals reinforce each other, never exceed 100."""
    miss = 1.0
    for s in decayed_scores:
        miss *= 1.0 - max(0.0, min(100.0, s)) / 100.0
    return round(100.0 * (1.0 - miss), 2)


def crosses_threshold(score: float, threshold: float = THRESHOLD) -> bool:
    return score >= threshold
