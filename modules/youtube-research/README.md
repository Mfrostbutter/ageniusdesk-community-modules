# YouTube Research

Paste a YouTube link. The module fetches the video's caption track, has your
configured AI model write a structured breakdown, and files it into your
AgeniusDesk notes vault under `research/<topic>/` - classified into one of your
existing topic folders, with tags in the frontmatter.

## What it does

1. **Transcribe (captions-only).** Uses yt-dlp in-process to pull the video's
   caption track (manual subtitles first, then auto-generated), preferring the
   json3 format. No GPU, no whisper, no sidecar. yt-dlp is provided by the
   AgeniusDesk runtime. The video must have captions or subtitles; videos with
   captions disabled are not supported in v1.
2. **Break down.** Sends the transcript to your configured AgeniusDesk AI
   provider (the same one the Assistant uses) and gets back a dense markdown
   breakdown: TL;DR, key concepts, how it works, concrete details, how to apply.
3. **Intake → classify → auto-file.** The breakdown is written to
   `research/inbox/` first (nothing is lost if classification fails), then the
   model classifies it into one of your **existing** `research/` topic folders
   (it never invents a topic) and the note is moved there with tags written into
   the frontmatter. No confident fit → it stays in `research/inbox/` for manual
   filing.

Breakdowns are written through the host notes vault, so they are first-class,
full-text-searchable notes - not loose files.

## Starter taxonomy

On first run it seeds these folders under `research/` (all operator-editable -
add or remove folders and the classifier adapts, because it reads the live
folder list as its candidate set):

`inbox` (intake, never a target), `ai-and-llms`, `automation-and-n8n`,
`business-and-marketing`, `engineering-and-devtools`, `productivity`, `misc`.

## Configuration

- **AI provider:** uses your AgeniusDesk Assistant provider + model + key
  (Settings → AI). No separate key needed; the `secrets_required` entries are
  optional and only there so the module card shows which provider key applies.
- No other configuration. v1 has no sidecar and no environment variables.

## Declared capabilities

| Capability | Declared |
|---|---|
| network | `*.youtube.com`, `*.youtu.be` (captions), `api.openai.com` / `api.anthropic.com` / `openrouter.ai` (breakdown) |
| filesystem | writes under the vault's `research/` subtree |
| subprocess | none |
| secrets | `ANTHROPIC_KEY` / `OPEN_AI_KEY` / `OPEN_ROUTER_KEY` (optional; resolved via the Assistant config) |

Filing goes through the indexed notes API rather than raw file writes, so
AgeniusDesk's static scanner reports the declared `research/` write path as an
over-declaration (INFO) - that is expected and documented: the scanner cannot
see writes that go through a host API.

## Limitations

- Captions-only. A whisper fallback for videos without captions is deferred.
- Caption fetching depends on YouTube's watch-page shape; if YouTube changes it
  or serves a consent interstitial, you get a clear error rather than a silent
  empty transcript.

## License

MIT.
