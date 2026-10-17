"""Slack Block Kit approval sender + request-signature verifier."""

import hashlib
import hmac
import time

from gtm_core.config import get_settings

from gtm_integrations.base import BaseClient

MAX_SKEW_SECONDS = 300


def verify_signature(signing_secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """Verify Slack v0 request signature, rejecting stale timestamps (replay protection)."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_SKEW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def build_approval_blocks(approval_id: str, title: str, details: str, reason: str) -> list[dict]:
    value = approval_id
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": details[:2800]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Why approval:* {reason}"}]},
        {
            "type": "actions",
            "block_id": f"approval_{approval_id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
                 "action_id": "approve", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Edit"}, "action_id": "edit", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"}, "style": "danger",
                 "action_id": "reject", "value": value},
            ],
        },
    ]


class SlackClient(BaseClient):
    name = "slack"

    def __init__(self, token: str | None = None, channel: str | None = None, **kw):
        s = get_settings()
        super().__init__(token or s.slack_bot_token, base_url="https://slack.com/api", **kw)
        self.channel = channel or s.slack_approval_channel_id
        self.sent: list[dict] = []

    async def post_approval(self, approval_id: str, title: str, details: str, reason: str) -> str:
        """Returns the message ts (stored in approval_requests.slack_message_ts)."""
        blocks = build_approval_blocks(approval_id, title, details, reason)
        if self.mock:
            ts = f"{time.time():.6f}"
            self.sent.append({"ts": ts, "channel": self.channel, "blocks": blocks})
            return ts
        data = await self._request(
            "POST",
            "/chat.postMessage",
            json={"channel": self.channel, "text": title, "blocks": blocks},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        return data["ts"]
