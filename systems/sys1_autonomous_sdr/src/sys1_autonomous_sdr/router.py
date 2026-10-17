"""FastAPI endpoints: /api/v1/sdr/*"""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from gtm_safety.gate import ApprovalError
from pydantic import BaseModel, Field

from sys1_autonomous_sdr.graph import get_runtime
from sys1_autonomous_sdr.nodes import handle_reply
from sys1_autonomous_sdr.prompts import DEFAULT_VALUE_PROP

router = APIRouter(prefix="/api/v1/sdr", tags=["sdr"])


class CampaignRequest(BaseModel):
    icp_prompt: str = Field(min_length=3)
    campaign_id: str
    max_accounts: int = Field(10, ge=1, le=100)
    value_prop: str = DEFAULT_VALUE_PROP


class ResolveRequest(BaseModel):
    decision: str  # approved | rejected | edited
    reviewer: str = "api"
    note: str | None = None
    edited_body: str | None = None


class ReplyRequest(BaseModel):
    draft_id: str
    text: str


@router.post("/campaigns", status_code=202)
async def launch(req: CampaignRequest, bg: BackgroundTasks) -> dict:
    rt = get_runtime()
    thread_id = f"{req.campaign_id}:{__import__('uuid').uuid4().hex[:8]}"
    bg.add_task(rt.runner.start, req.icp_prompt, req.campaign_id, req.max_accounts, req.value_prop, thread_id)
    return {"thread_id": thread_id, "status_url": f"/api/v1/sdr/campaigns/{thread_id}"}


@router.get("/campaigns/{thread_id}")
async def campaign_status(thread_id: str) -> dict:
    return await get_runtime().runner.status(thread_id)


@router.post("/approvals/{approval_id}/resolve")
async def resolve(approval_id: str, req: ResolveRequest) -> dict:
    rt = get_runtime()
    try:
        return await rt.runner.resume_by_approval(
            approval_id, {"decision": req.decision, "reviewer": req.reviewer, "note": req.note,
                          "edited_body": req.edited_body})
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ApprovalError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/replies")
async def reply(req: ReplyRequest) -> dict:
    try:
        return await handle_reply(get_runtime().deps, req.draft_id, req.text)
    except LookupError as e:
        raise HTTPException(404, f"unknown draft {e}") from e
