"""FastAPI endpoints: /api/v1/lookalike/*"""

from fastapi import APIRouter, HTTPException, Query

from sys5_lookalike_finder.similarity import Lookalike, SeedNotFoundError, find_lookalikes

router = APIRouter(prefix="/api/v1/lookalike", tags=["lookalike"])


@router.get("/rank", response_model=list[Lookalike])
async def rank_endpoint(
    seed_domain: str, top_k: int = Query(10, ge=1, le=100), min_shared_tech: int = Query(0, ge=0)
) -> list[Lookalike]:
    try:
        return await find_lookalikes(seed_domain, top_k=top_k, min_shared_tech=min_shared_tech)
    except SeedNotFoundError as e:
        raise HTTPException(404, f"unknown seed domain: {e}") from e
