from datetime import UTC, datetime, timedelta

import pytest
from gtm_core.config import Settings
from gtm_safety import audit as audit_mod
from gtm_safety import compliance
from gtm_safety.policy_engine import ActionContext, EmailContext, classify_action, classify_email
from gtm_safety.rate_limiter import RateLimiter

PROD_LIVE = Settings(gtm_env="production", gtm_live_send_enabled=True, gtm_require_approval_all=False)


def ctx(**kw) -> EmailContext:
    base = dict(account_tier="tier_2", qa_hallucination_score=1.0, qa_composite_score=0.95)
    base.update(kw)
    return EmailContext(**base)


def test_auto_for_tier2_clean_live():
    d = classify_email(ctx(), PROD_LIVE)
    assert d.tier == "auto"


def test_tier1_account_always_approve():
    assert classify_email(ctx(account_tier="tier_1"), PROD_LIVE).tier == "approve"


def test_sandbox_and_require_all_force_approve():
    assert classify_email(ctx(), Settings(gtm_env="sandbox")).tier == "approve"
    assert classify_email(ctx(), Settings(gtm_env="production", gtm_require_approval_all=True)).rule_triggered == "require_approval_all"
    live_off = Settings(gtm_env="production", gtm_require_approval_all=False, gtm_live_send_enabled=False)
    assert classify_email(ctx(), live_off).rule_triggered == "live_send_disabled"


def test_marginal_qa_is_approve():
    assert classify_email(ctx(qa_composite_score=0.80), PROD_LIVE).tier == "approve"


@pytest.mark.parametrize(
    "kw,rule",
    [
        (dict(contact_is_unsubscribed=True), "unsubscribed"),
        (dict(contact_email_status="bounced"), "bounced"),
        (dict(suppression_match="competitor"), "suppressed"),
        (dict(qa_hallucination_score=0.0), "hallucination"),
        (dict(qa_composite_score=0.70), "qa_low"),
        (dict(qa_hallucination_score=None, qa_composite_score=None), "qa_missing"),
        (dict(has_required_footer=False), "missing_footer"),
        (dict(rate_limit_reason="domain_daily_cap"), "rate_limit"),
        (dict(last_contacted_at=datetime.now(UTC) - timedelta(days=3)), "cooldown"),
    ],
)
def test_block_rules_beat_everything(kw, rule):
    # even tier_2 + live + otherwise-clean must block
    d = classify_email(ctx(**kw), PROD_LIVE)
    assert d.tier == "block" and d.rule_triggered == rule


def test_block_precedes_approve_for_tier1():
    d = classify_email(ctx(account_tier="tier_1", contact_is_unsubscribed=True), PROD_LIVE)
    assert d.tier == "block"


def test_cooldown_expired_allows():
    old = datetime.now(UTC) - timedelta(days=15)
    assert classify_email(ctx(last_contacted_at=old), PROD_LIVE).tier == "auto"


def test_action_classification():
    assert classify_action(ActionContext(action="read")).tier == "auto"
    assert classify_action(ActionContext(action="crm_write", record_count=1)).tier == "auto"
    assert classify_action(ActionContext(action="crm_bulk_update", record_count=6)).tier == "approve"
    assert classify_action(ActionContext(action="purge")).tier == "block"
    assert classify_action(ActionContext(action="purge", confirm_token_valid=True)).tier == "approve"


async def test_outbound_caps_domain_then_global(redis):
    rl = RateLimiter(redis)
    rl.s = Settings(domain_daily_send_cap=3, global_daily_send_cap=4)
    out = [await rl.consume_outbound("a.example") for _ in range(4)]
    assert [ok for ok, _ in out] == [True, True, True, False]
    assert out[-1][1] == "domain_daily_cap"
    assert await rl.outbound_block_reason("a.example") == "domain_daily_cap"
    assert (await rl.consume_outbound("b.example"))[0] is True  # 4th global
    assert (await rl.consume_outbound("c.example")) == (False, "global_daily_cap")
    # rolled-back increments leave counters at the caps, not above
    assert int(await redis.get(next(k for k in await redis.keys("ratelimit:outbound:global:*")))) == 4


def test_unsubscribe_token_roundtrip_and_tamper():
    t = compliance.build_unsubscribe_token("A@B.example")
    assert compliance.verify_unsubscribe_token(t) == "a@b.example"
    assert compliance.verify_unsubscribe_token(t + "x") is None


def test_footer_and_headers():
    text, html, headers = compliance.add_footer("a@b.example", "Hello")
    assert compliance.has_required_footer(text)
    assert "Unsubscribe" in html
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert not compliance.has_required_footer("Hello")


@pytest.mark.parametrize("txt", ["Please unsubscribe me", "remove me from this list", "STOP", "opt-out", "do not contact us"])
def test_opt_out_detection(txt):
    assert compliance.is_opt_out_text(txt)


def test_not_opt_out():
    assert not compliance.is_opt_out_text("Sounds interesting, can we chat Tuesday? Don't stop by though")


def test_suppression_matching():
    entries = [("rival.example", "competitor"), ("x@y.example", "email")]
    assert compliance.suppression_match("bob@rival.example", entries) == "competitor"
    assert compliance.suppression_match("bob@eu.rival.example", entries) == "competitor"
    assert compliance.suppression_match("X@Y.example", entries) == "email"
    assert compliance.suppression_match("z@y.example", entries) is None


async def test_audit_log_written():
    sink = audit_mod.MemoryAuditSink()
    audit_mod.set_sink(sink)
    await audit_mod.audit("sys1_sdr", "send_email", "email_draft", "123", "auto", after={"status": "sent"})
    assert sink.records[0]["actor"] == "sys1_sdr" and sink.records[0]["safety_tier"] == "auto"
    assert sink.records[0]["trace_id"]
