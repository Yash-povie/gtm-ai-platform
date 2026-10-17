"""Apply scripts/init_neo4j.cypher (constraints + indexes) to the configured Neo4j."""

import asyncio
from pathlib import Path

from gtm_core.graph import close_driver, get_driver


async def main() -> None:
    text = (Path(__file__).parent / "init_neo4j.cypher").read_text()
    stmts = [
        s.strip()
        for s in "\n".join(line for line in text.splitlines() if not line.strip().startswith("//")).split(";")
        if s.strip()
    ]
    async with get_driver().session() as session:
        for stmt in stmts:
            await session.run(stmt)
    await close_driver()
    print(f"applied {len(stmts)} neo4j statements")


if __name__ == "__main__":
    asyncio.run(main())
