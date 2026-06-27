# Contributing a module

A module is a directory under `modules/<id>/`. AgeniusDesk installs it by
downloading this repo, scanning just your subdir, and (on consent) copying that
one subtree into its `data/modules/<id>/`. So the contract is small and the
hard rule is self-containment.

## The contract

1. **`manifest.json`** at the module root with at least `id` and `name`. The
   `id` must match the directory name and is the install key. Declare
   `min_app_version`, `capabilities`, `secrets_required`, and a `frontend.nav`
   entry if the module has a UI.
2. **`__init__.py`** exposing a `router` (a FastAPI `APIRouter`), conventionally
   prefixed `/api/<id>`.
3. **Self-contained.** Do not import from sibling modules or from shared
   repo-root code. Only your `modules/<id>/` subtree is installed; anything
   outside it will not exist at runtime. You MAY import from the AgeniusDesk host
   (`backend.*`) since modules run in-process.

## Declaring capabilities

The `capabilities` block is your honest statement of what the module does.
AgeniusDesk's static scanner reconciles it against your code and shows the
operator any gap. Declare truthfully; an undeclared capability the scanner
detects becomes a HIGH "undeclared capability" finding.

```jsonc
"capabilities": {
  "network": { "enabled": true, "hosts": ["*.youtube.com", "api.openai.com"] },
  "filesystem": { "write_paths": ["research"] },  // relative to the vault/data root
  "subprocess": false,
  "env": ["SOME_OPTIONAL_URL"]
}
```

- `network.hosts` is an allowlist (globs allowed). Empty + `enabled:true` means
  "any host" and is itself flagged.
- `filesystem.write_paths` are paths the module writes. Writing elsewhere is a
  finding. (Writes that go through a host API rather than raw `open()` cannot be
  detected by the scanner; declaring them is still correct intent.)
- `secrets_required` declares credentials; the operator is prompted for them.

## What the scanner flags

| Severity | Examples |
|---|---|
| CRITICAL | `eval`/`exec`, `os.system`, dynamic imports, `pickle` loads, `ctypes` |
| HIGH | undeclared network/subprocess, off-allowlist hosts, raw sockets, writes outside declared paths, undeclared env reads, secret-store access |
| MEDIUM | out-of-tree reads, dynamic attribute access, large opaque literals |
| INFO | over-declaration (declared a capability the code never uses) |

Keep it boring: literal hosts, declared writes, no dynamic imports. A clean scan
is the easiest module to get an operator to install.

## Dependencies

Modules run inside the AgeniusDesk Python environment. Prefer the standard
library plus what AgeniusDesk already ships (`httpx`, `fastapi`, `pydantic`).
Avoid extra PyPI dependencies; AgeniusDesk does not pip-install per-module.
