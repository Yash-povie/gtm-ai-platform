"""Service container for the MCP tools (shared deps with Systems 1/5)."""

from dataclasses import dataclass

from gtm_core.config import Settings
from gtm_core.repo import MemoryRepo, Repo
from gtm_integrations.fixtures import dataset
from sys1_autonomous_sdr.graph import SdrRunner, build_deps
from sys1_autonomous_sdr.state import SdrDeps
from sys5_lookalike_finder.similarity import MemoryStore, PostgresStore, ProfileStore


@dataclass
class Services:
    deps: SdrDeps
    runner: SdrRunner
    store: ProfileStore

    @property
    def repo(self) -> Repo:
        return self.deps.repo


async def seed_memory_repo(repo: MemoryRepo) -> None:
    """Load the synthetic sandbox dataset so the MCP server is explorable without a database."""
    from datetime import datetime

    for a in dataset.accounts():
        rec = await repo.upsert_account(a)
        for c in dataset.contacts()[a.domain]:
            await repo.upsert_contact(c, rec["id"])
    for e in dataset.signals():
        acct = await repo.get_account(e["domain"])
        from sys2_signal_engine.scorer import base_strength, decay

        strength = base_strength(e["signal_type"], e["payload"])
        occurred = datetime.fromisoformat(e["occurred_at"])
        await repo.add_signal(account_id=acct["id"], contact_id=None, signal_type=e["signal_type"], source=e["source"],
                              payload=e["payload"], strength=strength, decayed=decay(strength, occurred),
                              key=f"seed:{e['domain']}:{e['signal_type']}:{e['occurred_at']}", occurred_at=occurred)


async def build_services(repo: Repo | None = None, settings: Settings | None = None, redis=None,
                         seed: bool = True, **overrides) -> Services:
    in_memory = repo is None or isinstance(repo, MemoryRepo)
    repo = repo or MemoryRepo()
    if in_memory and seed and isinstance(repo, MemoryRepo) and not repo.accounts:
        await seed_memory_repo(repo)
    deps = build_deps(repo=repo, settings=settings, redis=redis, **overrides)
    return Services(deps=deps, runner=SdrRunner(deps), store=MemoryStore() if in_memory else PostgresStore())
