"""Needs docker services: postgres (seeded) + neo4j. Auto-skipped when they are down."""

import pytest
from gtm_core.graph import run_cypher
from sys5_lookalike_finder.graph_sync import sync_all, write_lookalike_edges
from sys5_lookalike_finder.similarity import PostgresStore, find_lookalikes

pytestmark = pytest.mark.integration


async def test_sync_postgres_to_neo4j_and_rank():
    n = await sync_all()
    assert n >= 200
    count = (await run_cypher("MATCH (a:Account) RETURN count(a) AS n"))[0]["n"]
    assert count >= 200
    store = PostgresStore()
    profiles = await store.all_profiles()
    seed = next(d for d, p in profiles.items() if p.is_customer)
    results = await find_lookalikes(seed, top_k=5, store=store)
    assert len(results) == 5 and results[0].breakdown
    await write_lookalike_edges(seed, results)
    edges = (await run_cypher("MATCH ()-[l:LOOKALIKE_OF]->(:Account {domain:$d}) RETURN count(l) AS n", d=seed))[0]["n"]
    assert edges == 5
