"""Agent Fleet: a managed fleet of LangGraph agents for AgeniusDesk.

The dashboard loader imports this package and mounts `<package>.router`. `router`
is exposed lazily (PEP 562 `__getattr__`) so that importing a submodule for
`langgraph dev` / Studio (e.g. agent.studio) does NOT drag in the FastAPI router,
aiosqlite, or the host. Accessing the `router` attribute triggers the import then.
"""

from __future__ import annotations


def __getattr__(name):
    if name == "router":
        import sys

        from .router import router

        # Importing the `.router` submodule binds it as this package's `router`
        # attribute (Python's automatic submodule binding), which would shadow this
        # __getattr__ on a second access and hand callers the MODULE, not the
        # APIRouter (the loader reads `mod.router` twice: hasattr, then
        # include_router). Overwrite the attribute with the real APIRouter so every
        # subsequent access resolves correctly.
        setattr(sys.modules[__name__], "router", router)
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
