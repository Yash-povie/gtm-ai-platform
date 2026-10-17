"""Redis pool, fixed-window counters (rate limiting), idempotency keys and locks."""

import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from gtm_core.config import get_settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def set_redis(client: aioredis.Redis | None) -> None:
    """Inject a client (tests use fakeredis)."""
    global _redis
    _redis = client


async def incr_with_ttl(r: aioredis.Redis, key: str, ttl: int) -> int:
    """Atomically increment a counter, setting TTL on first hit."""
    pipe = r.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, ttl, nx=True)
    count, _ = await pipe.execute()
    return int(count)


async def check_and_set_idempotency(r: aioredis.Redis, source: str, digest: str) -> bool:
    """True if this is the first time we see (source, digest); False for duplicates."""
    return bool(await r.set(f"idempotency:webhook:{source}:{digest}", "1", nx=True, ex=86400))


@asynccontextmanager
async def account_lock(r: aioredis.Redis, account_id: str, ttl: int = 120):
    """Distributed lock preventing concurrent SDR + Signal runs on one account."""
    key = f"lock:account_process:{account_id}"
    token = uuid.uuid4().hex
    acquired = await r.set(key, token, nx=True, ex=ttl)
    try:
        yield bool(acquired)
    finally:
        if acquired and await r.get(key) == token:
            await r.delete(key)
