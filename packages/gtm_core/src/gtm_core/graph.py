"""Async Neo4j driver wrapper."""

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from gtm_core.config import get_settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        s = get_settings()
        _driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    return _driver


async def run_cypher(query: str, **params: Any) -> list[dict[str, Any]]:
    async with get_driver().session() as session:
        result = await session.run(query, **params)
        return [rec.data() async for rec in result]


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
