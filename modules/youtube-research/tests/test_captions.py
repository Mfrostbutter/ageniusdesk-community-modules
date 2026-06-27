"""Unit tests for the pure caption-parsing helpers (no network, no yt-dlp).

Run from the AgeniusDesk CE venv so httpx is available, e.g.:
    uv run --project ../ageniusdesk-ce --with pytest python -m pytest
"""

import sys
from pathlib import Path

# captions.py has no package-relative imports, so add the module dir and import it
# directly (the package name is hyphenated and not import-statement friendly).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import captions  # noqa: E402


def test_parse_video_id_forms():
    assert captions.parse_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://www.youtube.com/watch?v=goOZSXmrYQ4&t=121s") == "goOZSXmrYQ4"
    assert captions.parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert captions.parse_video_id("not a url") is None


def test_parse_caption_body_json3():
    body = (
        '{"events":['
        '{"tStartMs":0,"dDurationMs":1200,"segs":[{"utf8":"hello "},{"utf8":"world"}]},'
        '{"tStartMs":1200,"dDurationMs":900,"segs":[{"utf8":"second line"}]},'
        '{"tStartMs":2100,"dDurationMs":100,"segs":[{"utf8":"\\n"}]}'
        "]}"
    )
    segs = captions._parse_caption_body(body, "json3")
    assert [s["text"] for s in segs] == ["hello world", "second line"]
    assert segs[0]["start"] == 0.0 and segs[1]["start"] == 1.2


def test_parse_caption_body_vtt():
    body = (
        "WEBVTT\n\n"
        "1\n00:00:00.000 --> 00:00:02.000\nhello world\n\n"
        "2\n00:00:02.000 --> 00:00:04.000\nsecond line\n"
    )
    segs = captions._parse_caption_body(body, "vtt")
    assert [s["text"] for s in segs] == ["hello world", "second line"]
    assert segs[1]["start"] == 2.0


def test_select_track_prefers_manual_then_auto():
    info = {
        "subtitles": {"en": [{"ext": "json3", "url": "manual"}]},
        "automatic_captions": {"en": [{"ext": "json3", "url": "auto"}]},
    }
    lang, tracks, gen = captions._select_track(info, ["en"])
    assert tracks[0]["url"] == "manual" and gen is False

    info2 = {"subtitles": {}, "automatic_captions": {"en": [{"ext": "json3", "url": "auto"}]}}
    lang2, tracks2, gen2 = captions._select_track(info2, ["en"])
    assert tracks2[0]["url"] == "auto" and gen2 is True


def test_select_track_no_captions_raises():
    import pytest

    with pytest.raises(captions.CaptionsError):
        captions._select_track({"subtitles": {}, "automatic_captions": {}}, ["en"])
