"""Shared client plumbing: sandbox toggle, retry with backoff, circuit breaker."""

import time
from typing import Any

import httpx
from gtm_core.observability import span
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class RateLimitError(Exception):
    def __init__(self, retry_after: float = 1.0):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_after: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            return False  # half-open: allow one trial
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


def is_mock_key(value: str) -> bool:
    return value in ("", "mock") or value.endswith("-mock")


class BaseClient:
    """Base for external clients. `mock=True` => deterministic local fixtures, no network."""

    name = "base"

    def __init__(self, api_key: str, *, base_url: str = "", http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.mock: bool = is_mock_key(api_key)
        self.base_url = base_url
        self._http = http
        self.breaker = CircuitBreaker()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=20.0)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _request(self, method: str, url: str, **kw: Any) -> dict:
        if self.breaker.is_open:
            raise CircuitOpenError(f"{self.name} circuit open")

        @retry(
            retry=retry_if_exception_type((RateLimitError, httpx.TransportError)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            reraise=True,
        )
        async def _once() -> dict:
            with span("integration.call", tool_name=self.name, endpoint=url) as s:
                resp = await self._client().request(method, url, **kw)
                s.set_attribute("status_code", resp.status_code)
                if resp.status_code == 429:
                    raise RateLimitError(float(resp.headers.get("retry-after", 1)))
                if resp.status_code >= 500:
                    raise httpx.TransportError(f"{self.name} {resp.status_code}")
                resp.raise_for_status()
                return resp.json() if resp.content else {}

        try:
            result = await _once()
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return result
