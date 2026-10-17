"""GTM Command Center MCP server (stdio / SSE). 10 typed tools: read, write, destructive."""

import asyncio
from typing import Literal

from mcp.server.mcpserver import MCPServer

from sys3_command_center_mcp import tools_read as r
from sys3_command_center_mcp import tools_write as w
from sys3_command_center_mcp.services import Services, build_services

TOOL_PERMISSIONS = {
    "search_crm_accounts": "read", "get_account_360": "read", "enrich_prospect_waterfall": "write",
    "find_lookalike_accounts": "read", "query_active_signals": "read", "launch_sdr_campaign": "write",
    "evaluate_email_draft": "read", "upsert_crm_deal_or_task": "write", "resolve_approval_request": "write",
    "purge_or_suppress_prospect": "destructive",
}

_services: Services | None = None


async def _svc() -> Services:
    global _services
    if _services is None:
        _services = await build_services()
    return _services


def create_server(services: Services | None = None) -> MCPServer:
    mcp = MCPServer("gtm-command-center")

    async def svc() -> Services:
        return services or await _svc()

    @mcp.tool()
    async def search_crm_accounts(query: str | None = None, tier: Literal["tier_1", "tier_2", "tier_3"] | None = None,
                                  min_icp_score: float | None = None, limit: int = 10) -> dict:
        """[read] Unified search across PostgreSQL, HubSpot and Salesforce accounts."""
        return await r.search_crm_accounts(await svc(), query, tier, min_icp_score, limit)

    @mcp.tool()
    async def get_account_360(domain: str) -> dict:
        """[read] Full account firmographics, contacts, open signals, recent emails and CRM sync status."""
        return await r.get_account_360(await svc(), domain)

    @mcp.tool()
    async def enrich_prospect_waterfall(domain: str, target_titles: list[str] | None = None,
                                        use_clay: bool = True) -> dict:
        """[write] Trigger Apollo match + Clay enrichment for a domain (7-day cache)."""
        return await w.enrich_prospect_waterfall(await svc(), domain, target_titles, use_clay)

    @mcp.tool()
    async def find_lookalike_accounts(seed_domain: str, top_k: int = 10, min_shared_tech: int = 2) -> dict:
        """[read] Top lookalikes to a seed customer (Neo4j graph overlap + pgvector similarity)."""
        return await r.find_lookalike_accounts(await svc(), seed_domain, top_k, min_shared_tech)

    @mcp.tool()
    async def query_active_signals(signal_types: list[str] | None = None, min_score: float = 70.0,
                                   days_back: int = 7) -> dict:
        """[read] Recent high-scoring buying signals (time-decayed to now)."""
        return await r.query_active_signals(await svc(), signal_types, min_score, days_back)

    @mcp.tool()
    async def launch_sdr_campaign(icp_prompt: str, campaign_id: str, max_accounts: int = 10) -> dict:
        """[write] Start the Autonomous SDR from a plain-English ICP. Emails still pass QA + the safety gate."""
        return await w.launch_sdr_campaign(await svc(), icp_prompt, campaign_id, max_accounts)

    @mcp.tool()
    async def evaluate_email_draft(draft_id: str | None = None, subject: str | None = None, body: str | None = None,
                                   facts: list[str] | None = None) -> dict:
        """[read] Run the Outbound QA judge on an existing draft or raw email text."""
        return await r.evaluate_email_draft(await svc(), draft_id, subject, body, facts)

    @mcp.tool()
    async def upsert_crm_deal_or_task(domain: str, object_type: Literal["deal", "task"], title: str,
                                      amount_usd: float | None = None, due_date: str | None = None) -> dict:
        """[write] Create a deal or follow-up task in HubSpot (and a task in Salesforce)."""
        return await w.upsert_crm_deal_or_task(await svc(), domain, object_type, title, amount_usd, due_date)

    @mcp.tool()
    async def resolve_approval_request(approval_request_id: str, decision: Literal["approved", "rejected"],
                                       reviewer_note: str | None = None) -> dict:
        """[write] Approve or reject a pending HITL request and resume the paused SDR thread."""
        return await w.resolve_approval_request(await svc(), approval_request_id, decision, reviewer_note)

    @mcp.tool()
    async def purge_or_suppress_prospect(email_or_domain: str, reason: str, confirm_token: str = "") -> dict:
        """[destructive] Right-to-be-forgotten: suppress, redact PII, cancel drafts. Needs an operator confirm_token."""
        return await w.purge_or_suppress_prospect(await svc(), email_or_domain, reason, confirm_token)

    return mcp


def main() -> None:
    import sys

    server = create_server()
    if "--sse" in sys.argv:
        asyncio.run(server.run_sse_async())
    else:
        asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
