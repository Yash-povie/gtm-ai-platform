"""Clay webhook emitter + HMAC verifier + local mock that simulates the async callback."""

import asyncio
import hashlib
import hmac
import json

import httpx
from gtm_core.config import get_settings

from gtm_integrations.base import BaseClient
from gtm_integrations.fixtures import dataset

SIGNATURE_HEADER = "X-Clay-Signature"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature)


def mock_enrichment_payload(domain: str) -> dict:
    acct = next((a for a in dataset.accounts() if a.domain == domain), None)
    return {
        "domain": domain,
        "enriched": acct is not None,
        "company": acct.model_dump(mode="json") if acct else None,
        "contacts": [c.model_dump(mode="json") for c in dataset.contacts().get(domain, [])],
    }


class ClayClient(BaseClient):
    name = "clay"

    def __init__(self, webhook_url: str | None = None, signing_secret: str | None = None, **kw):
        s = get_settings()
        self.webhook_url = webhook_url or s.clay_webhook_url
        self.signing_secret = signing_secret or s.clay_signing_secret
        # Mock when pointed at the local mock endpoint.
        super().__init__("mock" if "localhost" in self.webhook_url else "live", **kw)

    async def push_row(self, row: dict, callback_url: str | None = None) -> dict:
        """Push a row to a Clay inbound webhook table; results arrive later via callback."""
        if self.mock:
            if callback_url:
                asyncio.create_task(self._deliver_mock_callback(callback_url, row["domain"]))
            return {"status": "queued", "mock": True}
        return await self._request(
            "POST", self.webhook_url, json=row, headers={"X-Clay-Webhook-Key": self.api_key}
        )

    async def _deliver_mock_callback(self, url: str, domain: str, delay: float = 3.0) -> None:
        await asyncio.sleep(delay)
        body = json.dumps(mock_enrichment_payload(domain)).encode()
        async with httpx.AsyncClient() as c:
            await c.post(
                url,
                content=body,
                headers={SIGNATURE_HEADER: sign(self.signing_secret, body), "Content-Type": "application/json"},
            )
