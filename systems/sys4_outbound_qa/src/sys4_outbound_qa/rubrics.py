"""Deterministic rubrics for the Outbound QA judge.

These always run (even alongside the LLM judge). The hallucination check is the safety floor:
every specific, checkable claim in the draft must be present in SOURCE_FACTS (or the value prop).
"""

import json
import re
from typing import Any

from gtm_core.schemas import QAResult
from pydantic import BaseModel, Field

SPAM_PHRASES = [
    "i hope this finds you well",
    "just circling back",
    "circle back",
    "synergy",
    "touch base",
    "game-changer",
    "game changer",
    "revolutionary",
    "act now",
    "limited time",
    "per my last email",
    "guaranteed",
    "once in a lifetime",
]
FAKE_FAMILIARITY = ["my friend", "buddy", "pal", "as we discussed", "as per our call"]
MAX_WORDS = 125

TECH_VOCAB = [
    "AWS", "GCP", "Azure", "Snowflake", "Datadog", "Kubernetes", "Terraform", "Postgres",
    "Kafka", "dbt", "Salesforce", "HubSpot", "Stripe", "Okta", "Databricks",
]  # fmt: skip
INVESTOR_VOCAB = ["Sequoia", "a16z", "Accel", "Lightspeed", "Tiger Global", "Greylock", "Index Ventures", "Insight Partners"]
TITLE_VOCAB = ["VP of Engineering", "Head of Data", "CTO", "Director of Sales", "VP of Marketing", "CEO", "CFO"]

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|million|[kKmMbB])?\b")
_STAGE = re.compile(r"\b(seed|series [a-f]|ipo)\b", re.I)
_COUNT = re.compile(
    r"\b(\d[\d,]*)\s+(?:open\s+)?(?:[a-z]+\s+)?(roles|engineers|employees|people|customers|offices|positions)\b", re.I
)
_PCT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
_GREETING = re.compile(r"^\s*(?i:hi|hello|hey|dear)\s+([A-Z][a-z]+)", re.M)
_GENERIC_GREETEES = {"there", "team", "all", "everyone", "folks"}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9']+")
_STOP = set(
    "the a an and or of to for with in on at by from is are was we you your our it this that as be can do how what "
    "who will would could should help".split()
)


class JudgeInput(BaseModel):
    source_facts: dict[str, Any] = Field(default_factory=dict)
    value_prop: str = ""
    subject: str = ""
    body: str
    draft_id: str | None = None
    attempt: int = 1


def _dollars(num: str, unit: str | None) -> float:
    n = float(num.replace(",", ""))
    u = (unit or "").lower()
    mult = {"k": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}.get(u, 1)
    return n * mult


def facts_text(source_facts: dict[str, Any]) -> str:
    return json.dumps(source_facts, default=str).lower()


def _fact_numbers(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in _NUM.findall(text)}


def find_unsupported_claims(inp: JudgeInput) -> list[str]:
    """Return human-readable unsupported claims; empty list => no hallucination."""
    full = f"{inp.subject}\n{inp.body}"
    ftext = facts_text(inp.source_facts)
    allowed = ftext + "\n" + inp.value_prop.lower()
    fnums = _fact_numbers(ftext)
    fdollars = set(fnums) | {_dollars(n, u) for n, u in _MONEY.findall(ftext)}
    claims: list[str] = []

    for m in _MONEY.finditer(full):
        amount = _dollars(m.group(1), m.group(2))
        if amount not in fdollars and not any(abs(amount - d) < 1 for d in fdollars):
            claims.append(f'amount "{m.group(0).strip()}" not in source facts')
    for m in _STAGE.finditer(full):
        if m.group(1).lower() not in allowed:
            claims.append(f'funding stage "{m.group(1)}" not in source facts')
    for m in _COUNT.finditer(full):
        if float(m.group(1).replace(",", "")) not in fnums:
            claims.append(f'count "{m.group(0)}" not in source facts')
    for m in _PCT.finditer(full):
        if m.group(0).replace(" ", "") not in allowed.replace(" ", ""):
            claims.append(f'percentage "{m.group(0)}" not in source facts')
    for term in TECH_VOCAB:
        if re.search(rf"\b{re.escape(term)}\b", full) and term.lower() not in allowed:
            claims.append(f'technology "{term}" not in source facts')
    for term in INVESTOR_VOCAB + TITLE_VOCAB:
        if re.search(rf"\b{re.escape(term)}\b", full) and term.lower() not in allowed:
            claims.append(f'"{term}" not in source facts')
    g = _GREETING.search(inp.body)
    if g and ftext != "{}" and g.group(1).lower() not in _GENERIC_GREETEES and g.group(1).lower() not in ftext:
        claims.append(f'recipient name "{g.group(1)}" not in source facts')
    return list(dict.fromkeys(claims))


def tone_score(subject: str, body: str) -> tuple[float, list[str]]:
    text = f"{subject}\n{body}".lower()
    score, notes = 1.0, []
    for phrase in SPAM_PHRASES:
        if phrase in text:
            score -= 0.25
            notes.append(f'spam phrase "{phrase}"')
    if text.count("!") > 1:
        score -= 0.25
        notes.append("excessive exclamation marks")
    for phrase in FAKE_FAMILIARITY:
        if phrase in text:
            score -= 0.25
            notes.append(f'fake familiarity "{phrase}"')
    if len(body.split()) > MAX_WORDS:
        score -= 0.25
        notes.append(f"body over {MAX_WORDS} words")
    return max(0.0, score), notes


def relevance_score(inp: JudgeInput) -> float:
    body = inp.body.lower()
    ftext = facts_text(inp.source_facts)
    score = 0.0
    # references a concrete fact (signal or tech) from the database
    leaves = [str(v).lower() for v in _walk(inp.source_facts) if isinstance(v, (str, int)) and len(str(v)) > 3]
    if any(leaf in body for leaf in leaves if leaf in ftext):
        score += 0.4
    vp_words = {w for w in _WORD.findall(inp.value_prop.lower()) if w not in _STOP and len(w) > 3}
    overlap = len(vp_words & set(_WORD.findall(body)))
    score += 0.3 if overlap >= 2 else 0.15 if overlap == 1 else 0.0
    if "?" in inp.body or re.search(r"\b(worth a|open to|happy to share)\b", body):
        score += 0.3
    return min(1.0, score)


def _walk(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def composite(h: float, t: float, r: float) -> float:
    return round(0.50 * h + 0.25 * t + 0.25 * r, 3)


def is_passed(h: float, comp: float) -> bool:
    return h == 1.0 and comp >= 0.85


def evaluate_deterministic(inp: JudgeInput) -> QAResult:
    claims = find_unsupported_claims(inp)
    h = 0.0 if claims else 1.0
    t, tone_notes = tone_score(inp.subject, inp.body)
    r = relevance_score(inp)
    comp = composite(h, t, r)
    critique = []
    if claims:
        critique.append("Unsupported claims: " + "; ".join(claims) + ". Remove them or use only SOURCE_FACTS.")
    if tone_notes:
        critique.append("Tone issues: " + "; ".join(tone_notes) + ".")
    if r < 0.7:
        critique.append("Relevance low: reference a concrete fact, tie it to the value prop, and end with a low-friction CTA.")
    return QAResult(
        hallucination_score=h,
        tone_score=t,
        relevance_score=r,
        composite_score=comp,
        passed=is_passed(h, comp),
        unsupported_claims=claims,
        critique=" ".join(critique) or "No issues.",
        rewrite_suggestion=None,
    )
