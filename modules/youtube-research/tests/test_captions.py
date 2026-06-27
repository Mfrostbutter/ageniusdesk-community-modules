"""Unit tests for the pure caption-parsing helpers (no network).

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
    assert captions.parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert captions.parse_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert captions.parse_video_id("not a url") is None


def test_extract_json_object_balanced():
    page = 'foo var ytInitialPlayerResponse = {"a": {"b": "}{"}, "c": [1,2]};</script>'
    obj = captions._extract_json_object(page, "ytInitialPlayerResponse")
    assert obj == {"a": {"b": "}{"}, "c": [1, 2]}


def test_extract_json_object_missing():
    assert captions._extract_json_object("nothing here", "ytInitialPlayerResponse") is None


def test_parse_timedtext_unescapes_and_strips():
    xml = (
        '<?xml version="1.0"?><transcript>'
        '<text start="0" dur="1">hello &amp;amp; welcome</text>'
        '<text start="1" dur="1">line &lt;b&gt;two&lt;/b&gt;</text>'
        '<text start="2" dur="1">  </text>'
        "</transcript>"
    )
    out = captions._parse_timedtext(xml)
    assert "hello & welcome" in out
    assert "line two" in out  # tags stripped after unescape
    assert out.count("  ") == 0  # blank segment dropped, single-spaced


def test_pick_track_prefers_manual_english():
    tracks = [
        {"languageCode": "es", "kind": "", "baseUrl": "es"},
        {"languageCode": "en", "kind": "asr", "baseUrl": "en-auto"},
        {"languageCode": "en", "kind": "", "baseUrl": "en-manual"},
    ]
    assert captions._pick_track(tracks)["baseUrl"] == "en-manual"


def test_pick_track_falls_back_to_first():
    tracks = [{"languageCode": "de", "kind": "", "baseUrl": "de"}]
    assert captions._pick_track(tracks)["baseUrl"] == "de"
