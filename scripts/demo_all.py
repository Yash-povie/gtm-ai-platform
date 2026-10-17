"""Run all five system demos end-to-end in sandbox mode and print MEASURED numbers.

No docker needed: in-memory repo + fakeredis (falls back automatically). `make demo-all`
"""

import asyncio
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("GTM_ENV", "sandbox")


async def _redis():
    import redis.asyncio as aioredis
    from gtm_core.config import get_settings

    try:
        r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        await r.ping()
        return r
    except Exception:
        import fakeredis

        return fakeredis.FakeAsyncRedis(decode_responses=True)


def hdr(n: int, title: str) -> None:
    print(f"\n{'=' * 78}\n SYSTEM {n}: {title}\n{'=' * 78}")


async def demo_sdr(svc) -> None:
    hdr(1, "Autonomous SDR (ICP prompt -> QA-verified, approved, CRM-logged email)")
    t0 = time.perf_counter()
    st = await svc.runner.start("Fintech companies using AWS", "demo-sdr", max_accounts=5)
    t_first = time.perf_counter() - t0
    print(f"accounts queued={st['accounts_queued']}  time to first QA-verified draft awaiting approval={t_first:.2f}s")
    pend = (await svc.repo.pending_approvals())[0]
    print(f"HITL pause: approval {pend['id'][:8]} -> would be posted to Slack; email NOT sent yet "
          f"(sent={len(svc.deps.sender.sent)})")
    st = await svc.runner.resume_by_approval(pend["id"], {"decision": "approved", "reviewer": "demo"})
    print(f"after approval: sent={len(svc.deps.sender.sent)}  hubspot activities={len(svc.deps.hubspot.activities)}  "
          f"salesforce activities={len(svc.deps.salesforce.activities)}")
    mail = svc.deps.sender.sent[0]
    print(f"headers: {list(mail['headers'])}\n--- email ---\n{mail['text']}")


async def demo_signals(svc) -> None:
    hdr(2, "Signal-to-Meeting Engine")
    from sys1_autonomous_sdr.graph import SdrRuntime
    from sys2_signal_engine import normalizer
    from sys2_signal_engine.router import process_event

    rt = SdrRuntime(svc.deps, svc.runner)
    seeded = {s["account_id"] for s in svc.repo.signals.values()}
    acct = next(a for a in svc.repo.accounts.values() if not a["is_customer"] and a["id"] not in seeded)
    now = datetime.now(UTC).isoformat()
    for body in (
        {"domain": acct["domain"], "signal_type": "hiring_spike", "source": "apollo", "occurred_at": now,
         "payload": {"open_roles": 6}},
        {"domain": acct["domain"], "signal_type": "champion_moved", "source": "sales_navigator", "occurred_at": now,
         "payload": {"new_title": "VP of Engineering"}},
    ):
        out = await process_event(rt, normalizer.normalize_n8n(body))
        print(f"{body['signal_type']:>15}: {out['status']:<15} composite={out.get('composite_score')} "
              f"latency_ms={out.get('latency_ms', '-')}")
    draft = list(svc.repo.drafts.values())[-1]
    print(f"--- multi-signal draft (status={draft['status']}) ---\n{draft['body_text'].split('--')[0]}")


def _tool_json(res) -> dict:
    sc = getattr(res, "structured_content", None)
    return sc if sc else json.loads(res.content[0].text)


async def demo_mcp(svc) -> None:
    hdr(3, "GTM Command Center (MCP, in-memory client)")
    from mcp.client import Client
    from sys3_command_center_mcp.server import create_server

    async with Client(create_server(svc)) as c:
        look = _tool_json(await c.call_tool("find_lookalike_accounts", {"seed_domain": "stripe", "top_k": 3,
                                                                         "min_shared_tech": 1}))
        print("lookalikes:", [(x["domain"], x["similarity_score"]) for x in look["lookalikes"]])
        for x in look["lookalikes"]:
            await c.call_tool("upsert_crm_deal_or_task", {"domain": x["domain"], "object_type": "task",
                                                            "title": f"Follow up with {x['domain']}"})
        print(f"hubspot tasks created: {len(svc.deps.hubspot.tasks)}")
        blocked = _tool_json(await c.call_tool("purge_or_suppress_prospect",
                                               {"email_or_domain": "acme.example", "reason": "demo"}))
        print("purge without operator token ->", {k: blocked[k] for k in ("suppressed", "blocked")})


async def demo_qa() -> None:
    hdr(4, "Outbound QA Layer")
    from sys4_outbound_qa.attribution import aggregate_attribution
    from sys4_outbound_qa.judge import QAJudge
    from sys4_outbound_qa.rubrics import JudgeInput

    golden = json.loads((Path(__file__).parent.parent / "tests/evals/golden_emails.json").read_text())
    judge = QAJudge(use_llm=False)
    res = [(g, await judge.evaluate(JudgeInput(source_facts=g["source_facts"], value_prop=g["value_prop"],
                                               subject=g["subject"], body=g["body"]))) for g in golden]
    hall = [(g, r) for g, r in res if g["label"] == "hallucination"]
    clean = [(g, r) for g, r in res if g["label"] == "clean"]
    print(f"golden set ({len(golden)} emails): hallucinations blocked {sum(not r.passed for _, r in hall)}/{len(hall)}, "
          f"clean passed {sum(r.passed for _, r in clean)}/{len(clean)}, "
          f"spam failed {sum(not r.passed for g, r in res if g['label'] == 'spam')}/10")
    g, r = hall[0]
    print(f"example: planted {g['planted_claim']!r} -> hallucination_score={r.hallucination_score} "
          f"claims={r.unsupported_claims}")
    print("NOTE: the golden set is templated; this shows rule coverage, not real-world recall.")
    print("attribution rows (synthetic):",
          aggregate_attribution([{"prompt_name": "sdr_email", "version_tag": "v1", "status": "sent",
                                  "reply_sentiment": "positive", "composite_score": 0.9}]))


async def demo_lookalike() -> None:
    hdr(5, "Lookalike Account Finder")
    from sys5_lookalike_finder.similarity import MemoryStore, rank, score_pair

    profiles = await MemoryStore().all_profiles()
    customers = [p for p in profiles.values() if p.is_customer]
    seed = customers[0]
    top = rank(seed, profiles.values(), top_k=5)
    for r in top:
        print(f"{r.domain:<28} score={r.similarity_score:.3f} shared={r.shared_tech} breakdown={r.breakdown}")

    # Hold-out precision@10: hide half the customers, rank on the other half, count hidden customers in top 10.
    def p_at_10(scorer) -> float:
        vals = []
        for k in range(5):
            hold = set(c.domain for c in customers[k::5])
            train = [c for c in customers if c.domain not in hold]
            pool = [p for p in profiles.values() if p.domain not in {t.domain for t in train}]
            scored = sorted(pool, key=lambda p: -statistics.fmean(scorer(t, p) for t in train))[:10]
            vals.append(sum(p.domain in hold for p in scored) / 10)
        return statistics.fmean(vals)

    hybrid = p_at_10(lambda s, c: score_pair(s, c).similarity_score)
    keyword = p_at_10(lambda s, c: float(s.industry == c.industry))  # standard industry filter baseline
    base = len(customers) / len(profiles)
    print(f"hold-out precision@10: hybrid={hybrid:.2f}  industry-filter baseline={keyword:.2f}  "
          f"random chance={base:.2f}")
    print("NOTE: sandbox data is randomly generated with no planted structure, so results sit near chance. "
          "Real precision needs real customer data.")


async def main() -> None:
    from gtm_core.redis_client import set_redis
    from gtm_safety import audit as audit_mod
    from sys3_command_center_mcp.services import build_services

    audit_mod.set_sink(audit_mod.MemoryAuditSink())
    set_redis(await _redis())
    svc = await build_services(redis=await _redis())
    await demo_sdr(svc)
    await demo_signals(svc)
    await demo_mcp(svc)
    await demo_qa()
    await demo_lookalike()
    print(f"\naudit records written: {len(audit_mod.get_sink().records)}")


if __name__ == "__main__":
    asyncio.run(main())
