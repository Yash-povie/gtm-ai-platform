"""Versioned prompts, ICP parser and email drafter (Claude when keyed, deterministic otherwise)."""

import json
import re
from typing import Any

from anthropic import AsyncAnthropic
from gtm_core.config import get_settings
from pydantic import BaseModel, Field

PROMPT_NAME = "sdr_email"
PROMPT_VERSION = "v1"
DEFAULT_VALUE_PROP = "We help engineering teams cut cloud costs with automated spend optimization."

ICP_SYSTEM = (
    "Translate a plain-English ideal-customer-profile into JSON with keys: industries (list), funding_stages (list), "
    "tech_keywords (list), headcount_min (int), headcount_max (int), titles (list of decision-maker titles). "
    "Return ONLY JSON."
)
DRAFT_SYSTEM = (
    "You write concise B2B cold emails (<=110 words). Cite ONLY facts present in FACTS - never invent funding, "
    "headcount, technologies, investors or names. No spam phrases or exclamation-mark overuse. End with one "
    "low-friction question. Return ONLY JSON: {\"subject\": str, \"body\": str, \"facts_used\": [str]}."
)
DRAFT_USER = "FACTS:\n{facts}\n\nVALUE_PROP: {value_prop}\n\nPREVIOUS_CRITIQUE: {critique}\n\nWrite the email."

INDUSTRY_ALIASES = {
    "fintech": "Fintech", "devtool": "DevTools", "developer tool": "DevTools", "healthtech": "Healthtech",
    "health tech": "Healthtech", "e-commerce": "E-commerce", "ecommerce": "E-commerce", "cybersecurity": "Cybersecurity",
    "security": "Cybersecurity", "data & ai": "Data & AI", "data and ai": "Data & AI", "logistics": "Logistics",
}
TECH = ["AWS", "GCP", "Azure", "Snowflake", "Datadog", "Kubernetes", "Terraform", "Postgres", "Kafka", "dbt",
        "Segment", "Salesforce", "HubSpot", "Stripe", "Okta", "Databricks"]
TITLES = ["VP of Engineering", "Head of Data", "CTO", "Director of Sales", "VP of Marketing"]
DEFAULT_TITLES = ["VP of Engineering", "Head of Data", "CTO"]


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if m is None:
        raise ValueError("model returned no JSON")
    return json.loads(m.group(0))


class IcpFilters(BaseModel):
    industries: list[str] = Field(default_factory=list)
    funding_stages: list[str] = Field(default_factory=list)
    tech_keywords: list[str] = Field(default_factory=list)
    headcount_min: int = 0
    headcount_max: int = 10**9
    titles: list[str] = Field(default_factory=lambda: list(DEFAULT_TITLES))


def parse_icp_deterministic(prompt: str) -> IcpFilters:
    low = prompt.lower()
    f = IcpFilters()
    f.industries = sorted({v for k, v in INDUSTRY_ALIASES.items() if k in low})
    f.funding_stages = sorted({m.group(0).title() for m in re.finditer(r"series [a-f]|seed", low)})
    f.tech_keywords = [t for t in TECH if re.search(rf"\b{re.escape(t.lower())}\b", low)]
    if m := re.search(r"(\d[\d,]*)\s*(?:to|-|and)\s*(\d[\d,]*)\s*(?:employees|people|headcount)", low):
        f.headcount_min, f.headcount_max = (int(g.replace(",", "")) for g in m.groups())
    elif m := re.search(r"(?:over|more than|at least|>)\s*(\d[\d,]*)\s*(?:employees|people)", low):
        f.headcount_min = int(m.group(1).replace(",", ""))
    elif m := re.search(r"(?:under|fewer than|less than|at most)\s*(\d[\d,]*)\s*(?:employees|people)", low):
        f.headcount_max = int(m.group(1).replace(",", ""))
    found_titles = [t for t in TITLES if t.lower() in low]
    if found_titles:
        f.titles = found_titles
    return f


class IcpParser:
    def __init__(self, use_llm: bool | None = None, client: AsyncAnthropic | None = None):
        s = get_settings()
        self.use_llm = (s.anthropic_api_key not in ("", "mock")) if use_llm is None else use_llm
        self._client = client

    async def parse(self, prompt: str) -> dict:
        if self.use_llm:
            try:
                client = self._client or AsyncAnthropic(api_key=get_settings().anthropic_api_key)
                resp = await client.messages.create(
                    model=get_settings().llm_primary_model, max_tokens=400, system=ICP_SYSTEM,
                    messages=[{"role": "user", "content": prompt}])
                text = "".join(getattr(b, "text", "") for b in resp.content)
                return IcpFilters.model_validate(_json_from(text)).model_dump()
            except Exception:  # fall through to deterministic parse
                pass
        return parse_icp_deterministic(prompt).model_dump()


class DraftOut(BaseModel):
    subject: str
    body: str
    facts_used: list[str]


def build_source_facts(account: dict, contact: dict, signal: dict | list | None) -> dict[str, Any]:
    raw = account.get("firmographic_raw") or {}
    facts: dict[str, Any] = {
        "account": {
            "name": account["name"], "industry": account.get("industry"),
            "employee_count": account.get("employee_count"), "funding_stage": account.get("funding_stage"),
            "funding_amount_usd": raw.get("funding_amount_usd"), "investors": raw.get("investors", []),
            "tech_stack": account.get("tech_stack", []),
        },
        "contact": {"first_name": contact.get("first_name"), "title": contact.get("title")},
    }
    signals = [signal] if isinstance(signal, dict) else list(signal or [])
    if signals:
        facts["signals"] = [{"type": s["signal_type"], "payload": s.get("raw_payload", {})} for s in signals]
        facts["signal"] = facts["signals"][0]
    return facts


class Drafter:
    """LLM drafter when keyed; otherwise a deterministic template that only states facts it was given."""

    def __init__(self, use_llm: bool | None = None, client: AsyncAnthropic | None = None):
        s = get_settings()
        self.use_llm = (s.anthropic_api_key not in ("", "mock")) if use_llm is None else use_llm
        self._client = client

    async def draft(self, *, facts: dict, value_prop: str, critique: str | None = None, attempt: int = 1) -> DraftOut:
        if self.use_llm:
            try:
                return await self._llm(facts, value_prop, critique)
            except Exception:
                pass
        return self._template(facts, value_prop)

    async def _llm(self, facts: dict, value_prop: str, critique: str | None) -> DraftOut:
        client = self._client or AsyncAnthropic(api_key=get_settings().anthropic_api_key)
        resp = await client.messages.create(
            model=get_settings().llm_primary_model, max_tokens=700, system=DRAFT_SYSTEM,
            messages=[{"role": "user", "content": DRAFT_USER.format(
                facts=json.dumps(facts, default=str, indent=2), value_prop=value_prop, critique=critique or "none")}])
        text = "".join(getattr(b, "text", "") for b in resp.content)
        return DraftOut.model_validate(_json_from(text))

    @staticmethod
    def _template(facts: dict, value_prop: str) -> DraftOut:
        a, c = facts["account"], facts["contact"]
        sigs = facts.get("signals") or ([facts["signal"]] if facts.get("signal") else [])
        techs = a.get("tech_stack") or []
        used = [f"company: {a['name']}"]
        hooks: list[str] = []
        for sig in sigs[:2]:
            if sig["type"] == "hiring_spike" and sig["payload"].get("open_roles"):
                n = sig["payload"]["open_roles"]
                hooks.append(f"I saw {a['name']} is hiring {n} engineers.")
                used.append(f"hiring_spike: {n} open roles")
            elif sig["type"] == "funding_round" and a.get("funding_stage"):
                hooks.append(f"Congrats on the {a['funding_stage']} round.")
                used.append(f"funding_stage: {a['funding_stage']}")
            elif sig["type"] in ("champion_moved", "job_change") and sig["payload"].get("new_title"):
                hooks.append(f"Congrats on the new {sig['payload']['new_title']} role at {a['name']}.")
                used.append(f"new_title: {sig['payload']['new_title']}")
        if not hooks:
            hooks.append(f"I noticed {a['name']} is growing in {a.get('industry') or 'your space'}.")
            used.append(f"industry: {a.get('industry')}")
        hook = " ".join(hooks)
        if len(techs) >= 2:
            tech_line = f" With {techs[0]} and {techs[1]} in your stack, cloud spend tends to climb fast."
            used.append(f"tech_stack: {techs[0]}, {techs[1]}")
        else:
            tech_line = " Cloud spend tends to climb fast as teams scale."
        first = c.get("first_name") or "there"
        body = f"Hi {first},\n\n{hook}{tech_line} {value_prop}\n\nWorth a 15-minute chat next week?\n\nBest,\nSam"
        return DraftOut(subject=f"{a['name']} and cloud costs", body=body, facts_used=used)
