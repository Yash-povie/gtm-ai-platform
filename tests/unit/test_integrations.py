import json

import pytest
from gtm_core.embeddings import cosine, embed_text
from gtm_integrations import clay, slack
from gtm_integrations.apollo import ApolloClient
from gtm_integrations.base import CircuitBreaker, is_mock_key
from gtm_integrations.fixtures import dataset
from gtm_integrations.hubspot import HubSpotClient
from gtm_integrations.sales_nav import SalesNavCsvError, parse_csv
from gtm_integrations.salesforce import SalesforceClient
from gtm_safety.rate_limiter import RateLimiter


def test_dataset_has_200_deterministic_unique_accounts():
    a = dataset.accounts()
    assert len(a) == 200
    assert len({x.domain for x in a}) == 200
    assert all(x.domain.endswith(".example") for x in a)
    assert sum(1 for x in a if x.is_customer) == 20
    assert dataset.accounts() is dataset.accounts()  # cached => deterministic


def test_contacts_emails_unique_and_safe():
    emails = [c.email for people in dataset.contacts().values() for c in people]
    assert len(emails) == len(set(emails)) == 600  # globally unique: domain is part of the email
    assert all(e.endswith(".example") for e in emails)


async def test_apollo_mock_filters():
    ap = ApolloClient("mock")
    assert ap.mock
    res = await ap.search_companies({"industries": ["Fintech"], "tech_keywords": ["AWS"]}, limit=100)
    assert res and all(r.industry == "Fintech" and "AWS" in r.tech_stack for r in res)
    people = await ap.search_people(res[0].domain, titles=["VP"])
    assert all("vp" in (p.title or "").lower() for p in people)
    assert await ap.match_person("nobody@nowhere.example") is None


async def test_hubspot_and_salesforce_mock_are_idempotent():
    acct = dataset.accounts()[5]
    person = dataset.contacts()[acct.domain][0]
    for crm, upsert_acct in ((HubSpotClient("mock"), "upsert_company"), (SalesforceClient(), "upsert_account")):
        first = await getattr(crm, upsert_acct)(acct)
        again = await getattr(crm, upsert_acct)(acct)
        assert first == again
        cid = await crm.upsert_contact(person, first)
        assert cid == await crm.upsert_contact(person, first)


def test_clay_hmac_roundtrip_and_tamper():
    body = json.dumps(clay.mock_enrichment_payload(dataset.accounts()[0].domain)).encode()
    sig = clay.sign("secret", body)
    assert clay.verify("secret", body, sig)
    assert not clay.verify("secret", body + b"x", sig)
    assert not clay.verify("other", body, sig)
    assert not clay.verify("secret", body, None)


def test_slack_signature_and_replay_window():
    body, ts = b"payload=1", "1700000000"
    import hashlib
    import hmac

    sig = "v0=" + hmac.new(b"s", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert slack.verify_signature("s", ts, body, sig, now=1700000100)
    assert not slack.verify_signature("s", ts, body, sig, now=1700001000)  # stale
    assert not slack.verify_signature("s", ts, body, "v0=bad", now=1700000100)


async def test_slack_mock_post_returns_ts_and_blocks():
    c = slack.SlackClient("xoxb-mock")
    ts = await c.post_approval("abc", "Approve email", "details", "tier_1")
    assert ts and c.sent[0]["blocks"][-1]["elements"][0]["value"] == "abc"


def test_sales_nav_csv_normalizes_and_rejects_bad_rows():
    text = (
        "signal_type,company_domain,contact_email,occurred_at\n"
        "champion_moved,Acme.example,Jo@Acme.example,2026-01-02T00:00:00Z\n"
        "job_change,,x@y.example,2026-01-02\n"
        "weird,acme.example,,2026-01-02\n"
    )
    events, errors = parse_csv(text)
    assert len(events) == 1 and events[0].domain == "acme.example" and events[0].contact_email == "jo@acme.example"
    assert len(errors) == 2
    with pytest.raises(SalesNavCsvError):
        parse_csv("a,b\n1,2\n")


def test_circuit_breaker_opens_and_resets():
    cb = CircuitBreaker(failure_threshold=2, reset_after=0.0)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open  # reset_after=0 => half-open immediately
    cb2 = CircuitBreaker(failure_threshold=2, reset_after=60)
    cb2.record_failure()
    cb2.record_failure()
    assert cb2.is_open
    cb2.record_success()
    assert not cb2.is_open


def test_mock_key_detection():
    assert is_mock_key("mock") and is_mock_key("xoxb-mock") and not is_mock_key("sk-real")


def test_embeddings_similarity():
    a = embed_text("Series B fintech using AWS and Snowflake")
    b = embed_text("fintech company using Snowflake and AWS")
    c = embed_text("organic farm tractor harvest")
    assert len(a) == 1536
    assert cosine(a, b) > cosine(a, c)


async def test_integration_rate_limit_window(redis):
    rl = RateLimiter(redis)
    results = [await rl.check_integration("apollo", limit=3, window_seconds=60) for _ in range(5)]
    assert results == [True, True, True, False, False]


async def test_slack_ok_false_raises_clear_error():
    import httpx

    handler = lambda req: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})  # noqa: E731
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.com/api")
    with pytest.raises(slack.SlackError, match="invalid_auth"):
        await slack.SlackClient("xoxb-live", http=http).post_approval("id", "t", "d", "r")
