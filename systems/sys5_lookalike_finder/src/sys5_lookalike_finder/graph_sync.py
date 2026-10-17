"""Postgres -> Neo4j entity and relationship sync (idempotent MERGE)."""

from typing import Any

from gtm_core.graph import get_driver, run_cypher


def build_rows(accounts: list[dict[str, Any]], contacts: list[dict[str, Any]], champion_links: dict[str, list[str]]) -> dict:
    """Pure transform of relational rows into UNWIND-ready parameter lists."""
    acct_rows, tech_rows, inv_rows = [], [], []
    for a in accounts:
        acct_rows.append(
            {
                "domain": a["domain"],
                "id": str(a["id"]),
                "name": a["name"],
                "industry": a.get("industry") or "Unknown",
                "employee_count": a.get("employee_count") or 0,
                "is_customer": bool(a.get("is_customer")),
                "ltv_usd": float(a["ltv_usd"]) if a.get("ltv_usd") is not None else 0.0,
                "icp_score": float(a.get("icp_score") or 0),
            }
        )
        tech_rows += [{"domain": a["domain"], "tech": t} for t in (a.get("tech_stack") or [])]
        inv_rows += [{"domain": a["domain"], "investor": i} for i in (a.get("investors") or [])]
    contact_rows = [
        {"id": str(c["id"]), "email": c["email"], "seniority": c.get("seniority") or "", "domain": c["domain"]}
        for c in contacts
    ]
    champ_rows = [{"prospect": p, "customer": c} for p, cs in champion_links.items() for c in cs]
    return {"accounts": acct_rows, "tech": tech_rows, "investors": inv_rows, "contacts": contact_rows, "champions": champ_rows}


Q_ACCOUNTS = """
UNWIND $rows AS r
MERGE (a:Account {domain: r.domain})
SET a.id = r.id, a.name = r.name, a.employee_count = r.employee_count,
    a.is_customer = r.is_customer, a.ltv_usd = r.ltv_usd, a.icp_score = r.icp_score
MERGE (i:Industry {name: r.industry})
MERGE (a)-[:IN_INDUSTRY]->(i)
"""
Q_TECH = """
UNWIND $rows AS r
MATCH (a:Account {domain: r.domain})
MERGE (t:Technology {name: r.tech})
MERGE (a)-[u:USES_TECH]->(t)
ON CREATE SET u.detected_at = datetime(), u.confidence = 0.9
"""
Q_INVESTORS = """
UNWIND $rows AS r
MATCH (a:Account {domain: r.domain})
MERGE (inv:Investor {name: r.investor})
MERGE (a)-[:BACKED_BY]->(inv)
"""
Q_CONTACTS = """
UNWIND $rows AS r
MATCH (a:Account {domain: r.domain})
MERGE (c:Contact {id: r.id})
SET c.email = r.email, c.seniority = r.seniority
MERGE (c)-[w:WORKS_AT]->(a)
ON CREATE SET w.is_current = true
"""
Q_CHAMPIONS = """
UNWIND $rows AS r
MATCH (p:Account {domain: r.prospect}), (cust:Account {domain: r.customer})
MATCH (c:Contact)-[:WORKS_AT]->(p)
WITH r, cust, c ORDER BY c.id
WITH r, cust, collect(c)[0] AS champion
MERGE (champion)-[pa:PREVIOUSLY_AT]->(cust)
ON CREATE SET pa.was_champion = true, pa.left_at = datetime()
"""
Q_LOOKALIKE_EDGES = """
UNWIND $rows AS r
MATCH (s:Account {domain: r.seed}), (c:Account {domain: r.domain})
MERGE (c)-[l:LOOKALIKE_OF]->(s)
SET l.similarity_score = r.score, l.shared_tech_count = r.shared, l.updated_at = datetime()
"""


async def write_graph(rows: dict) -> None:
    async with get_driver().session() as session:
        for query, key in (
            (Q_ACCOUNTS, "accounts"),
            (Q_TECH, "tech"),
            (Q_INVESTORS, "investors"),
            (Q_CONTACTS, "contacts"),
            (Q_CHAMPIONS, "champions"),
        ):
            if rows[key]:
                await session.run(query, rows=rows[key])


async def sync_all() -> int:
    """Read accounts/contacts from Postgres and MERGE them into Neo4j. Returns accounts synced."""
    from gtm_core.db import session_scope
    from gtm_core.models import Account, Contact
    from gtm_integrations.fixtures import dataset
    from sqlalchemy import select

    async with session_scope() as s:
        accts = (await s.execute(select(Account))).scalars().all()
        contacts = (await s.execute(select(Contact, Account.domain).join(Account, Contact.account_id == Account.id))).all()
    acct_dicts = [
        {
            "id": a.id, "domain": a.domain, "name": a.name, "industry": a.industry, "employee_count": a.employee_count,
            "is_customer": a.is_customer, "ltv_usd": a.ltv_usd, "icp_score": a.icp_score, "tech_stack": a.tech_stack,
            "investors": (a.firmographic_raw or {}).get("investors", []),
        }
        for a in accts
    ]
    contact_dicts = [{"id": c.id, "email": c.email, "seniority": c.seniority, "domain": d} for c, d in contacts]
    await write_graph(build_rows(acct_dicts, contact_dicts, dataset.champion_links()))
    return len(acct_dicts)


async def write_lookalike_edges(seed_domain: str, results: list) -> None:
    rows = [{"seed": seed_domain, "domain": r.domain, "score": r.similarity_score, "shared": len(r.shared_tech)} for r in results]
    await run_cypher(Q_LOOKALIKE_EDGES, rows=rows)
