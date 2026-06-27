"""LLM prompts for the breakdown and the deep dive.

Ported from the AgeniusDesk research module so the OUTPUT matches the version on
the main app exactly:
  - SINGLE_PASS: one structured breakdown (thesis, concepts, architectures, how
    to apply). The executive summary.
  - DEEP_DIVE: one transcript-grounded extraction of the depth the summary omits
    (exact numbers, verbatim command/tool sequences, rationale, quotes, gaps).
"""

from __future__ import annotations

# ── Single pass ──────────────────────────────────────────────────────────────

SINGLE_PASS_SYSTEM = (
    "You are a senior engineer and technical analyst. You turn a raw video "
    "transcript into a dense, accurate research breakdown that a builder can "
    "learn from and put into practice. You write plainly, no corporate filler, "
    "no em-dashes. You never invent claims the transcript does not support. "
    "When the transcript has artifacts (repeated lines from caption VAD), you "
    "silently de-duplicate them."
)


def single_pass_prompt(title: str, channel: str, url: str, transcript: str) -> str:
    return f"""Produce a research breakdown of this video as GitHub-flavored markdown.

Video: {title or "(unknown title)"}
Channel: {channel or "(unknown)"}
URL: {url}

Structure the breakdown with these sections (use ## headers):

1. **One-line thesis** - the single core argument.
2. **Key concepts** - each major idea, defined clearly. Use subsections.
3. **Architectures / systems / workflows** - any concrete technical structure
   described (diagrams in prose, pipelines, loops, components). Be precise.
4. **Notable techniques and golden nuggets** - the practical, actionable bits.
5. **Tools, people, and resources named** - with a one-line note on each.
6. **How this applies to our environment** - THIS IS THE MOST IMPORTANT SECTION.
   Map the video's ideas onto a builder's own AI/automation stack: where the
   concept already exists, what is worth adopting, and the concrete next step.
   Be specific and opinionated.

Rules:
- Faithful to the transcript. Do not fabricate.
- Dense and skimmable. Tables and bullets over paragraphs where it helps.
- No em-dashes. No hype words (groundbreaking, revolutionary, game-changing).

Transcript:
---
{transcript}
---
"""


# ── Deep dive (single transcript-grounded extraction) ────────────────────────

DEEP_DIVE_SYSTEM = (
    "You are extracting actionable technical depth from a video transcript for "
    "a working engineer who already understands the domain. You do the work an "
    "executive summary does not: you surface every exact number, the verbatim "
    "sequence of tool or command invocations, and the reason behind each "
    "non-obvious choice. You quote the transcript rather than paraphrasing it "
    "into vague competence. You never launder a speaker's hedge ('usually', "
    "'about', 'depends on your printer') into apparent precision, and you never "
    "invent a rationale the speaker did not give. No em-dashes. No hype words. "
    "No emoji."
)


def deep_dive_prompt(title: str, channel: str, url: str, transcript: str) -> str:
    return f"""You are extracting actionable technical depth from this video transcript.
Your audience is a working engineer who already understands the domain. Skip
executive-summary framing; a separate shallow breakdown already covers that.
This document MUST do work the shallow file does not.

Video: {title or "(unknown title)"}
Channel: {channel or "(unknown)"}
URL: {url}

First, silently identify the domain (e.g. CAD / 3D printing, an AI/agent
framework, DevOps tooling, a programming tutorial). Adapt the section content to
that domain: "Numeric specifications" captures whatever quantitative values the
domain uses (offsets, angles, fillet radii, layer heights for CAD; temperatures,
token limits, timeouts, context windows, model params for AI; ports, replicas,
resource limits for infra). "Command / tool sequence" captures whatever the
operator actually does in order (Fusion tool invocations, CLI commands, API
calls, UI clicks, code edits).

Hard rules:
- NO "how this applies to my stack / AI tooling / environment" section. Tie-ins
  to unrelated tools are off-topic noise. Delete entirely.
- NO generic open questions ("set up a test server", "audit security"). Open
  questions must be DOMAIN-specific: things the video left underspecified about
  the actual workflow it demonstrated.
- NO bullets that merely restate the shallow breakdown. If you would repeat
  yourself, go deeper or cut it.
- Speaker hedges ("usually", "about", "depends") must be flagged as such. Do not
  launder uncertainty into apparent precision.
- When the transcript shows obvious transcription noise (a line repeated 2-3x, a
  unit mismatch between adjacent sentences like "10 degrees" then "4 millimeters"
  for the same field, or a numeric contradiction), prefer the corrected reading
  in every other section AND record what you corrected in the "Transcript noise
  observed" section. Never silently average or guess past a contradiction.
- Faithful to the transcript. Do not fabricate. If the speaker gives no reason
  for a choice, write "no rationale given" rather than inventing one.

Required sections, in this order, using ## headers:

## Numeric specifications
Every quantitative value the speaker states, in a table:
| Value | Units | What it controls | Why this value | Speaker certainty |
"Speaker certainty" is one of: stated as fact / stated as recommendation /
speaker hedged / derived. Include every offset, angle, radius, dimension,
clearance, gap, layer height, nozzle/tool size, temperature, timeout, limit,
count, or param the speaker names. Most tutorial numbers are recommendations,
not facts: a number the presenter picks to demonstrate is a recommendation; a
hedge ("usually", "about", a stated range) is "speaker hedged"; "stated as fact"
only when given with no hedge AND tied to a concrete external reason. "Derived"
for a value that follows mathematically from other stated values (show the math).

## Command / tool sequence
The verbatim operator actions in order, with their arguments. Format each step:
"<n>. <Tool> [<shortcut if given>] on <target>: <args>. -> <result>."

## Why these choices (design rationale)
For each non-obvious choice, the reason the speaker gives or that an engineer
should infer. One bullet per choice: lead with the choice, end with the reason.
If the speaker gives no reason, write "no rationale given". Do not invent one.

## Conflicts with established practice (if any)
If this video's recipe differs from common practice, call it out with both
values. Otherwise: "None observed."

## Direct quotes worth preserving
3-7 short verbatim quotes that capture the speaker's substantive POV, in double
quotes. Skip friendly filler.

## Transcript noise observed
Concrete transcription artifacts you corrected for (repeated lines, unit
mismatches, numeric contradictions, garbled tool names): the noise, then the
reading you used and why. If clean, write "None observed."

## Underspecified by the video
What a competent practitioner would need that the video did not cover. Flag the
ABSENCE of information, not divergence from your preferred values. Genuine gaps
the engineer would hit, not editorializing. Do not list "the software version is
not stated" as a generic gap.

## One-paragraph synthesis
About 80 words. The takeaway a working engineer would tell a colleague over
coffee. Not a marketing summary.

Output rules:
- GitHub-flavored markdown. Tables where the shape fits. No emoji. No em-dashes.
- Let substance set the length; pad nothing. Density beats length.
- If the video genuinely has no depth past the shallow breakdown, do not pad.
  Output exactly one line: "Not enough technical substance for a deep dive." and
  stop.

Transcript:
---
{transcript}
---
"""
