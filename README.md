# AgeniusDesk Community Modules

A monorepo of community modules for [AgeniusDesk CE](https://github.com/Mfrostbutter/ageniusdesk-ce).
One repo, many modules: each lives under `modules/<id>/` and is installed
independently through AgeniusDesk's two-phase inspect/install flow.

## Modules

| Module | id | What it does |
|---|---|---|
| [YouTube Research](modules/youtube-research/) | `youtube-research` | Paste a YouTube link, transcribe it from captions, generate a structured breakdown, and auto-file it into your notes vault under `research/<topic>/`. |

## Installing a module

In AgeniusDesk: **Settings -> Modules -> Install a community module**.

1. Enter this repo (`Mfrostbutter/ageniusdesk-community-modules`) and click **Discover**.
2. Pick a module from the list and click **Inspect**. AgeniusDesk downloads the
   repo, runs a static scan of just that module, and shows what it declares vs
   what its code does.
3. Review the capability and scan report, consent, and **Install**. Restart the
   app to activate.

> Heuristic review, not a sandbox. AgeniusDesk's scan catches low-effort or
> accidental danger and forces an informed-consent moment, but community modules
> run in-process with full access. Only install modules you trust.

## Repo layout

```
modules/
  <module-id>/
    manifest.json     # id, name, version, min_app_version, capabilities, frontend
    __init__.py       # exposes `router` (a FastAPI APIRouter)
    ...               # the module's own code
    static/           # optional: HTML/JS view served at /modules/<id>/static/
    README.md
```

Each module is **self-contained**: it does not import from sibling modules or
shared repo-root code, because AgeniusDesk installs only the one `modules/<id>/`
subtree. See [CONTRIBUTING.md](CONTRIBUTING.md) for the module contract.

## License

MIT. See [LICENSE](LICENSE).
