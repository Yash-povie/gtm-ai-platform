"""M1.1 acceptance: all 10 MCP tools through an in-memory MCP client session (no docker needed)."""

import json

import pytest
from gtm_integrations.fixtures import dataset
from gtm_safety import audit as audit_mod
from mcp.client import Client
from sys3_command_center_mcp.server import TOOL_PERMISSIONS, create_server
from sys3_command_center_mcp.services import build_services
from sys3_command_center_mcp.voice_bridge import VoiceSession, plan


@pytest.fixture
async def svc(redis):
    audit_mod.set_sink(audit_mod.MemoryAuditSink())
    return await build_services(redis=redis)


class PerCallClient:
    """Opens a fresh in-memory MCP session per call (avoids anyio scopes spanning pytest fixtures)."""

    def __init__(self, server):
        self.server = server

    async def call_tool(self, name, args):
        async with Client(self.server) as c:
            return await c.call_tool(name, args)

    async def list_tools(self):
        async with Client(self.server) as c:
            return await c.list_tools()


@pytest.fixture
def client(svc):
    return PerCallClient(create_server(svc))


async def call(client, name: str, **args) -> dict:
    res = await client.call_tool(name, args)
    assert not res.is_error, res
    if getattr(res, "structured_content", None):
        sc = res.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


async def test_exposes_exactly_the_ten_spec_tools(client):
    names = {t.name for t in (await client.list_tools()).tools}
    assert names == set(TOOL_PERMISSIONS) and len(names) == 10


async def test_read_tools(client, svc):
    found = await call(client, "search_crm_accounts", query="Stripelike", limit=5)
    assert found["accounts"][0]["domain"] == "stripelike.example"

    acct = await call(client, "get_account_360", domain="stripelike.example")
    assert acct["account"]["name"] == "Stripelike Pay" and len(acct["contacts"]) == 3

    look = await call(client, "find_lookalike_accounts", seed_domain="stripe", top_k=5, min_shared_tech=1)
    assert look["seed"] == "stripelike.example" and 1 <= len(look["lookalikes"]) <= 5
    profiles = await svc.store.all_profiles()
    assert all(not profiles[x["domain"]].is_customer for x in look["lookalikes"])

    every = await call(client, "query_active_signals", min_score=0.0, days_back=60)
    strong = await call(client, "query_active_signals", min_score=70.0, days_back=60)
    assert every["signals"] and len(strong["signals"]) < len(every["signals"])
    assert all(s["decayed_score"] >= 70 for s in strong["signals"])

    bad = await call(client, "evaluate_email_draft", subject="hi", facts=["Series A", "$15M"],
                     body="Hi Sam,\n\nCongrats on your $40M Series B! Worth a chat?")
    assert bad["passed"] is False and bad["hallucination_score"] == 0.0 and bad["unsupported_claims"]


async def test_write_tools_end_to_end(client, svc):
    first = await call(client, "enrich_prospect_waterfall", domain="stripelike.example", target_titles=["VP"])
    again = await call(client, "enrich_prospect_waterfall", domain="stripelike.example", target_titles=["VP"])
    assert first["cache_hit"] is False and again["cache_hit"] is True and first["contacts_found"] >= 1

    task = await call(client, "upsert_crm_deal_or_task", domain="datawatch.example", object_type="task",
                      title="Call champion")
    assert task["safety_tier"] == "auto" and task["hubspot_object_id"] and task["salesforce_object_id"]
    deal = await call(client, "upsert_crm_deal_or_task", domain="datawatch.example", object_type="deal",
                      title="Expansion", amount_usd=50000)
    assert deal["hubspot_object_id"] and deal["salesforce_object_id"] is None

    camp = await call(client, "launch_sdr_campaign", icp_prompt="Fintech companies", campaign_id="mcp-demo",
                      max_accounts=2)
    assert camp["accounts_queued"] >= 1 and camp["pending_approvals"] == 1
    assert svc.deps.sender.sent == []  # launching never sends without approval
    pending = await svc.repo.pending_approvals()
    res = await call(client, "resolve_approval_request", approval_request_id=pending[0]["id"], decision="approved",
                     reviewer_note="lgtm")
    assert res["new_status"] == "approved" and res["resumed_thread_id"] == camp["thread_id"]
    assert len(svc.deps.sender.sent) == 1
    again = await call(client, "resolve_approval_request", approval_request_id=pending[0]["id"], decision="approved")
    assert again["new_status"].startswith("error")  # cannot double-resolve


async def test_destructive_tool_needs_operator_token(client, svc):
    domain = "datawatch.example"
    before = len(await svc.repo.list_contacts((await svc.repo.get_account(domain))["id"]))
    blocked = await call(client, "purge_or_suppress_prospect", email_or_domain=domain, reason="gdpr")
    assert blocked["suppressed"] is False and blocked["blocked"] is True
    wrong = await call(client, "purge_or_suppress_prospect", email_or_domain=domain, reason="gdpr",
                       confirm_token="deadbeef0000")
    assert wrong["suppressed"] is False
    token = svc.deps.gate.confirm_token("purge", domain)
    assert token not in json.dumps(blocked) + json.dumps(wrong)  # the token value never reaches the caller
    contacts = await svc.repo.list_contacts((await svc.repo.get_account(domain))["id"])
    assert len(contacts) == before and not any(c["is_unsubscribed"] for c in contacts)

    done = await call(client, "purge_or_suppress_prospect", email_or_domain=domain, reason="gdpr", confirm_token=token)
    assert done["suppressed"] is True and done["contacts_redacted"] == before and done["audit_log_id"] >= 1
    assert all(c["email"].endswith("@redacted.invalid") for c in svc.repo.contacts.values()
               if c["account_id"] == contacts[0]["account_id"])
    assert (domain, "domain") in await svc.repo.suppression_entries()


async def test_every_write_tool_is_audited(client, svc):
    sink = audit_mod.get_sink()
    await call(client, "enrich_prospect_waterfall", domain="stripelike.example")
    await call(client, "upsert_crm_deal_or_task", domain="datawatch.example", object_type="task", title="x")
    await call(client, "purge_or_suppress_prospect", email_or_domain="x.example", reason="r")
    actions = [r["action"] for r in sink.records]
    assert "enrich" in actions and "crm_write" in actions and "purge" in actions


def test_voice_planner_demo_sentence():
    steps = plan("Find our top 3 lookalikes to Stripe, enrich their VP of Engineering, and create a follow-up task in HubSpot")
    assert [s["tool"] for s in steps] == ["find_lookalike_accounts", "enrich_prospect_waterfall",
                                          "upsert_crm_deal_or_task"]
    assert steps[0]["args"]["top_k"] == 3 and steps[1]["foreach"] == "lookalikes"


async def test_voice_session_confirms_before_writes(svc):
    voice = VoiceSession(svc)
    st = await voice.say("s1", "Find our top 3 lookalikes to stripe, enrich their VP of Engineering, and create a "
                               "follow-up task in HubSpot")
    assert st["awaiting_confirmation"] and len(st["plan"]) == 3
    assert svc.deps.hubspot.tasks == []  # nothing executed before confirmation
    st = await voice.confirm("s1", True)
    assert not st["awaiting_confirmation"] and len(svc.deps.hubspot.tasks) == 3
    assert {o["tool"] for o in st["tool_outputs"]} == {"find_lookalike_accounts", "enrich_prospect_waterfall",
                                                       "upsert_crm_deal_or_task"}

    st = await voice.say("s2", "create a follow-up task for datawatch.example")
    declined = await voice.confirm("s2", False)
    assert "Cancelled" in declined["response"]


async def test_voice_delete_account_is_blocked_by_safety_gate(svc):
    voice = VoiceSession(svc)
    await voice.say("s3", "Delete the stripelike account")
    st = await voice.confirm("s3", True)  # even a confirmed voice command lacks the operator token
    out = [o for o in st["tool_outputs"] if o["tool"] == "purge_or_suppress_prospect"][0]["result"]
    assert out["suppressed"] is False and out["blocked"] is True
    assert dataset.accounts()  # fixtures untouched
