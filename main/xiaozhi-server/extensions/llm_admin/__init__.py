"""LLM call logging + admin viewer.

Single touchpoint to enable the feature:

    from extensions.llm_admin import install
    install(config)  # returns an asyncio.Task running the admin HTTP server

`install()` is safe to call when the extension is disabled — it just no-ops.
Enable via config:

    extensions:
      llm_admin:
        enabled: true   # default false
        port: 8004
"""

import asyncio


def is_enabled(config: dict) -> bool:
    return bool(
        (config.get("extensions") or {}).get("llm_admin", {}).get("enabled", False)
    )


def install(config: dict) -> asyncio.Task | None:
    if not is_enabled(config):
        return None
    from .patches import apply_patches
    from . import server

    apply_patches()
    return asyncio.create_task(server.start(config))
