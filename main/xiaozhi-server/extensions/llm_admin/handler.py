"""aiohttp handlers for the LLM-call admin page.

Routes (registered in server.py):
  GET /admin                                  -> HTML page
  GET /admin/api/llm-calls                    -> session-aggregated list
  GET /admin/api/llm-calls/session/{key}      -> all calls in one session
  GET /admin/api/stats                        -> total / today counts and cost

Reads only; the writer is in extensions/llm_admin/logger.py.
"""

import asyncio
import datetime as dt
import json
import sqlite3
from pathlib import Path

from aiohttp import web

from core.api.base_handler import BaseHandler
from .logger import db_path

_ADMIN_HTML = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")

# NULL session_id rows can't be aggregated with anything else, so we synthesize
# a per-row "solo:<id>" key. The detail route falls back to a by-id lookup when
# it sees that prefix.
_SOLO_KEY_PREFIX = "solo:"
_SESS_KEY_SQL = f"COALESCE(session_id, '{_SOLO_KEY_PREFIX}' || id)"


def _open_db() -> sqlite3.Connection | None:
    """Open the DB; return None if it doesn't exist yet (page just shows empty)."""
    path = db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        conn.execute("ALTER TABLE llm_calls ADD COLUMN tools TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def _build_filter_sql(params: dict) -> tuple[str, list]:
    conditions: list[str] = []
    args: list = []
    if params.get("session_id"):
        conditions.append("session_id LIKE ?")
        args.append(f"%{params['session_id']}%")
    if params.get("model"):
        conditions.append("model_name LIKE ?")
        args.append(f"%{params['model']}%")
    if params.get("call_type"):
        conditions.append("call_type = ?")
        args.append(params["call_type"])
    if params.get("date_from"):
        d = dt.datetime.strptime(params["date_from"], "%Y-%m-%d")
        conditions.append("ts >= ?")
        args.append(d.timestamp())
    if params.get("date_to"):
        d = dt.datetime.strptime(params["date_to"], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
        conditions.append("ts <= ?")
        args.append(d.timestamp())
    if params.get("errors_only") == "1":
        conditions.append("error IS NOT NULL AND error != ''")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, args


def _query_sessions(params: dict) -> list[dict]:
    conn = _open_db()
    if conn is None:
        return []
    where, args = _build_filter_sql(params)
    limit = min(int(params.get("limit", 200)), 1000)

    sql = f"""
        SELECT
            {_SESS_KEY_SQL} AS sess_key,
            MAX(session_id) AS session_id,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            COUNT(*) AS calls,
            SUM(prompt_tokens) AS prompt_tokens,
            SUM(completion_tokens) AS completion_tokens,
            SUM(cost) AS cost,
            SUM(duration_ms) AS duration_ms,
            MAX(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS has_error,
            GROUP_CONCAT(DISTINCT model_name) AS models,
            GROUP_CONCAT(DISTINCT call_type) AS call_types
        FROM llm_calls
        {where}
        GROUP BY sess_key
        ORDER BY last_ts DESC
        LIMIT ?
    """
    rows = [dict(r) for r in conn.execute(sql, [*args, limit]).fetchall()]

    if rows:
        keys = [r["sess_key"] for r in rows]
        ph = ",".join("?" * len(keys))
        latest = {
            r["sess_key"]: r["input_messages"]
            for r in conn.execute(
                f"""
                SELECT {_SESS_KEY_SQL} AS sess_key, input_messages
                FROM llm_calls
                WHERE id IN (
                    SELECT MAX(id) FROM llm_calls
                    WHERE {_SESS_KEY_SQL} IN ({ph})
                    GROUP BY {_SESS_KEY_SQL}
                )
                """,
                keys,
            ).fetchall()
        }
        for r in rows:
            r["last_input"] = latest.get(r["sess_key"])

    conn.close()
    return rows


def _query_session_detail(sess_key: str) -> dict | None:
    conn = _open_db()
    if conn is None:
        return None
    if sess_key.startswith(_SOLO_KEY_PREFIX):
        try:
            rid = int(sess_key[len(_SOLO_KEY_PREFIX):])
        except ValueError:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT * FROM llm_calls WHERE id = ?", (rid,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM llm_calls WHERE session_id = ? ORDER BY ts ASC, id ASC",
            (sess_key,),
        ).fetchall()
    conn.close()

    if not rows:
        return None
    calls = [dict(r) for r in rows]
    total_cost = sum((c.get("cost") or 0.0) for c in calls)
    return {
        "sess_key": sess_key,
        "session_id": calls[0].get("session_id") or sess_key,
        "first_ts": min(c["ts"] for c in calls),
        "last_ts": max(c["ts"] for c in calls),
        "calls_total": len(calls),
        "prompt_tokens_total": sum((c.get("prompt_tokens") or 0) for c in calls),
        "completion_tokens_total": sum((c.get("completion_tokens") or 0) for c in calls),
        "cost_total": total_cost if total_cost > 0 else None,
        "duration_ms_total": sum((c.get("duration_ms") or 0) for c in calls),
        "has_error": any(c.get("error") for c in calls),
        "calls": calls,
    }


def _query_stats() -> dict:
    conn = _open_db()
    if conn is None:
        return {"total_calls": 0, "today_calls": 0, "total_cost": None, "today_cost": None}

    now = dt.datetime.now()
    today_ts = dt.datetime(now.year, now.month, now.day).timestamp()
    total = conn.execute("SELECT COUNT(*), SUM(cost) FROM llm_calls").fetchone()
    today = conn.execute(
        "SELECT COUNT(*), SUM(cost) FROM llm_calls WHERE ts >= ?", (today_ts,)
    ).fetchone()
    conn.close()

    return {
        "total_calls": total[0] or 0,
        "total_cost": total[1],
        "today_calls": today[0] or 0,
        "today_cost": today[1],
    }


def _json_response(payload, *, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
        charset="utf-8",
    )


async def _run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class AdminHandler(BaseHandler):
    async def handle_page(self, request: web.Request) -> web.Response:
        return web.Response(text=_ADMIN_HTML, content_type="text/html", charset="utf-8")

    async def handle_calls(self, request: web.Request) -> web.Response:
        rows = await _run_blocking(_query_sessions, dict(request.rel_url.query))
        resp = _json_response(rows)
        self._add_cors_headers(resp)
        return resp

    async def handle_session_detail(self, request: web.Request) -> web.Response:
        key = request.match_info.get("key", "")
        data = await _run_blocking(_query_session_detail, key)
        resp = (
            _json_response({"error": "not found"}, status=404)
            if data is None
            else _json_response(data)
        )
        self._add_cors_headers(resp)
        return resp

    async def handle_stats(self, request: web.Request) -> web.Response:
        stats = await _run_blocking(_query_stats)
        resp = _json_response(stats)
        self._add_cors_headers(resp)
        return resp
