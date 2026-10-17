"""Redis-backed rate limits: per-integration windows and outbound send caps."""

import time
from datetime import UTC, datetime

import redis.asyncio as aioredis
from gtm_core.config import get_settings
from gtm_core.redis_client import incr_with_ttl

DAY_TTL = 172800


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class RateLimiter:
    def __init__(self, r: aioredis.Redis):
        self.r = r
        self.s = get_settings()

    # --- outbound caps -----------------------------------------------------------------
    def _keys(self, domain: str) -> tuple[str, str]:
        d = _today()
        return f"ratelimit:outbound:global:{d}", f"ratelimit:outbound:domain:{domain.lower()}:{d}"

    async def outbound_block_reason(self, domain: str) -> str | None:
        """Read-only headroom check (used by the policy engine for classification)."""
        gkey, dkey = self._keys(domain)
        g = int(await self.r.get(gkey) or 0)
        d = int(await self.r.get(dkey) or 0)
        if g >= self.s.global_daily_send_cap:
            return "global_daily_cap"
        if d >= self.s.domain_daily_send_cap:
            return "domain_daily_cap"
        return None

    async def consume_outbound(self, domain: str) -> tuple[bool, str | None]:
        """Atomically reserve one send. Rolls back if either cap is exceeded."""
        gkey, dkey = self._keys(domain)
        g = await incr_with_ttl(self.r, gkey, DAY_TTL)
        if g > self.s.global_daily_send_cap:
            await self.r.decr(gkey)
            return False, "global_daily_cap"
        d = await incr_with_ttl(self.r, dkey, DAY_TTL)
        if d > self.s.domain_daily_send_cap:
            await self.r.decr(dkey)
            await self.r.decr(gkey)
            return False, "domain_daily_cap"
        return True, None

    # --- external API windows ----------------------------------------------------------
    async def check_integration(self, tool: str, limit: int, window_seconds: int = 60) -> bool:
        bucket = int(time.time() // window_seconds)
        key = f"ratelimit:integration:{tool}:{window_seconds}s:{bucket}"
        count = await incr_with_ttl(self.r, key, window_seconds)
        return count <= limit
