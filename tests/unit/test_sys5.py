import pytest
from gtm_core.embeddings import embed_text
from sys5_lookalike_finder.graph_sync import build_rows
from sys5_lookalike_finder.similarity import (
    W_CHAMPION,
    W_INDUSTRY,
    W_INVESTOR,
    W_TECH,
    W_VECTOR,
    MemoryStore,
    Profile,
    SeedNotFoundError,
    find_lookalikes,
    jaccard,
    rank,
)


def prof(domain, tech, industry="Fintech", investors=(), desc="", customer=False, champ=()):
    return Profile(domain=domain, name=domain, industry=industry, tech=set(tech), investors=set(investors),
                   embedding=embed_text(desc or domain), is_customer=customer, champion_from=set(champ))


def test_weights_sum_to_one():
    assert W_TECH + W_INVESTOR + W_INDUSTRY + W_CHAMPION + W_VECTOR == pytest.approx(1.0)


def test_jaccard_edges():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


def test_identical_profile_outranks_disjoint_and_is_explainable():
    seed = prof("seed.example", ["AWS", "Snowflake"], investors=["Accel"], desc="fintech aws snowflake")
    twin = prof("twin.example", ["AWS", "Snowflake"], investors=["Accel"], desc="fintech aws snowflake")
    far = prof("far.example", ["Okta"], industry="Logistics", desc="trucks and warehouses")
    out = rank(seed, [far, twin], top_k=5)
    assert [r.domain for r in out] == ["twin.example", "far.example"]
    assert out[0].shared_tech == ["AWS", "Snowflake"] and out[0].shared_industry
    assert out[0].similarity_score >= 0.9 and out[0].breakdown["tech_jaccard"] == 1.0


def test_filters_seed_customers_and_min_shared_tech():
    seed = prof("s.example", ["AWS", "GCP"])
    cands = [prof("c.example", ["AWS"], customer=True), prof("a.example", ["AWS"]), prof("b.example", ["AWS", "GCP"])]
    assert [r.domain for r in rank(seed, [seed, *cands])] == ["b.example", "a.example"]
    assert [r.domain for r in rank(seed, cands, min_shared_tech=2)] == ["b.example"]
    assert len(rank(seed, cands, exclude_customers=False)) == 3


def test_champion_path_boosts_rank():
    seed = prof("cust.example", ["AWS"])
    plain = prof("plain.example", ["AWS"])
    champ = prof("champ.example", ["AWS"], champ=["cust.example"])
    out = rank(seed, [plain, champ])
    assert out[0].domain == "champ.example" and out[0].champion_path


async def test_find_lookalikes_over_sandbox_dataset():
    store = MemoryStore()
    profiles = await store.all_profiles()
    seed = next(d for d, p in profiles.items() if p.is_customer)
    out = await find_lookalikes(seed, top_k=10, store=store)
    assert len(out) == 10
    assert all(not profiles[r.domain].is_customer for r in out)
    assert out == sorted(out, key=lambda r: (-r.similarity_score, r.domain))
    with pytest.raises(SeedNotFoundError):
        await find_lookalikes("nope.example", store=store)


def test_graph_rows_transform():
    rows = build_rows(
        [{"id": "1", "domain": "a.example", "name": "A", "tech_stack": ["AWS"], "investors": ["Accel"], "is_customer": True,
          "ltv_usd": 100}],
        [{"id": "c1", "email": "x@a.example", "domain": "a.example"}],
        {"b.example": ["a.example"]},
    )
    assert rows["accounts"][0]["industry"] == "Unknown" and rows["accounts"][0]["ltv_usd"] == 100.0
    assert rows["tech"] == [{"domain": "a.example", "tech": "AWS"}]
    assert rows["champions"] == [{"prospect": "b.example", "customer": "a.example"}]
