"""Standalone aiohttp server for the LLM admin page.

Runs on its own port (default 8004) so the extension stays decoupled from
core/http_server.py — adding/removing it never touches upstream code.
"""

import asyncio

from aiohttp import web

from config.logger import setup_logging
from core.utils.util import get_local_ip
from .handler import AdminHandler

TAG = __name__
logger = setup_logging()


async def start(config: dict) -> None:
    ext_config = config.get("extensions", {}).get("llm_admin", {}) or {}
    host = ext_config.get("ip", config.get("server", {}).get("ip", "0.0.0.0"))
    port = int(ext_config.get("port", 8004))

    try:
        handler = AdminHandler(config)
        app = web.Application()
        app.add_routes([
            web.get("/admin", handler.handle_page),
            web.get("/admin/", handler.handle_page),
            web.get("/admin/api/llm-calls", handler.handle_calls),
            web.get("/admin/api/llm-calls/session/{key}", handler.handle_session_detail),
            web.get("/admin/api/stats", handler.handle_stats),
            web.options("/admin/api/llm-calls", handler.handle_options),
            web.options("/admin/api/llm-calls/session/{key}", handler.handle_options),
            web.options("/admin/api/stats", handler.handle_options),
        ])

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.bind(tag=TAG).info(
            "LLM 管理页面\thttp://{}:{}/admin",
            get_local_ip(),
            port,
        )

        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        logger.bind(tag=TAG).error(f"LLM admin server failed to start: {e}")
        import traceback
        logger.bind(tag=TAG).error(traceback.format_exc())
