"""FastAPI endpoints: /api/v1/qa/*"""

from typing import Any

from fastapi import APIRouter
from gtm_core.db import session_scope
from gtm_core.schemas import QAResult
from pydantic import BaseModel, Field

from sys4_outbound_qa.attribution import fetch_attribution
from sys4_outbound_qa.judge import QAJudge
from sys4_outbound_qa.rubrics import JudgeInput

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])


class EvaluateRequest(BaseModel):
    subject: str = ""
    body: str
    source_facts: dict[str, Any] = Field(default_factory=dict)
    facts: list[str] = Field(default_factory=list)
    value_prop: str = ""
    draft_id: str | None = None


@router.post("/evaluate", response_model=QAResult)
async def evaluate(req: EvaluateRequest) -> QAResult:
    facts = dict(req.source_facts)
    if req.facts:
        facts["facts"] = req.facts
    return await QAJudge().evaluate(
        JudgeInput(source_facts=facts, value_prop=req.value_prop, subject=req.subject, body=req.body, draft_id=req.draft_id)
    )


@router.get("/attribution")
async def attribution() -> dict:
    async with session_scope() as s:
        return {"prompt_versions": await fetch_attribution(s)}
