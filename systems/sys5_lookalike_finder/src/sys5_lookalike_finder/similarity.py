"""Hybrid lookalike ranker: graph overlap (Jaccard on tech/investors) + pgvector-style cosine.

The core `rank` is pure so it is fast to test; stores supply profiles from Postgres or memory.
Every result carries an explainable score breakdown.
"""

import os
from collections.abc import Iterable
from typing import Protocol

from gtm_core.embeddings import cosine, embed_text
from pydantic import BaseModel, Field

# Weights sum to 1.0
W_TECH, W_INVESTOR, W_INDUSTRY, W_CHAMPION, W_VECTOR = 0.40, 0.10, 0.15, 0.10, 0.25


class Profile(BaseModel):
    domain: str
    name: str = ""
    industry: str | None = None
    tech: set[str] = Field(default_factory=set)
    investors: set[str] = Field(default_factory=set)
    embedding: list[float] | None = None
    is_customer: bool = False
    ltv_usd: float | None = None
    champion_from: set[str] = Field(default_factory=set)  # customer domains a contact previously worked at


class Lookalike(BaseModel):
    domain: str
    name: str
    similarity_score: float
    shared_tech: list[str]
    shared_industry: bool
    shared_investors: list[str]
    champion_path: bool
    breakdown: dict[str, float]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def score_pair(seed: Profile, cand: Profile) -> Lookalike:
    tech = jaccard(seed.tech, cand.tech)
    inv = jaccard(seed.investors, cand.investors)
    ind = 1.0 if seed.industry and seed.industry == cand.industry else 0.0
    champ = 1.0 if seed.domain in cand.champion_from else 0.0
    vec = 0.0
    if seed.embedding and cand.embedding:
        vec = max(0.0, cosine(seed.embedding, cand.embedding))
    total = W_TECH * tech + W_INVESTOR * inv + W_INDUSTRY * ind + W_CHAMPION * champ + W_VECTOR * vec
    return Lookalike(
        domain=cand.domain,
        name=cand.name,
        similarity_score=round(total, 4),
        shared_tech=sorted(seed.tech & cand.tech),
        shared_industry=bool(ind),
        shared_investors=sorted(seed.investors & cand.investors),
        champion_path=bool(champ),
        breakdown={
            "tech_jaccard": round(tech, 4),
            "investor_jaccard": round(inv, 4),
            "industry_match": ind,
            "champion_path": champ,
            "vector_cosine": round(vec, 4),
        },
    )


def rank(
    seed: Profile,
    candidates: Iterable[Profile],
    top_k: int = 10,
    min_shared_tech: int = 0,
    exclude_customers: bool = True,
) -> list[Lookalike]:
    scored = []
    for c in candidates:
        if c.domain == seed.domain or (exclude_customers and c.is_customer):
            continue
        if len(seed.tech & c.tech) < min_shared_tech:
            continue
        scored.append(score_pair(seed, c))
    scored.sort(key=lambda r: (-r.similarity_score, r.domain))
    return scored[:top_k]


# ---- stores ---------------------------------------------------------------------------------
class ProfileStore(Protocol):
    async def all_profiles(self) -> dict[str, Profile]: ...


class MemoryStore:
    """Profiles built from the synthetic sandbox dataset (no database needed)."""

    async def all_profiles(self) -> dict[str, Profile]:
        from gtm_integrations.fixtures import dataset

        links = dataset.champion_links()
        return {
            a.domain: Profile(
                domain=a.domain,
                name=a.name,
                industry=a.industry,
                tech=set(a.tech_stack),
                investors=set(a.investors),
                embedding=embed_text(a.description or a.name),
                is_customer=a.is_customer,
                ltv_usd=a.ltv_usd,
                champion_from=set(links.get(a.domain, [])),
            )
            for a in dataset.accounts()
        }


class PostgresStore:
    async def all_profiles(self) -> dict[str, Profile]:
        from gtm_core.db import session_scope
        from gtm_core.models import Account
        from sqlalchemy import select

        async with session_scope() as s:
            rows = (await s.execute(select(Account))).scalars().all()
        from gtm_integrations.fixtures import dataset  # champion links live in the graph; sandbox uses the fixture

        links = dataset.champion_links()
        out = {}
        for a in rows:
            raw = a.firmographic_raw or {}
            emb = a.description_embedding
            out[a.domain] = Profile(
                domain=a.domain,
                name=a.name,
                industry=a.industry,
                tech=set(a.tech_stack or []),
                investors=set(raw.get("investors", [])),
                embedding=list(emb) if emb is not None else None,
                is_customer=a.is_customer,
                ltv_usd=float(a.ltv_usd) if a.ltv_usd is not None else None,
                champion_from=set(links.get(a.domain, [])),
            )
        return out


def default_store() -> ProfileStore:
    return MemoryStore() if os.environ.get("LOOKALIKE_STORE", "postgres") == "memory" else PostgresStore()


class SeedNotFoundError(LookupError):
    pass


async def find_lookalikes(
    seed_domain: str, top_k: int = 10, min_shared_tech: int = 0, store: ProfileStore | None = None
) -> list[Lookalike]:
    profiles = await (store or default_store()).all_profiles()
    seed = profiles.get(seed_domain)
    if seed is None:
        raise SeedNotFoundError(seed_domain)
    return rank(seed, profiles.values(), top_k=top_k, min_shared_tech=min_shared_tech)
