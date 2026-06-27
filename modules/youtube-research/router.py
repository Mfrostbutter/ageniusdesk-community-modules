"""FastAPI routes for the YouTube Research module.

Mirrors the main app's research surface so the view + output match, but writes
into the containerized notes vault (see artifacts.py):

  GET    /api/youtube-research/config              UI bootstrap (default engine)
  GET    /api/youtube-research/folders?path=       folder picker: subfolders
  POST   /api/youtube-research/folders             folder picker: create folder
  POST   /api/youtube-research/jobs                create + start {url, depth, destination, model}
  GET    /api/youtube-research/jobs                list recent jobs (no big blobs)
  GET    /api/youtube-research/jobs/{id}           full job (transcript + breakdown)
  POST   /api/youtube-research/jobs/{id}/deepdive  run the deep dive
  POST   /api/youtube-research/jobs/{id}/move      move the artifact folder
  DELETE /api/youtube-research/jobs/{id}           forget a job
  GET    /api/youtube-research/jobs/{id}/artifact?kind=breakdown|deep|transcript

Jobs are tracked in memory and run as fire-and-forget asyncio tasks; the durable
artifacts (transcript.md, BREAKDOWN.md, BREAKDOWN-deep.md, meta.json) live in the
vault, so a restart loses the in-memory list but never the knowledge.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from backend.auth_gate import require_trusted_request
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import artifacts, captions, classify, deepdive, store
from .llm import LLMError, complete
from .prompts import SINGLE_PASS_SYSTEM, single_pass_prompt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/youtube-research",
    tags=["youtube-research"],
    dependencies=[Depends(require_trusted_request)],
)


async def _set(job_id: str, **fields: Any) -> dict[str, Any] | None:
    """Persist a job update (host DB) and broadcast it live."""
    job = await store.update(job_id, **fields)
    if job:
        _broadcast(job)
    return job


def _broadcast(job: dict[str, Any]) -> None:
    try:
        from backend.websocket import manager

        asyncio.create_task(manager.broadcast("youtube-research:job", store.public_list_item(job)))
    except Exception as e:  # pragma: no cover - ws is a nicety
        logger.debug("youtube-research broadcast failed: %s", e)


# ── Config + folder picker ───────────────────────────────────────────────────


@router.get("/config")
async def get_config():
    # v1 is captions-only (yt-dlp in-process; no GPU, no sidecar).
    return {"default_engine": "captions", "engines": ["captions"]}


@router.get("/folders")
async def list_folders(path: str = Query(default="")):
    artifacts.ensure_taxonomy()
    rel = artifacts.sanitize_destination(path)
    return {"path": rel, "folders": artifacts.list_folders(rel)}


class CreateFolder(BaseModel):
    path: str = Field(..., description="Relative folder path under the research root.")


@router.post("/folders")
async def create_folder(req: CreateFolder):
    try:
        created = artifacts.make_folder(req.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": created}


# ── Jobs ─────────────────────────────────────────────────────────────────────


class CreateJob(BaseModel):
    url: str = Field(..., description="YouTube URL or 11-char video id.")
    depth: str = Field(default="single", description="'single' or 'deep'.")
    engine: str = Field(default="captions", description="captions-only in v1.")
    destination: str = Field(default="", description="Topic subfolder under research/ (blank = _inbox).")
    model: str = Field(default="", description="Optional LLM model override for this run.")


@router.post("/jobs")
async def create_job(req: CreateJob):
    video_id = captions.parse_video_id(req.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not parse a YouTube video id from that input.")
    if req.depth not in ("single", "deep"):
        raise HTTPException(status_code=400, detail="depth must be 'single' or 'deep'.")
    ts = store.now()
    fields = {
        "url": req.url.strip(),
        "video_id": video_id,
        "depth": req.depth,
        "engine": "captions",
        "engine_used": "",
        "destination": artifacts.sanitize_destination(req.destination),
        "model": req.model.strip(),
        "title": "",
        "channel": "",
        "duration_seconds": None,
        "status": "queued",
        "progress": "Queued",
        "error": "",
        "breakdown_md": "",
        "deepdive_md": "",
        "transcript_text": "",
        "artifact_dir": "",
        "created_at": ts,
        "updated_at": ts,
    }
    # A re-run of the same video replaces its existing entry (the harness keeps
    # one copy, so the view does too): reuse the row, reset it, bump it to top.
    existing = await store.find_by_video(video_id)
    if existing:
        job_id = existing["id"]
        created = await store.update(job_id, **fields)
    else:
        job_id = uuid.uuid4().hex[:16]
        created = await store.create({"id": job_id, **fields})
    asyncio.create_task(_run_job(job_id))
    return created


@router.get("/jobs")
async def list_jobs(limit: int = 200):
    return {"jobs": await store.list_jobs(limit)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


class DeepdiveReq(BaseModel):
    model: str = Field(default="", description="Optional model override for this deep dive.")


@router.post("/jobs/{job_id}/deepdive")
async def deepdive_job(job_id: str, req: DeepdiveReq | None = None):
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.get("transcript_text"):
        raise HTTPException(status_code=409, detail="Job has no transcript yet.")
    if job.get("status") == "deepdiving":
        raise HTTPException(status_code=409, detail="Deep dive already running.")
    model = (req.model.strip() if req else "") or job.get("model") or ""
    if model:
        await store.update(job_id, model=model)
    asyncio.create_task(_run_deepdive(job_id))
    return {"ok": True, "job_id": job_id}


class MoveReq(BaseModel):
    destination: str = Field(default="", description="New topic folder under research/ (blank = _inbox).")


@router.post("/jobs/{job_id}/move")
async def move_job(job_id: str, req: MoveReq):
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.get("artifact_dir"):
        raise HTTPException(status_code=409, detail="This job has no saved artifacts to move.")
    destination = artifacts.sanitize_destination(req.destination)
    try:
        new_dir = await artifacts.move_artifact(job["artifact_dir"], destination)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Move failed: {e}")
    updated = await _set(job_id, artifact_dir=new_dir, destination=destination)
    return {"ok": True, "artifact_dir": new_dir, "destination": destination, "job": updated}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if not await store.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"ok": True}


@router.get("/jobs/{job_id}/artifact", response_class=PlainTextResponse)
async def download_artifact(job_id: str, kind: str = Query(default="breakdown")):
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    field = {"breakdown": "breakdown_md", "deep": "deepdive_md", "transcript": "transcript_text"}.get(kind)
    if not field:
        raise HTTPException(status_code=400, detail="kind must be breakdown, deep, or transcript.")
    content = job.get(field) or ""
    if not content:
        raise HTTPException(status_code=404, detail=f"No {kind} content for this job.")
    slug = (job.get("title") or job_id)[:40].replace("/", "-")
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{slug}-{kind}.md"'},
        media_type="text/markdown",
    )


# ── Pipeline ─────────────────────────────────────────────────────────────────


async def _run_job(job_id: str) -> None:
    """Transcribe -> breakdown -> write to vault. Never raises."""
    try:
        job = await store.get(job_id)
        if not job:
            return
        model = job.get("model") or ""
        depth = job.get("depth") or "single"

        await _set(job_id, status="transcribing", progress="Fetching captions")
        try:
            rec = await captions.fetch_transcript(job["video_id"])
        except captions.CaptionsError as e:
            await _set(job_id, status="error", engine_used="captions", error=str(e))
            return

        title = artifacts.clean_title(rec.get("title") or "") or (rec.get("title") or "")
        channel = rec.get("channel") or ""
        transcript_text = rec.get("text") or ""
        await _set(
            job_id,
            status="analyzing",
            engine_used="captions",
            title=title,
            channel=channel,
            duration_seconds=rec.get("duration_seconds"),
            transcript_text=transcript_text,
            progress="Generating breakdown",
        )

        try:
            breakdown_md = await complete(
                SINGLE_PASS_SYSTEM,
                single_pass_prompt(title, channel, rec.get("url", job["url"]), transcript_text),
                model=model,
            )
        except LLMError as e:
            await _set(job_id, status="error", error=f"Breakdown failed: {e}")
            return

        artifact_dir = ""
        try:
            rel_dir = artifacts.artifact_dir_for(rec["video_id"], title, channel, job.get("destination") or "")
            meta = {
                "video_id": rec["video_id"],
                "url": rec.get("url", job["url"]),
                "title": title,
                "channel_title": channel,
                "duration_seconds": rec.get("duration_seconds"),
                "language": rec.get("language"),
                "engine_used": "captions",
                "transcript": {"backend": "yt-dlp"},
            }
            transcript_md = artifacts.transcript_markdown(meta, transcript_text)
            artifact_dir = await artifacts.write_base(
                rel_dir, meta=meta, transcript_md=transcript_md, breakdown_md=breakdown_md
            )
        except Exception as e:  # noqa: BLE001 - artifact write must not lose the work
            logger.warning("youtube-research: artifact write failed for %s: %s", job_id, e)

        # Auto-file: when the operator picked no destination, classify the
        # breakdown into a topic folder and move it out of the inbox. An explicit
        # destination is respected as-is.
        destination = job.get("destination") or ""
        if artifact_dir and destination in ("", artifacts.DEFAULT_TOPIC):
            artifacts.ensure_taxonomy()  # guarantee candidate folders exist to classify into
            await _set(job_id, progress="Filing into a topic folder")
            topic = await classify.classify_topic(
                title, channel, breakdown_md, artifacts.list_topics(), model=model
            )
            if topic:
                try:
                    artifact_dir = await artifacts.move_artifact(artifact_dir, topic)
                    destination = topic
                except Exception as e:  # noqa: BLE001
                    logger.warning("youtube-research: auto-file move failed for %s: %s", job_id, e)

        await _set(
            job_id,
            status="deepdiving" if depth == "deep" else "done",
            breakdown_md=breakdown_md,
            artifact_dir=artifact_dir,
            destination=destination,
            progress="Extracting deep technical detail" if depth == "deep" else "Breakdown ready",
        )

        if depth == "deep":
            await _run_deepdive(job_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("youtube-research job crashed: %s", job_id)
        await _set(job_id, status="error", error=f"Crashed: {type(e).__name__}: {e}")


async def _run_deepdive(job_id: str) -> None:
    """Run the deep dive on an already-transcribed job. Never raises."""
    try:
        job = await store.get(job_id)
        if not job or not job.get("transcript_text"):
            await _set(job_id, status="error", error="Deep dive needs a completed transcript first.")
            return
        await _set(job_id, status="deepdiving", progress="Extracting deep technical detail")
        try:
            deep_md = await deepdive.run(
                job.get("title", ""), job.get("channel", ""), job.get("url", ""),
                job["transcript_text"], model=job.get("model", ""),
            )
        except LLMError as e:
            await _set(job_id, status="error", error=f"Deep dive failed: {e}")
            return

        if job.get("artifact_dir"):
            try:
                await artifacts.write_deep(job["artifact_dir"], deep_md)
            except Exception as e:  # noqa: BLE001
                logger.warning("youtube-research: deep write failed for %s: %s", job_id, e)

        await _set(job_id, status="done", deepdive_md=deep_md, progress="Deep dive ready")
    except Exception as e:  # noqa: BLE001
        logger.exception("youtube-research deepdive crashed: %s", job_id)
        await _set(job_id, status="error", error=f"Deep dive crashed: {type(e).__name__}: {e}")
