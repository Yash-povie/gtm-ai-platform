"""CAN-SPAM / GDPR helpers: unsubscribe tokens, mandatory footer, opt-out detection, suppression."""

import re
from datetime import UTC, datetime, timedelta

import jwt
from gtm_core.config import get_settings

UNSUB_MARKER = "/webhooks/unsubscribe?token="
_OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|stop (emailing|contacting|sending)|opt[- ]?out|do not (email|contact)|take me off)\b",
    re.I,
)
_STOP_ONLY = re.compile(r"^\s*stop[.!]?\s*$", re.I)


def build_unsubscribe_token(email: str, days_valid: int = 365) -> str:
    s = get_settings()
    payload = {"sub": email.lower(), "aud": "unsubscribe", "exp": datetime.now(UTC) + timedelta(days=days_valid)}
    return jwt.encode(payload, s.unsubscribe_jwt_secret, algorithm="HS256")


def verify_unsubscribe_token(token: str) -> str | None:
    try:
        data = jwt.decode(token, get_settings().unsubscribe_jwt_secret, algorithms=["HS256"], audience="unsubscribe")
    except jwt.PyJWTError:
        return None
    return data.get("sub")


def unsubscribe_url(email: str) -> str:
    return f"{get_settings().unsubscribe_base_url}{UNSUB_MARKER}{build_unsubscribe_token(email)}"


def add_footer(email: str, body_text: str, body_html: str | None = None) -> tuple[str, str, dict[str, str]]:
    """Append unsubscribe link + postal address; returns (text, html, extra_headers)."""
    s = get_settings()
    url = unsubscribe_url(email)
    text = f"{body_text.rstrip()}\n\n--\n{s.sender_postal_address}\nUnsubscribe: {url}\n"
    html = (body_html if body_html is not None else "<p>" + body_text.replace("\n", "<br>") + "</p>")
    html += f'<hr><p style="font-size:12px">{s.sender_postal_address}<br><a href="{url}">Unsubscribe</a></p>'
    headers = {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return text, html, headers


def has_required_footer(body_text: str) -> bool:
    s = get_settings()
    return UNSUB_MARKER in body_text and s.sender_postal_address in body_text


def is_opt_out_text(text: str) -> bool:
    return bool(_OPT_OUT.search(text) or _STOP_ONLY.match(text))


def suppression_match(email: str, entries: list[tuple[str, str]]) -> str | None:
    """entries: (email_or_domain, type). Returns the matching entry type or None."""
    email = email.lower()
    domain = email.rsplit("@", 1)[-1]
    for value, typ in entries:
        v = value.lower()
        if v == email or v == domain or domain.endswith("." + v):
            return typ
    return None
