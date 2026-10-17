"""Prompt-version reply-rate attribution."""

from collections import defaultdict
from statistics import mean

from sqlalchemy import text

ATTRIBUTION_SQL = text(
    """
SELECT
    pv.prompt_name,
    pv.version_tag,
    COUNT(ed.id) AS total_drafts,
    COUNT(ed.id) FILTER (WHERE ed.status IN ('sent', 'replied')) AS total_sent,
    ROUND(AVG(qa.composite_score), 3) AS avg_qa_score,
    ROUND(
        COUNT(ed.id) FILTER (WHERE ed.reply_sentiment = 'positive')::NUMERIC /
        NULLIF(COUNT(ed.id) FILTER (WHERE ed.status IN ('sent', 'replied')), 0), 4
    ) AS positive_reply_rate,
    COUNT(ed.id) FILTER (WHERE ed.meeting_booked = TRUE) AS meetings_booked
FROM prompt_versions pv
LEFT JOIN email_drafts ed ON ed.prompt_version_id = pv.id
LEFT JOIN qa_evaluations qa ON qa.email_draft_id = ed.id
GROUP BY pv.id, pv.prompt_name, pv.version_tag
ORDER BY positive_reply_rate DESC NULLS LAST
"""
)


async def fetch_attribution(session) -> list[dict]:
    rows = (await session.execute(ATTRIBUTION_SQL)).mappings().all()
    return [
        {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in dict(r).items()}
        for r in rows
    ]


def aggregate_attribution(drafts: list[dict]) -> list[dict]:
    """Pure-python equivalent of ATTRIBUTION_SQL over dict rows (used by tests and the demo).

    Each draft: prompt_name, version_tag, status, reply_sentiment, meeting_booked, composite_score.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in drafts:
        groups[(d["prompt_name"], d["version_tag"])].append(d)
    out = []
    for (name, tag), ds in groups.items():
        sent = [d for d in ds if d["status"] in ("sent", "replied")]
        pos = [d for d in ds if d.get("reply_sentiment") == "positive"]
        scores = [d["composite_score"] for d in ds if d.get("composite_score") is not None]
        out.append(
            {
                "prompt_name": name,
                "version_tag": tag,
                "total_drafts": len(ds),
                "total_sent": len(sent),
                "avg_qa_score": round(mean(scores), 3) if scores else None,
                "positive_reply_rate": round(len(pos) / len(sent), 4) if sent else None,
                "meetings_booked": sum(1 for d in ds if d.get("meeting_booked")),
            }
        )
    return sorted(out, key=lambda r: (r["positive_reply_rate"] is None, -(r["positive_reply_rate"] or 0)))
