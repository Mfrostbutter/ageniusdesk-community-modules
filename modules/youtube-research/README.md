# YouTube Research

Drop a YouTube link. The module transcribes it from captions, has your
configured AI model write a structured breakdown (and an optional deep dive),
and saves the artifacts into your AgeniusDesk notes vault under
`research/<topic>/`. The view mirrors the main AgeniusDesk Research tab; the
difference is that everything is saved inside the containerized harness vault,
not on your local disk.

## What it does

1. **Transcribe (captions-only).** Uses yt-dlp in-process to pull the caption
   track (manual subtitles first, then auto-generated), preferring json3. No GPU,
   no whisper, no sidecar. yt-dlp is provided by the AgeniusDesk runtime. The
   video must have captions or subtitles.
2. **Single pass.** Sends the transcript to your configured AgeniusDesk AI
   provider and gets a dense breakdown: thesis, key concepts, architectures,
   golden nuggets, tools, and how to apply it.
3. **Deep dive (optional).** A second, transcript-grounded pass that extracts the
   depth the summary omits: exact numbers, verbatim command/tool sequences,
   design rationale, quotes, and what the video underspecifies.
4. **Save to the harness vault.** Each run writes `transcript.md`, `BREAKDOWN.md`,
   (and `BREAKDOWN-deep.md` when run) plus `meta.json` into
   `research/<destination>/<channel>/<title>/` in your notes vault,
   through the indexed notes API, so they are full-text-searchable in the Harness.

## The view

Same as the main app: a job list on the left, a detail pane on the right with
**Breakdown / Deep dive / Transcript** tabs and rendered markdown. A toolbar with
the URL and a **Single pass / Deep dive** toggle; it also shows the provider and
model that writes the breakdown (configured in Settings > AI). Per run you can
run a deep dive, **View in Harness** (open the run's note in the vault),
download the markdown, move it to another topic folder, or delete it.

## Filing: auto-classify or pick a folder

- **Leave the destination blank** and the breakdown is auto-classified: the LLM
  picks the best-fit topic folder from your existing `research/` folders (or
  proposes a concise new one) and the note is moved there, out of `_inbox`.
- **Pick or type a folder** in the destination box to file there explicitly.
- **Create your own folders** with the **+ Folder** button (nest with `/`, e.g.
  `ai/agents`). Custom folders immediately become both destination options and
  auto-classify targets, so the classifier "drops it in the right place" for
  your own taxonomy. You own the taxonomy; the starter folders are just a seed
  you can rename, remove, or extend.

## Configuration

- **AI provider:** uses your AgeniusDesk Assistant provider + model + key
  (Settings -> AI), with the per-run provider/model pickers. If the global key is
  set as a `$REF` in Models, it is resolved by the conventional secret name
  (`$OPEN_ROUTER_KEY` / `$OPEN_AI_KEY` / `$ANTHROPIC_KEY`).
- No sidecar, no GPU, no extra environment variables in v1.

## Declared capabilities

| Capability | Declared |
|---|---|
| network | `*.youtube.com`, `*.youtu.be` (captions), `api.openai.com` / `api.anthropic.com` / `openrouter.ai` (breakdown) |
| filesystem | writes under the vault's `research/` subtree |
| subprocess | none |
| secrets | `ANTHROPIC_KEY` / `OPEN_AI_KEY` / `OPEN_ROUTER_KEY` (optional; resolved via the Assistant config) |

Filing goes through the indexed notes API rather than raw file writes, so
AgeniusDesk's static scanner reports the declared `research/` write path as an
over-declaration (INFO). That is expected: the scanner cannot see writes that go
through a host API.

## Notes and limitations

- Captions-only. A whisper fallback for videos without captions is deferred.
- The recent-runs list is per session; the durable record is the set of notes in
  your vault (searchable in the Harness). A restart clears the list, not the notes.
- Caption fetching depends on YouTube; if it changes its watch page you get a
  clear error rather than a silent empty transcript.

## License

MIT.
