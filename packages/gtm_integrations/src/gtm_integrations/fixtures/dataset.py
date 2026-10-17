"""Deterministic synthetic GTM dataset (200 accounts) used by all sandbox mocks.

All company names are fictional and all domains use the reserved `.example` TLD, so a
sandbox email can never reach a real inbox.
"""

import random
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from gtm_core.schemas import AccountData, ContactData

SEED = 42
N_ACCOUNTS = 200

INDUSTRIES = ["Fintech", "DevTools", "Healthtech", "E-commerce", "Cybersecurity", "Data & AI", "Logistics"]
TECH_POOL = [
    "AWS", "GCP", "Azure", "Snowflake", "Datadog", "Kubernetes", "Terraform", "Postgres",
    "Kafka", "dbt", "Segment", "Salesforce", "HubSpot", "Stripe", "Okta", "Databricks",
]
INVESTORS = ["Sequoia", "a16z", "Accel", "Index", "Lightspeed", "Tiger Global", "Insight", "Greylock"]
STAGES = ["Seed", "Series A", "Series B", "Series C", "Series D", "Public"]
COUNTRIES = ["US", "US", "US", "CA", "GB", "DE", "IN"]
_PREFIX = ["Nimbus", "Vertex", "Quanta", "Helio", "Bright", "Apex", "Lumen", "Orbit", "Cobalt", "Fable",
           "Tidal", "Ember", "Zenith", "Argo", "Pivot", "Sable", "Kestrel", "Mosaic", "Halcyon", "Ironclad"]
_SUFFIX = ["Labs", "Systems", "Cloud", "AI", "Pay", "Health", "Secure", "Data", "Works", "Grid"]
_FIRST = ["Ava", "Liam", "Noah", "Maya", "Ethan", "Zoe", "Arjun", "Priya", "Lucas", "Nina", "Omar", "Sara"]
_LAST = ["Chen", "Patel", "Garcia", "Kim", "Nguyen", "Rossi", "Singh", "Muller", "Okafor", "Silva"]
_TITLES = [
    ("VP of Engineering", "vp", "engineering"),
    ("Head of Data", "head", "data"),
    ("CTO", "c_suite", "engineering"),
    ("Director of Sales", "director", "sales"),
    ("VP of Marketing", "vp", "marketing"),
]
_CUSTOMER_DOMAINS_NAMED = {
    0: ("Stripelike Pay", "stripelike.example"),
    1: ("Datawatch Cloud", "datawatch.example"),
    2: ("Snowfield Data", "snowfield.example"),
}
_SIGNAL_TYPES = ["job_change", "funding_round", "hiring_spike", "tech_added", "champion_moved"]


@lru_cache
def accounts() -> list[AccountData]:
    rng = random.Random(SEED)
    out: list[AccountData] = []
    used: set[str] = set()
    for i in range(N_ACCOUNTS):
        if i in _CUSTOMER_DOMAINS_NAMED:
            name, domain = _CUSTOMER_DOMAINS_NAMED[i]
        else:
            while True:
                name = f"{rng.choice(_PREFIX)} {rng.choice(_SUFFIX)}"
                domain = name.lower().replace(" ", "") + ".example"
                if domain not in used:
                    break
        used.add(domain)
        industry = rng.choice(INDUSTRIES)
        employees = rng.choice([25, 60, 120, 250, 400, 800, 1500, 4000])
        stage = rng.choice(STAGES)
        tech = sorted(rng.sample(TECH_POOL, rng.randint(3, 7)))
        investors = sorted(rng.sample(INVESTORS, rng.randint(1, 3)))
        is_customer = i < 20
        out.append(
            AccountData(
                domain=domain,
                name=name,
                industry=industry,
                employee_count=employees,
                annual_revenue_usd=employees * rng.randint(80_000, 250_000),
                funding_stage=stage,
                hq_country=rng.choice(COUNTRIES),
                tech_stack=tech,
                investors=investors,
                funding_amount_usd=rng.choice([5, 15, 40, 80, 150]) * 1_000_000,
                description=f"{name} is a {stage} {industry} company using {', '.join(tech[:3])}.",
                is_customer=is_customer,
                ltv_usd=float(rng.randint(60, 400) * 1000) if is_customer else None,
            )
        )
    return out


@lru_cache
def contacts() -> dict[str, list[ContactData]]:
    rng = random.Random(SEED + 1)
    result: dict[str, list[ContactData]] = {}
    for acc in accounts():
        people: list[ContactData] = []
        seen: set[str] = set()
        for title, seniority, dept in rng.sample(_TITLES, 3):
            while True:
                first, last = rng.choice(_FIRST), rng.choice(_LAST)
                slug = f"{first}.{last}".lower()
                if slug not in seen:
                    seen.add(slug)
                    break
            people.append(
                ContactData(
                    email=f"{slug}@{acc.domain}",
                    first_name=first,
                    last_name=last,
                    title=title,
                    seniority=seniority,
                    department=dept,
                    linkedin_url=f"https://www.linkedin.com/in/{slug}-{acc.domain.split('.')[0]}",
                    apollo_person_id=f"apl_{acc.domain.split('.')[0]}_{slug}",
                    email_status="verified" if rng.random() > 0.15 else "catch_all",
                    company_domain=acc.domain,
                )
            )
        result[acc.domain] = people
    return result


@lru_cache
def champion_links() -> dict[str, list[str]]:
    """prospect domain -> customer domains where one of its contacts previously worked (champion path)."""
    rng = random.Random(SEED + 3)
    customers = [a.domain for a in accounts() if a.is_customer]
    prospects = [a.domain for a in accounts() if not a.is_customer]
    return {d: [rng.choice(customers)] for d in rng.sample(prospects, 60)}


def signals(now: datetime | None = None) -> list[dict]:
    """~120 deterministic raw signal events relative to `now`."""
    now = now or datetime(2026, 1, 1, tzinfo=UTC)
    rng = random.Random(SEED + 2)
    events = []
    for acc in rng.sample(accounts(), 120):
        stype = rng.choice(_SIGNAL_TYPES)
        events.append(
            {
                "domain": acc.domain,
                "signal_type": stype,
                "source": "n8n_scraper" if stype in ("hiring_spike", "tech_added") else "clay",
                "occurred_at": (now - timedelta(days=rng.randint(0, 20))).isoformat(),
                "payload": _signal_payload(stype, rng),
            }
        )
    return events


def _signal_payload(stype: str, rng: random.Random) -> dict:
    if stype == "hiring_spike":
        return {"open_roles": rng.randint(4, 12), "department": "engineering"}
    if stype == "funding_round":
        return {"round": rng.choice(["Series A", "Series B"]), "amount_usd": rng.choice([15, 40]) * 1_000_000}
    if stype in ("job_change", "champion_moved"):
        return {"new_title": "VP of Engineering", "previous_company": "Prior Co"}
    return {"technology": rng.choice(TECH_POOL)}
