"""Captions-only YouTube transcription via yt-dlp, in-process.

v1 is captions-only by design: no GPU, no whisper, no sidecar. We use yt-dlp to
discover the caption tracks (manual first, then auto-generated), download the
chosen track (json3 preferred, vtt/srt fallback), and flatten it to plain text.
yt-dlp handles YouTube's track discovery and formats robustly, which a hand-rolled
timedtext fetch does not.

Modeled on Mfrostbutter/transcript-to-knowledge (ttk/transcribe.py). yt-dlp is
provided by the AgeniusDesk runtime; if it is absent the error says so. The only
network egress is to YouTube (declared in the manifest).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
_PREFERRED_LANGS = ["en", "en-US", "en-GB"]
_TRACK_TIMEOUT = 30.0


class CaptionsError(RuntimeError):
    """A transcription failure with an operator-facing message."""


def parse_video_id(raw: str) -> str | None:
    """Validate input is a YouTube URL or 11-char id; return the id or None.

    Strict host allowlist so junk is rejected before any network call.
    """
    if not raw:
        return None
    raw = raw.strip()
    if _VIDEO_ID_RE.match(raw):
        return raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host in _YOUTUBE_HOSTS:
        qs = parse_qs(parsed.query)
        if qs.get("v") and _VIDEO_ID_RE.match(qs["v"][0]):
            return qs["v"][0]
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if path.startswith(prefix):
                cand = path[len(prefix):].split("/")[0]
                if _VIDEO_ID_RE.match(cand):
                    return cand
    if host in _YOUTU_BE_HOSTS:
        cand = path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(cand):
            return cand
    return None


def _parse_caption_body(body: str, ext: str) -> list[dict[str, Any]]:
    """Parse a json3 or vtt/srt caption body into timed segments."""
    if ext == "json3" or body.lstrip().startswith("{"):
        data = json.loads(body)
        segments: list[dict[str, Any]] = []
        for event in data.get("events", []) or []:
            seg_text = "".join((s.get("utf8") or "") for s in (event.get("segs") or [])).strip()
            if not seg_text:
                continue
            segments.append({
                "start": (event.get("tStartMs") or 0) / 1000.0,
                "duration": (event.get("dDurationMs") or 0) / 1000.0,
                "text": seg_text,
            })
        return segments

    cue_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[\.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[\.,](\d{3})"
    )
    segments = []
    current_start = current_end = 0.0
    buf: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        m = cue_re.search(line)
        if m:
            if buf:
                text = " ".join(buf).strip()
                if text:
                    segments.append({
                        "start": current_start,
                        "duration": max(0.0, current_end - current_start),
                        "text": text,
                    })
                buf = []
            h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in m.groups())
            current_start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
            current_end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        elif line and not line.isdigit() and line.upper() != "WEBVTT":
            buf.append(line)
    if buf:
        text = " ".join(buf).strip()
        if text:
            segments.append({"start": current_start, "duration": max(0.0, current_end - current_start), "text": text})
    return segments


def _select_track(info: dict[str, Any], languages: list[str]) -> tuple[str, list[dict], bool]:
    """Pick (language, tracks, is_generated): manual subs first, then auto."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for lang in languages:
        if lang in subs:
            return lang, subs[lang], False
    for lang in languages:
        if lang in auto:
            return lang, auto[lang], True
    if subs:
        lang = next(iter(subs))
        return lang, subs[lang], False
    if auto:
        lang = next(iter(auto))
        return lang, auto[lang], True
    raise CaptionsError(
        "This video has no caption track. v1 is captions-only - try a video that has "
        "captions or subtitles enabled."
    )


def _fetch_captions_sync(video_id: str, languages: list[str]) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as e:
        raise CaptionsError(
            "yt-dlp is required for caption transcription but is not installed in this "
            "AgeniusDesk runtime."
        ) from e

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    opts: dict[str, Any] = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": languages,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url, download=False)
    except Exception as e:  # noqa: BLE001 - yt-dlp raises a variety of types
        raise CaptionsError(f"Could not load the video: {type(e).__name__}: {e}") from e

    language, tracks, is_generated = _select_track(info, languages)
    track = next((t for t in tracks if t.get("ext") == "json3"), None) or tracks[0]
    url = track.get("url")
    if not url:
        raise CaptionsError("Caption track had no fetchable URL.")

    try:
        with httpx.Client(timeout=_TRACK_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            body = resp.text
    except httpx.HTTPError as e:
        raise CaptionsError(f"Could not fetch the caption track: {type(e).__name__}: {e}") from e

    segments = _parse_caption_body(body, (track.get("ext") or "").lower())
    text = " ".join(s["text"] for s in segments if s["text"]).strip()
    if not text:
        raise CaptionsError("The caption track downloaded but contained no usable text.")

    return {
        "video_id": video_id,
        "url": watch_url,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_seconds": info.get("duration"),
        "language": language,
        "is_generated": is_generated,
        "text": text,
    }


async def fetch_transcript(video_id: str, languages: list[str] | None = None) -> dict[str, Any]:
    """Fetch title, channel, and the caption transcript for a video id.

    yt-dlp is blocking, so it runs in a worker thread. Returns
    {video_id, title, channel, text, language, url, ...}. Raises CaptionsError
    with an operator-facing message on any failure.
    """
    return await asyncio.to_thread(_fetch_captions_sync, video_id, languages or _PREFERRED_LANGS)
