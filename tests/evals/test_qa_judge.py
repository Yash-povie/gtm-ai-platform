import json
from pathlib import Path

import pytest
from sys4_outbound_qa.attribution import aggregate_attribution
from sys4_outbound_qa.judge import QAJudge
from sys4_outbound_qa.rubrics import JudgeInput, composite, is_passed

GOLDEN = json.loads((Path(__file__).parent / "golden_emails.json").read_text())


def to_input(rec: dict) -> JudgeInput:
    return JudgeInput(
        source_facts=rec["source_facts"], value_prop=rec["value_prop"], subject=rec["subject"], body=rec["body"]
    )


def test_golden_set_shape():
    labels = [r["label"] for r in GOLDEN]
    assert len(GOLDEN) == 50
    assert labels.count("clean") == 25 and labels.count("hallucination") == 15 and labels.count("spam") == 10


@pytest.mark.parametrize("rec", [r for r in GOLDEN if r["label"] == "clean"], ids=lambda r: r["id"])
async def test_clean_emails_pass(rec):
    res = await QAJudge(use_llm=False).evaluate(to_input(rec))
    assert res.passed, (res.unsupported_claims, res.tone_score, res.relevance_score)
    assert res.hallucination_score == 1.0


@pytest.mark.parametrize("rec", [r for r in GOLDEN if r["label"] == "hallucination"], ids=lambda r: r["id"])
async def test_every_planted_hallucination_is_blocked(rec):
    res = await QAJudge(use_llm=False).evaluate(to_input(rec))
    assert res.hallucination_score == 0.0 and not res.passed
    assert rec["planted_claim"] in " ".join(res.unsupported_claims), res.unsupported_claims


@pytest.mark.parametrize("rec", [r for r in GOLDEN if r["label"] == "spam"], ids=lambda r: r["id"])
async def test_spam_fails_composite(rec):
    res = await QAJudge(use_llm=False).evaluate(to_input(rec))
    assert not res.passed and res.tone_score <= 0.25


def test_composite_and_pass_rule():
    assert composite(1.0, 1.0, 1.0) == 1.0
    assert composite(0.0, 1.0, 1.0) == 0.5
    assert is_passed(1.0, 0.85) and not is_passed(1.0, 0.84) and not is_passed(0.99, 1.0)


async def test_llm_failure_falls_back_to_deterministic():
    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**_):
                raise RuntimeError("api down")

    bad = next(r for r in GOLDEN if r["label"] == "hallucination")
    res = await QAJudge(client=Boom(), use_llm=True).evaluate(to_input(bad))
    assert not res.passed and res.hallucination_score == 0.0


async def test_lenient_llm_cannot_override_hallucination_floor():
    class Resp:
        content = [type("B", (), {"type": "text", "text": json.dumps(
            {"hallucination_score": 1.0, "tone_score": 1.0, "relevance_score": 1.0, "unsupported_claims": [],
             "critique": "looks great"})})()]

    class Lenient:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**_):
                return Resp()

    bad = next(r for r in GOLDEN if r["label"] == "hallucination")
    res = await QAJudge(client=Lenient(), use_llm=True).evaluate(to_input(bad))
    assert res.hallucination_score == 0.0 and not res.passed and res.unsupported_claims


def test_attribution_aggregation_orders_by_reply_rate():
    def d(v, status, sent, **kw):
        return {"prompt_name": "sdr_email", "version_tag": v, "status": status, "composite_score": 0.9, **kw}

    drafts = (
        [d("v1", "sent", 1, reply_sentiment="positive")] + [d("v1", "sent", 1)] * 3
        + [d("v2", "replied", 1, reply_sentiment="positive", meeting_booked=True)] * 3 + [d("v2", "sent", 1)]
        + [d("v2", "draft", 0)]
    )
    rows = aggregate_attribution(drafts)
    assert [r["version_tag"] for r in rows] == ["v2", "v1"]
    assert rows[0]["positive_reply_rate"] == 0.75 and rows[0]["meetings_booked"] == 3
    assert rows[0]["total_drafts"] == 5 and rows[0]["total_sent"] == 4
