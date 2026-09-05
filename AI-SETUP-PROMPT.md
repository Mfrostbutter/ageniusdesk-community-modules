# AI Setup Prompt: scaffold a new AgeniusDesk community module

Copy everything in the block below into your AI assistant (Claude, ChatGPT,
Cursor, etc.), replace the placeholders in the first paragraph, and let it
scaffold the module. The prompt encodes the module contract from
[CONTRIBUTING.md](CONTRIBUTING.md); the assistant should still read that file
if it has repo access.

```text
You are scaffolding a new community module for AgeniusDesk CE inside the
ageniusdesk-community-modules repo. The module id is `<MODULE_ID>` and it
should: <ONE_SENTENCE_DESCRIPTION_OF_WHAT_THE_MODULE_DOES>.

Follow this contract exactly. It comes from the repo's CONTRIBUTING.md; if you
can read that file, treat it as the authority over this summary.

HOW MODULES ARE INSTALLED (why the rules exist):
AgeniusDesk downloads the repo, statically scans ONLY the `modules/<id>/`
subtree, shows the operator what the manifest declares versus what the code
actually does, and on consent copies that ONE subtree into its own
`data/modules/<id>/`. Anything outside the subtree does not exist at runtime.

HARD RULES:
1. Everything lives under `modules/<MODULE_ID>/`. The module must be fully
   self-contained: never import from sibling modules or from shared repo-root
   code. Importing from the AgeniusDesk host (`backend.*`) is allowed because
   modules run in-process.
2. `modules/<MODULE_ID>/manifest.json` with at least `id` and `name`. The `id`
   must equal the directory name (it is the install key). Also declare:
   - `version`, `description`, `min_app_version`
   - `capabilities`: an honest statement of what the code does. Shape:
       "capabilities": {
         "network": { "enabled": true|false, "hosts": ["api.example.com"] },
         "filesystem": { "write_paths": ["some/relative/path"] },
         "subprocess": false,
         "env": ["OPTIONAL_ENV_VAR"]
       }
     `network.hosts` is an allowlist (globs allowed); empty with enabled:true
     means "any host" and is itself flagged. `filesystem.write_paths` are
     relative to the vault/data root. The static scanner reconciles this block
     against the code: undeclared network, subprocess, env reads, or writes
     outside declared paths are HIGH findings; `eval`/`exec`, `os.system`,
     dynamic imports, pickle loads, or ctypes are CRITICAL. Declaring a
     capability the code never uses is only an INFO finding, so declare
     truthfully rather than minimally.
   - `secrets_required`: a list of credentials the module needs; the operator
     is prompted for them at install.
   - `frontend.nav` only if the module ships a UI view.
3. `modules/<MODULE_ID>/__init__.py` must expose `router`, a FastAPI
   `APIRouter`, with routes conventionally prefixed `/api/<MODULE_ID>`.
4. Dependencies: standard library plus what AgeniusDesk already ships
   (`httpx`, `fastapi`, `pydantic`, and `yt-dlp` for media/transcript work).
   Do NOT add other PyPI dependencies; AgeniusDesk does not pip-install per
   module, so an unmet import makes the module fail to load.
5. Keep the code scanner-friendly: literal host strings, declared write
   paths, no dynamic imports, no eval/exec/subprocess unless declared.

FRONTEND (only if the module has a UI):
- Ship the view HTML and optional `module.js` under
  `modules/<MODULE_ID>/static/`, and point `frontend.nav.view` at the HTML.
- The view is loaded in a sandboxed iframe (allow-scripts, NOT
  allow-same-origin): no host DOM, window, cookies, or storage.
- Write an HTML FRAGMENT, not a full page. Host CSS is injected: component
  classes (.btn, .btn-primary, .btn-sm, .input, .card) and theme variables
  (var(--accent), var(--bg-panel), var(--text-secondary), var(--radius))
  are available. `module.js` loads as <script type="module">.
- Talk to the host only through `window.AgeniusDesk`:
  `await AgeniusDesk.fetch(path, opts)` (same-origin /api/ paths only; host
  adds auth and CSRF; body as string, headers as plain object),
  `AgeniusDesk.notify(message, level)`, `AgeniusDesk.navigate(viewName)`,
  `AgeniusDesk.openInHarness(relPath)`.
- `prompt`, `confirm`, and `window.open` work; the iframe auto-resizes to
  content height. A fresh iframe is created on every view open, so timers
  and listeners are torn down on navigate-away.

DELIVERABLES:
- modules/<MODULE_ID>/manifest.json
- modules/<MODULE_ID>/__init__.py exposing `router`
- the module's own code files
- modules/<MODULE_ID>/static/ (only if there is a UI)
- modules/<MODULE_ID>/README.md (what it does, endpoints, secrets, install)
- modules/<MODULE_ID>/tests/ with at least one test that imports the router
  and exercises the core logic without network access

Before finishing, re-check: does the manifest declare every capability the
code uses, and does the code use nothing outside its own subtree except the
AgeniusDesk host? Use modules/youtube-research/ and modules/proxmox/ as
reference implementations.
```
