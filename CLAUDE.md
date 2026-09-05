# CLAUDE.md

## What this repo is

An MIT-licensed monorepo of **community modules for AgeniusDesk CE**. Each module
is independently installable: it lives under `modules/<id>/` with a
`manifest.json`, an `__init__.py` exposing a FastAPI `APIRouter` named `router`,
and an optional `static/` view. AgeniusDesk installs a module through its
two-phase **inspect/install** flow: it downloads the repo, statically scans just
the one module subtree, shows the operator declared-vs-detected capabilities,
and on consent copies that single `modules/<id>/` subtree into its own
`data/modules/<id>/`.

[CONTRIBUTING.md](CONTRIBUTING.md) is the authoring contract. Read it before
creating or modifying any module; the rules below are a summary, not a
replacement.

## Hard rules (from the contract)

1. **Self-contained modules.** A module may only use code inside its own
   `modules/<id>/` subtree, plus the AgeniusDesk host (`backend.*`, available
   because modules run in-process). **Never import from a sibling module or
   from shared repo-root code**; nothing outside the subtree exists after
   install.
2. **Manifest schema.** `manifest.json` at the module root with at least `id`
   (must equal the directory name, it is the install key) and `name`. Declare
   `min_app_version`, `capabilities` (`network.hosts` allowlist,
   `filesystem.write_paths`, `subprocess`, `env`), `secrets_required`, and a
   `frontend.nav` entry if the module ships a UI.
3. **Declare capabilities truthfully.** The static scanner reconciles the
   `capabilities` block against the code; undeclared network/subprocess/env/
   filesystem use becomes a HIGH finding, and `eval`/`exec`/dynamic imports are
   CRITICAL. Keep it boring: literal hosts, declared write paths.
4. **Frontend is a sandboxed iframe.** Views are HTML fragments (host CSS and
   theme variables are injected) that talk to the host only via
   `window.AgeniusDesk` (`fetch`, `notify`, `navigate`, `openInHarness`). No
   host DOM access.
5. **Dependencies.** Standard library plus what AgeniusDesk ships (`httpx`,
   `fastapi`, `pydantic`, `yt-dlp`). No per-module pip installs happen; an
   unmet import means the module fails to load.

## Current modules

| id | What it is |
|---|---|
| `youtube-research` | YouTube link -> caption transcript -> LLM breakdown filed into the notes vault. |
| `proxmox` | Read-first Proxmox VE cluster view (nodes, VMs, LXCs) with cluster resource stats, gated guest power actions, and gated provisioning (create/clone/delete); token injected host-side. |

## Conventions

- Tests co-locate at `modules/<id>/tests/` (see `pytest.ini`); lint with `ruff`.
- Each module ships its own `README.md`.
- Never use em-dashes in prose or docs.
