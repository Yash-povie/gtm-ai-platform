"""M2.1 acceptance: ICP prompt -> discovery -> scoring -> draft -> QA -> interrupt -> resume -> CRM sync.

Runs without docker: MemoryRepo + fakeredis + InMemorySaver + sandbox sender.
"""

import pytest
from gtm_core.config import Settings
from gtm_core.repo import MemoryRepo
from gtm_safety import audit as audit_mod
from sys1_autonomous_sdr.graph import SdrRunner, build_deps
from sys1_autonomous_sdr.nodes import classify_reply, handle_reply, icp_score, tier_for
from sys1_autonomous_sdr.prompts import Drafter, parse_icp_deterministic
from sys1_autonomous_sdr.state import SandboxSender

PROD_AUTO = Settings(gtm_env="production", gtm_live_send_enabled=True, gtm_require_approval_all=False)


@pytest.fixture
def audit_sink():
    sink = audit_mod.MemoryAuditSink()
    audit_mod.set_sink(sink)
    return sink


async def drive(runner: SdrRunner, status: dict, decision: str = "approved", limit: int = 50) -> dict:
    """Resolve approvals until the campaign finishes."""
    for _ in range(limit):
        if status["finished"]:
            return status
        assert status["pending_approvals"] == 1, status
        status = await runner.resume(status["thread_id"], {"decision": decision, "reviewer": "tester"})
    raise AssertionError("campaign did not finish")


class ScriptedDrafter(Drafter):
    """Fabricates a funding claim on the first N attempts, then behaves."""

    def __init__(self, bad_attempts: int):
        super().__init__(use_llm=False)
        self.bad_attempts, self.calls = bad_attempts, 0

    async def draft(self, *, facts, value_prop, critique=None, attempt=1):
        self.calls += 1
        out = self._template(facts, value_prop)
        if attempt <= self.bad_attempts:
            out.body = out.body.replace("Worth a", "Congrats on your $999M Series Z raise. Worth a")
        return out


async def test_prompt_parsing_and_scoring():
    f = parse_icp_deterministic("Series B Fintechs using AWS and Snowflake with 100 to 900 employees")
    assert f.industries == ["Fintech"] and f.funding_stages == ["Series B"]
    assert f.tech_keywords == ["AWS", "Snowflake"] and (f.headcount_min, f.headcount_max) == (100, 900)
    assert tier_for(85, 100) == "tier_1" and tier_for(40, 800) == "tier_1"
    assert tier_for(70, 100) == "tier_2" and tier_for(59.9, 100) == "tier_3"
    acct = {"industry": "Fintech", "tech_stack": ["AWS"], "employee_count": 200, "funding_stage": "Series B"}
    assert icp_score(acct, f.model_dump(), [{"seniority": "vp"}]) == 100 - 30 * 0.5  # only half the tech matches


async def test_sandbox_campaign_interrupts_then_sends_and_syncs_crm(redis, audit_sink):
    deps = build_deps(repo=MemoryRepo(), redis=redis)
    runner = SdrRunner(deps)
    st = await runner.start("Fintech companies using AWS", "demo", max_accounts=3)
    assert st["accounts_queued"] >= 1
    assert st["pending_approvals"] == 1 and not st["finished"]  # sandbox => every email needs a human
    assert deps.sender.sent == []  # nothing leaves before approval
    approval = (await deps.repo.pending_approvals())[0]
    assert approval["action_type"] == "send_email" and approval["slack_message_ts"]
    assert deps.gate.slack.sent  # Slack Block Kit message posted

    st = await drive(runner, st)
    n = st["completed"]
    assert n == st["accounts_queued"] - len([e for e in st["errors"] if e.startswith("no eligible")])
    assert len(deps.sender.sent) == n
    for mail in deps.sender.sent:
        assert "List-Unsubscribe" in mail["headers"] and "Unsubscribe:" in mail["text"]
        assert mail["to"].endswith(".example")
    assert len(deps.hubspot.activities) == n and len(deps.salesforce.activities) == n
    assert all(d["status"] == "sent" for d in deps.repo.drafts.values())
    assert all(q["passed"] for q in deps.repo.qa)
    actions = {r["action"] for r in audit_sink.records}
    assert {"classify_email", "approval_requested", "approval_approved", "send_email"} <= actions


async def test_rejection_sends_nothing(redis, audit_sink):
    deps = build_deps(repo=MemoryRepo(), redis=redis)
    runner = SdrRunner(deps)
    st = await runner.start("Fintech companies", "rej", max_accounts=2)
    assert st["accounts_queued"] >= 1 and st["pending_approvals"] == 1
    st = await drive(runner, st, decision="rejected")
    assert deps.sender.sent == []
    assert deps.repo.drafts and all(d["status"] == "rejected" for d in deps.repo.drafts.values())


async def test_prod_live_auto_sends_tier2_but_always_gates_tier1(redis, audit_sink):
    deps = build_deps(repo=MemoryRepo(), redis=redis, settings=PROD_AUTO, sender=SandboxSender())
    deps.settings.global_daily_send_cap = 50
    runner = SdrRunner(deps)
    st = await runner.start("Fintech companies", "prod", max_accounts=6)
    st = await drive(runner, st)
    tiers = {d["account_domain"]: d for d in st["drafts"]}
    accounts = {a["domain"]: a for a in deps.repo.accounts.values()}
    assert tiers
    for domain, d in tiers.items():
        if accounts[domain]["account_tier"] == "tier_1":
            assert d["safety_tier"] == "approve" and d["approval_status"] == "approved"
        else:
            assert d["safety_tier"] == "auto" and d["approval_status"] == "auto_approved"
    assert any(d["safety_tier"] == "auto" for d in tiers.values()) or any(
        a["account_tier"] == "tier_1" for a in accounts.values())


async def test_qa_retry_loop_self_corrects_hallucination(redis, audit_sink):
    drafter = ScriptedDrafter(bad_attempts=1)
    deps = build_deps(repo=MemoryRepo(), redis=redis, drafter=drafter)
    runner = SdrRunner(deps)
    st = await runner.start("Fintech companies", "retry", max_accounts=1)
    assert st["pending_approvals"] == 1
    approval = (await deps.repo.pending_approvals())[0]
    draft = deps.repo.drafts[approval["target_entity_id"]]
    assert "$999M" not in draft["body_text"]
    assert drafter.calls == 2
    failed = [q for q in deps.repo.qa if not q["passed"]]
    assert failed and failed[0]["hallucination_score"] == 0.0 and "$999M" in " ".join(failed[0]["unsupported_claims"])
    st = await drive(runner, st)
    assert st["drafts"][0]["attempt_count"] == 2 and st["drafts"][0]["qa_passed"]


async def test_three_failed_qa_attempts_never_send(redis, audit_sink):
    deps = build_deps(repo=MemoryRepo(), redis=redis, drafter=ScriptedDrafter(bad_attempts=99))
    st = await SdrRunner(deps).start("Fintech companies", "bad", max_accounts=1)
    assert st["accounts_queued"] == 1 and st["finished"] and st["pending_approvals"] == 0
    assert deps.sender.sent == []
    assert [d["status"] for d in deps.repo.drafts.values()] == ["qa_failed"]
    assert st["drafts"][0]["attempt_count"] == 3


async def test_suppressed_domain_is_blocked_not_sent(redis, audit_sink):
    probe = build_deps(repo=MemoryRepo(), redis=redis)
    await SdrRunner(probe).start("Fintech companies", "probe", max_accounts=1)
    domain = (await probe.repo.pending_approvals())[0]["payload_snapshot"]["to"].split("@")[1]

    deps = build_deps(repo=MemoryRepo(), redis=redis, settings=PROD_AUTO, sender=SandboxSender())
    await deps.repo.add_suppression(domain, "competitor", "test")
    st = await SdrRunner(deps).start("Fintech companies", "supp", max_accounts=1)
    assert st["finished"] and deps.sender.sent == []
    d = st["drafts"][0]
    assert d["account_domain"] == domain and d["safety_tier"] == "block" and d["block_rule"] == "suppressed"
    assert [x["status"] for x in deps.repo.drafts.values()] == ["rejected"]


async def test_domain_cap_blocks_at_gate(redis, audit_sink):
    s = Settings(gtm_env="production", gtm_live_send_enabled=True, gtm_require_approval_all=False,
                 domain_daily_send_cap=0)
    deps = build_deps(repo=MemoryRepo(), redis=redis, settings=s, sender=SandboxSender())
    st = await SdrRunner(deps).start("Fintech companies", "cap", max_accounts=2)
    assert st["drafts"] and st["finished"] and deps.sender.sent == []
    assert {d["block_rule"] for d in st["drafts"]} == {"rate_limit"}


async def test_reply_handling_unsubscribe_and_positive(redis, audit_sink):
    deps = build_deps(repo=MemoryRepo(), redis=redis)
    runner = SdrRunner(deps)
    st = await drive(runner, await runner.start("Fintech companies", "reply", max_accounts=1))
    draft_id = st["drafts"][0]["draft_id"]
    pos = await handle_reply(deps, draft_id, "Sounds good, let's chat Tuesday")
    assert pos["sentiment"] == "positive" and "booking_link" in pos and deps.hubspot.tasks
    out = await handle_reply(deps, draft_id, "Please remove me from your list")
    assert out["sentiment"] == "unsubscribe"
    contact = await deps.repo.get_contact_by_email(st["drafts"][0]["contact_email"])
    assert contact["is_unsubscribed"] and contact["email"] in {e for e, _ in await deps.repo.suppression_entries()}


async def test_replayed_approval_id_cannot_approve_the_next_email(redis, audit_sink):
    from gtm_safety.gate import ApprovalError

    deps = build_deps(repo=MemoryRepo(), redis=redis)
    runner = SdrRunner(deps)
    st = await runner.start("Fintech companies using AWS", "replay", max_accounts=3)
    assert st["accounts_queued"] >= 2, "need 2+ accounts to prove the replay is harmless"
    first = st["pending_approval_ids"][0]
    st = await runner.resume_by_approval(first, {"decision": "approved", "reviewer": "a"})
    second = st["pending_approval_ids"][0]
    assert second != first and len(deps.sender.sent) == 1
    with pytest.raises(ApprovalError):
        await runner.resume_by_approval(first, {"decision": "approved", "reviewer": "attacker"})
    assert len(deps.sender.sent) == 1  # the second email is still waiting for its own approval
    assert (await deps.repo.get_approval(second))["status"] == "pending"


def test_reply_classifier():
    assert classify_reply("I'm out of office until Monday") == "ooo"
    assert classify_reply("not interested, thanks") == "negative"
    assert classify_reply("STOP") == "unsubscribe"
    assert classify_reply("who are you?") == "neutral"
