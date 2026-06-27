"""FastAPI routes for the YouTube Research module.

  POST   /api/youtube-research/jobs        create + start a job {url, model?}
  GET    /api/youtube-research/jobs        list recent jobs
  GET    /api/youtube-research/jobs/{id}   one job (with breakdown)
  DELETE /api/youtube-research/jobs/{id}   forget a job
  GET    /api/youtube-research/topics      current research-vault topic folders

Jobs are tracked in memory and run as fire-and-forget asyncio tasks; the durable
artifact is the filed note in the vault, so jobs are transient progress trackers.
Routes are gated by the host auth (require_trusted_request).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from backend.auth_gate import require_trusted_request
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import captions, filing
from .llm import LLMError, complete
from .prompts import SINGLE_PASS_SYSTEM, single_pass_prompt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/youtube-research",
    tags=["youtube-research"],
    dependencies=[Depends(require_trusted_request)],
)

# In-memory job registry, newest last; capped so a long session does not grow
# without bound. Jobs are ephemeral; filed notes are the durable record.
_JOBS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_JOBS = 100

# Fields omitted from list responses (the big text blob).
_LIST_OMIT = ("breakdown_md",)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set(job_id: str, **fields: Any) -> dict[str, Any] | None:
    job = _JOBS.get(job_id)
    if not job:
        return None
    job.update(fields)
    job["updated_at"] = _now()
    _broadcast(job)
    return job


def _broadcast(job: dict[str, Any]) -> None:
    """Best-effort live update over the host WebSocket; the UI also polls."""
    try:
        from backend.websocket import manager

        asyncio.create_task(manager.broadcast("youtube-research:job", _public(job)))
    except Exception as e:  # pragma: no cover - ws is a nicety
        logger.debug("youtube-research broadcast failed: %s", e)


def _public(job: dict[str, Any], *, full: bool = True) -> dict[str, Any]:
    if full:
        return dict(job)
    return {k: v for k, v in job.items() if k not in _LIST_OMIT}


class CreateJob(BaseModel):
    url: str = Field(..., description="YouTube URL or 11-char video id.")
    model: str = Field(default="", description="Optional LLM model id override for this run.")


@router.get("/topics")
async def list_topics():
    """The current research-vault topic folders (classifier candidate set)."""
    filing.ensure_taxonomy()
    return {"topics": filing.list_topics()}


@router.post("/jobs")
async def create_job(req: CreateJob):
    video_id = captions.parse_video_id(req.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not parse a YouTube video id from that input.")
    job_id = uuid.uuid4().hex[:16]
    job = {
        "id": job_id,
        "url": req.url.strip(),
        "video_id": video_id,
        "model": req.model.strip(),
        "title": "",
        "channel": "",
        "status": "queued",
        "progress": "Queued",
        "error": "",
        "breakdown_md": "",
        "filed_path": "",
        "topic": "",
        "tags": [],
        "confidence": 0.0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _JOBS[job_id] = job
    while len(_JOBS) > _MAX_JOBS:
        _JOBS.popitem(last=False)
    asyncio.create_task(_run_job(job_id))
    return _public(job)


@router.get("/jobs")
async def list_jobs():
    return {"jobs": [_public(j, full=False) for j in reversed(_JOBS.values())]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return _public(job)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if _JOBS.pop(job_id, None) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"ok": True}


async def _run_job(job_id: str) -> None:
    """Transcribe -> breakdown -> file. Never raises; records errors on the job."""
    try:
        job = _JOBS.get(job_id)
        if not job:
            return
        model = job.get("model") or ""

        # 1) Transcribe from captions.
        _set(job_id, status="transcribing", progress="Fetching captions")
        try:
            rec = await captions.fetch_transcript(job["video_id"])
        except captions.CaptionsError as e:
            _set(job_id, status="error", error=str(e))
            return

        meta = {
            "video_id": rec["video_id"],
            "title": rec.get("title") or "",
            "channel": rec.get("channel") or "",
            "url": rec.get("url") or job["url"],
            "language": rec.get("language") or "",
        }
        _set(job_id, status="analyzing", title=meta["title"], channel=meta["channel"], progress="Generating breakdown")

        # 2) Breakdown.
        try:
            breakdown_md = await complete(
                SINGLE_PASS_SYSTEM,
                single_pass_prompt(meta["title"], meta["channel"], meta["url"], rec["text"]),
                model=model,
            )
        except LLMError as e:
            _set(job_id, status="error", error=f"Breakdown failed: {e}")
            return

        _set(job_id, status="filing", breakdown_md=breakdown_md, progress="Filing into the research vault")

        # 3) Intake -> classify -> auto-file.
        try:
            inbox_rel = await filing.intake(breakdown_md, meta)
            result = await filing.classify_and_file(inbox_rel, breakdown_md, meta, model=model)
        except Exception as e:  # noqa: BLE001 - filing must not lose the breakdown
            logger.warning("youtube-research: filing failed for %s: %s", job_id, e)
            _set(
                job_id,
                status="done",
                progress="Breakdown ready (filing failed; see logs)",
                error=f"Filing failed: {e}",
            )
            return

        topic = result.get("topic") or ""
        _set(
            job_id,
            status="done",
            filed_path=result.get("filed") or "",
            topic=topic,
            tags=result.get("tags") or [],
            confidence=result.get("confidence") or 0.0,
            progress=(f"Filed under research/{topic}" if topic else "Filed to research/inbox (no confident topic)"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("youtube-research job crashed: %s", job_id)
        _set(job_id, status="error", error=f"Crashed: {type(e).__name__}: {e}")
