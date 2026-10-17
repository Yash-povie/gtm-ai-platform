"""Build tests/evals/golden_emails.json: 25 clean + 15 hallucinated + 10 spammy emails (deterministic)."""

import json
import random
from pathlib import Path

from gtm_integrations.fixtures import dataset

VALUE_PROP = "We help engineering teams cut cloud costs with automated spend optimization."
INVESTORS = ["Sequoia", "Accel", "Greylock", "Lightspeed", "Tiger Global"]
TECHS = ["AWS", "GCP", "Azure", "Snowflake", "Datadog", "Kubernetes", "Terraform", "Kafka", "Databricks", "Okta"]


def facts_for(acct, contact, signal) -> dict:
    return {
        "account": {
            "name": acct.name,
            "industry": acct.industry,
            "employee_count": acct.employee_count,
            "funding_stage": acct.funding_stage,
            "funding_amount_usd": acct.funding_amount_usd,
            "investors": acct.investors,
            "tech_stack": acct.tech_stack,
        },
        "contact": {"first_name": contact.first_name, "title": contact.title},
        "signal": signal,
    }


def clean_email(acct, contact, signal, rng) -> tuple[str, str]:
    t1, t2 = acct.tech_stack[0], acct.tech_stack[1]
    money = f"${acct.funding_amount_usd // 1_000_000}M"
    if signal["type"] == "hiring_spike":
        hook = f"I saw {acct.name} is hiring {signal['payload']['open_roles']} engineers."
    else:
        hook = f"Congrats on the {acct.funding_stage} round of {money}."
    body = (
        f"Hi {contact.first_name},\n\n{hook} With {t1} and {t2} in your stack, cloud spend tends to climb fast. "
        f"{VALUE_PROP}\n\nWorth a 15-minute chat next week?\n\nBest,\nSam"
    )
    return f"{acct.name} and cloud costs", body


def main() -> None:
    rng = random.Random(7)
    accts = [a for a in dataset.accounts() if a.funding_stage not in ("Public",)]
    records = []
    for i in range(50):
        acct = accts[i]
        contact = dataset.contacts()[acct.domain][0]
        if i % 2 == 0:
            signal = {"type": "hiring_spike", "payload": {"open_roles": rng.randint(4, 12)}}
        else:
            signal = {"type": "funding_round", "payload": {"round": acct.funding_stage}}
        facts = facts_for(acct, contact, signal)
        subject, body = clean_email(acct, contact, signal, rng)
        rec = {"id": f"g{i:02d}", "label": "clean", "planted_claim": None, "value_prop": VALUE_PROP}

        if i >= 25 and i < 40:  # 15 hallucinations, 5 kinds x 3
            kind = (i - 25) % 5
            if kind == 0:  # wrong funding amount
                wrong = f"${acct.funding_amount_usd // 1_000_000 + 25}M"
                body = body.replace(f"${acct.funding_amount_usd // 1_000_000}M", wrong)
                if wrong not in body:  # hiring-spike variant has no amount line
                    body = body.replace("Worth a", f"Congrats on your {wrong} raise. Worth a")
                rec["planted_claim"] = wrong
            elif kind == 1:  # invented technology
                bad = next(t for t in TECHS if t not in acct.tech_stack)
                body = body.replace("cloud spend", f"your {bad} bill and cloud spend", 1)
                rec["planted_claim"] = bad
            elif kind == 2:  # invented investor
                bad = next(v for v in INVESTORS if v not in acct.investors)
                body = body.replace("Worth a", f"Great to see {bad} backing you. Worth a")
                rec["planted_claim"] = bad
            elif kind == 3:  # invented headcount of open roles
                body = body.replace("Worth a", "I noticed your 47 open roles. Worth a")
                rec["planted_claim"] = "47 open roles"
            else:  # wrong recipient name
                body = body.replace(f"Hi {contact.first_name}", "Hi Jonathan", 1)
                rec["planted_claim"] = "Jonathan"
            rec["label"] = "hallucination"
        elif i >= 40:  # 10 spammy (>=3 tone violations each => fails composite)
            body = (
                f"Hi {contact.first_name},\n\nI hope this finds you well!! Just circling back, my friend. "
                f"{VALUE_PROP} This is a game-changer and pure synergy for {acct.name}! "
                + "Act now while this limited time offer lasts. " * 6
                + "\n\nWorth a chat?"
            )
            rec["label"] = "spam"
        rec.update(source_facts=facts, subject=subject, body=body)
        records.append(rec)
    out = Path(__file__).parent / "golden_emails.json"
    out.write_text(json.dumps(records, indent=2))
    print(f"wrote {len(records)} records to {out}")


if __name__ == "__main__":
    main()
