"""Fire signed test payloads at a running gateway.

  uv run python scripts/replay_webhooks.py --scenario champion_hiring --domain nimbuspay.example
  uv run python scripts/replay_webhooks.py --csv signals.csv
"""

import argparse
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
from gtm_core.config import get_settings
from gtm_integrations import clay


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def scenario(name: str, domain: str) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    if name == "champion_hiring":
        return [
            {"domain": domain, "signal_type": "champion_moved", "source": "sales_navigator", "occurred_at": now,
             "payload": {"new_title": "VP of Engineering"}},
            {"domain": domain, "signal_type": "hiring_spike", "source": "apollo", "occurred_at": now,
             "payload": {"open_roles": 6, "department": "engineering"}},
        ]
    if name == "funding":
        return [{"domain": domain, "signal_type": "funding_round", "source": "clay", "occurred_at": now,
                 "payload": {"round": "Series B", "amount_usd": 40_000_000}}]
    raise SystemExit(f"unknown scenario {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--scenario", default="champion_hiring")
    ap.add_argument("--domain", default="nimbuspay.example")
    ap.add_argument("--csv", help="Sales Navigator CSV export to send to /webhooks/signals/sales-nav")
    args = ap.parse_args()
    s = get_settings()
    if args.csv:
        body = open(args.csv, "rb").read()
        r = httpx.post(f"{args.url}/webhooks/signals/sales-nav", content=body,
                       headers={"X-N8N-Signature": _sign(s.n8n_webhook_secret, body), "Content-Type": "text/csv"})
    else:
        body = json.dumps(scenario(args.scenario, args.domain)).encode()
        r = httpx.post(f"{args.url}/webhooks/signals/ingest", content=body,
                       headers={"X-N8N-Signature": _sign(s.n8n_webhook_secret, body),
                                "Content-Type": "application/json"})
    print(r.status_code, r.text)
    _ = clay  # clay.sign is available for replaying /webhooks/clay/enrichment-complete payloads


if __name__ == "__main__":
    main()
