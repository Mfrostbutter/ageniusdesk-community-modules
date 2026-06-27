"""Prompts for the breakdown and the auto-filing classifier."""

from __future__ import annotations

import json

# ── Single-pass breakdown ─────────────────────────────────────────────────────

SINGLE_PASS_SYSTEM = (
    "You are a sharp technical analyst. You turn a video transcript into a dense, "
    "skimmable breakdown that a busy engineer can read in two minutes and walk away "
    "knowing what the video actually teaches and how to use it. You are grounded in "
    "the transcript: you never invent specifics that are not there. You write in "
    "plain markdown, terse, no filler, no preamble, no sign-off."
)


def single_pass_prompt(title: str, channel: str, url: str, transcript: str) -> str:
    """Build the breakdown user prompt from the transcript and metadata."""
    return f"""Break down this video transcript.

Title: {title or "(unknown)"}
Channel: {channel or "(unknown)"}
URL: {url}

Write the breakdown as markdown with these sections (omit a section only if the
transcript truly has nothing for it):

## TL;DR
3-5 bullets: what this video is and the single most useful thing in it.

## Key concepts
The core ideas, each with a one-line explanation grounded in what was said.

## How it works
The architecture, workflow, or mechanism the video describes, in order.

## Concrete details
Verbatim-grounded specifics worth keeping: names, numbers, commands, tools,
versions, settings, gotchas. Do not vague these up.

## How to apply it
Actionable steps for someone who wants to use this. Be specific.

Transcript:
\"\"\"
{transcript}
\"\"\"
"""


# ── Auto-filing classifier ────────────────────────────────────────────────────

CLASSIFY_SYSTEM = (
    "You file research notes into an existing topic taxonomy. You pick the single "
    "best-fit topic from the provided list ONLY - never invent a new topic. If "
    "nothing fits with reasonable confidence, return an empty topic so the note "
    "stays in the inbox for manual filing. You return STRICT JSON and nothing else."
)


def classify_prompt(title: str, channel: str, breakdown: str, topics: list[str]) -> str:
    """Constrain classification to the existing topic folders (candidate set)."""
    topic_list = "\n".join(f"- {t}" for t in topics) or "(none)"
    schema = json.dumps(
        {
            "topic": "<one of the topics, or empty string>",
            "tags": ["3-6 short kebab-case tags"],
            "confidence": "0.0-1.0",
        }
    )
    return f"""Classify this research note into ONE of the existing topics below.

Existing topics (choose exactly one, or "" if none fit):
{topic_list}

Rules:
- Pick the single best-fit topic from the list. Do NOT invent a new topic.
- If no topic is a confident fit, set "topic" to "" (it will stay in the inbox).
- Tags are free-form, 3-6 short kebab-case keywords describing the content.
- confidence is your fit confidence for the chosen topic (0.0-1.0).

Return STRICT JSON matching this shape, nothing else:
{schema}

Note title: {title or "(unknown)"}
Channel: {channel or "(unknown)"}

Breakdown:
\"\"\"
{breakdown[:6000]}
\"\"\"
"""
