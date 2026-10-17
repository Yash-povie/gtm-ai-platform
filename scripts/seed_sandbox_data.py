"""Seed 200 synthetic accounts, contacts, signals, prompt versions, suppression entries.

Usage: uv run python scripts/seed_sandbox_data.py [--skip-graph] [--dry-run]
"""

import argparse
import asyncio
import hashlib
from datetime import datetime

from gtm_core.db import dispose_engine, session_scope
from gtm_core.embeddings import embed_text
from gtm_core.models import Account, Contact, PromptVersion, Signal, SuppressionEntry
from gtm_integrations.fixtures import dataset
from sqlalchemy.dialects.postgresql import insert

SDR_PROMPT_SYSTEM = (
    "You write concise (<=110 words) B2B cold emails. Cite ONLY facts in FACTS. "
    "One low-friction CTA. No spam phrases."
)
SDR_PROMPT_USER = "FACTS:\n{facts}\n\nVALUE_PROP: {value_prop}\n\nWrite subject and body."


def signal_strength(stype: str) -> float:
    return {"funding_round": 80, "champion_moved": 85, "job_change": 70, "hiring_spike": 65, "tech_added": 55}.get(
        stype, 50
    )


async def seed(skip_graph: bool) -> dict[str, int]:
    accts = dataset.accounts()
    async with session_scope() as s:
        for a in accts:
            vals = {
                "domain": a.domain,
                "name": a.name,
                "industry": a.industry,
                "employee_count": a.employee_count,
                "annual_revenue_usd": a.annual_revenue_usd,
                "funding_stage": a.funding_stage,
                "hq_country": a.hq_country,
                "tech_stack": a.tech_stack,
                "firmographic_raw": {"investors": a.investors, "funding_amount_usd": a.funding_amount_usd,
                                     "description": a.description},
                "is_customer": a.is_customer,
                "ltv_usd": a.ltv_usd,
                "description_embedding": embed_text(a.description or a.name),
            }
            stmt = insert(Account).values(**vals).on_conflict_do_update(
                index_elements=["domain"], set_={k: v for k, v in vals.items() if k != "domain"}
            )
            await s.execute(stmt)
        await s.flush()
        ids = {row.domain: row.id for row in (await s.execute(Account.__table__.select())).all()}

        n_contacts = 0
        for domain, people in dataset.contacts().items():
            for c in people:
                vals = c.model_dump(exclude={"company_domain"})
                vals["account_id"] = ids[domain]
                await s.execute(insert(Contact).values(**vals).on_conflict_do_nothing(index_elements=["email"]))
                n_contacts += 1

        n_signals = 0
        for e in dataset.signals():
            key = hashlib.sha256(f"seed|{e['domain']}|{e['signal_type']}".encode()).hexdigest()[:32]  # stable across re-seeds
            strength = signal_strength(e["signal_type"])
            await s.execute(
                insert(Signal)
                .values(
                    account_id=ids[e["domain"]],
                    signal_type=e["signal_type"],
                    source=e["source"],
                    raw_payload=e["payload"],
                    signal_strength=strength,
                    decayed_score=strength,
                    idempotency_key=f"seed:{key}",
                    occurred_at=datetime.fromisoformat(e["occurred_at"]),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            n_signals += 1

        await s.execute(
            insert(PromptVersion)
            .values(prompt_name="sdr_email", version_tag="v1", system_prompt=SDR_PROMPT_SYSTEM,
                    user_template=SDR_PROMPT_USER, model_name="claude-sonnet-5-5")
            .on_conflict_do_nothing(index_elements=["prompt_name", "version_tag"])
        )
        for value, typ, reason in [
            ("competitor-one.example", "competitor", "known competitor"),
            ("stripelike.example", "current_customer", "existing customer - do not prospect"),
        ]:
            await s.execute(
                insert(SuppressionEntry).values(email_or_domain=value, type=typ, reason=reason)
                .on_conflict_do_nothing(index_elements=["email_or_domain"])
            )

    graph_nodes = 0
    if not skip_graph:
        try:
            from sys5_lookalike_finder.graph_sync import sync_all

            graph_nodes = await sync_all()
        except ImportError:
            print("sys5 graph_sync not available yet; skipping Neo4j sync")
    from gtm_core.graph import close_driver

    await close_driver()
    await dispose_engine()
    return {"accounts": len(accts), "contacts": n_contacts, "signals": n_signals, "graph_accounts": graph_nodes}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-graph", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print dataset counts without touching any DB")
    args = p.parse_args()
    if args.dry_run:
        print({"accounts": len(dataset.accounts()), "contacts": sum(map(len, dataset.contacts().values())),
               "signals": len(dataset.signals())})
        return
    print(asyncio.run(seed(args.skip_graph)))


if __name__ == "__main__":
    main()
