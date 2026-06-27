"""Captions-only YouTube transcription, in-process over HTTP.

v1 is captions-only by design: no GPU, no whisper, no sidecar. We fetch the
watch page, read the caption-track list out of `ytInitialPlayerResponse`, pull
the chosen track's timed-text, and flatten it to plain text. The only network
egress is to `*.youtube.com` (declared in the manifest).

Limitations (documented honestly): a video with captions disabled cannot be
transcribed in v1, and YouTube occasionally changes the watch-page shape or
serves a consent interstitial, which surfaces as a clear error rather than a
silent empty transcript.
"""

from __future__ import annotations

import html
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

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 30.0
_PREFERRED_LANGS = ("en", "en-US", "en-GB")


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


def _extract_json_object(page: str, marker: str) -> dict[str, Any] | None:
    """Extract the JSON object that follows `marker` via balanced-brace scan.

    Regex can't safely match a nested JSON blob; we find the first `{` after the
    marker and walk to its matching `}`, respecting string literals/escapes.
    """
    idx = page.find(marker)
    if idx == -1:
        return None
    start = page.find("{", idx)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(page)):
        c = page[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(page[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _caption_tracks(player: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    ) or []


def _pick_track(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a manual English track, then any English, then any track."""
    def lang(t: dict[str, Any]) -> str:
        return (t.get("languageCode") or "").lower()

    for want in _PREFERRED_LANGS:
        for t in tracks:
            if lang(t) == want.lower() and t.get("kind") != "asr":
                return t
    for want in _PREFERRED_LANGS:
        for t in tracks:
            if lang(t) == want.lower():
                return t
    return tracks[0]


def _parse_timedtext(body: str) -> str:
    """Flatten a timedtext XML response into plain text.

    Segments come as `<text start=.. dur=..>escaped</text>`; YouTube
    HTML-escapes the content (sometimes doubly), so we unescape twice and strip
    any leftover tags, then join into paragraphs.
    """
    segs = re.findall(r"<text\b[^>]*>(.*?)</text>", body, re.S)
    out: list[str] = []
    for seg in segs:
        txt = html.unescape(html.unescape(seg))
        txt = re.sub(r"<[^>]+>", "", txt)
        txt = txt.replace("\n", " ").strip()
        if txt:
            out.append(txt)
    return " ".join(out).strip()


async def fetch_transcript(video_id: str) -> dict[str, Any]:
    """Fetch title, channel, and the caption transcript for a video id.

    Returns {video_id, title, channel, text, language, url}. Raises
    CaptionsError with an operator-facing message on any failure.
    """
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        try:
            resp = await client.get(watch_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise CaptionsError(f"Could not load the video page: {type(e).__name__}: {e}") from e

        player = _extract_json_object(resp.text, "ytInitialPlayerResponse")
        if not player:
            raise CaptionsError(
                "Could not read the video metadata (YouTube may have served a consent page). Try again."
            )

        details = player.get("videoDetails", {}) or {}
        title = details.get("title") or ""
        channel = details.get("author") or ""

        status = (player.get("playabilityStatus", {}) or {}).get("status")
        if status and status not in ("OK", "LIVE_STREAM_OFFLINE"):
            reason = (player.get("playabilityStatus", {}) or {}).get("reason") or status
            raise CaptionsError(f"Video is not playable: {reason}")

        tracks = _caption_tracks(player)
        if not tracks:
            raise CaptionsError(
                "This video has no caption track. v1 is captions-only - try a video that has "
                "captions or subtitles enabled."
            )

        track = _pick_track(tracks)
        base_url = track.get("baseUrl")
        if not base_url:
            raise CaptionsError("Caption track had no fetchable URL.")

        try:
            tr = await client.get(base_url)
            tr.raise_for_status()
        except httpx.HTTPError as e:
            raise CaptionsError(f"Could not fetch the caption track: {type(e).__name__}: {e}") from e

        text = _parse_timedtext(tr.text)
        if not text:
            raise CaptionsError("The caption track was empty.")

        return {
            "video_id": video_id,
            "title": title,
            "channel": channel,
            "text": text,
            "language": track.get("languageCode") or "",
            "url": watch_url,
        }
