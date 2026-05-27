"""SQLite writer for LLM call logs.

Writes one row per LLM call to `data/llm_calls.db` (project-root relative).
Schema is created on first connection; the `tools` column is added defensively
for older DBs.
"""

import json
import sqlite3
import time
import threading
from pathlib import Path

_DB_PATH = Path(__file__).parents[2] / "data" / "llm_calls.db"
_local = threading.local()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    session_id        TEXT,
    call_type         TEXT    NOT NULL,
    model_name        TEXT,
    base_url          TEXT,
    input_messages    TEXT,
    output_content    TEXT,
    tools             TEXT,
    tool_calls        TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cost              REAL,
    prompt_cost       REAL,
    completion_cost   REAL,
    usage_json        TEXT,
    duration_ms       REAL,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_session ON llm_calls(session_id);
"""


def db_path() -> Path:
    return _DB_PATH


def _conn() -> sqlite3.Connection:
    if not getattr(_local, "conn", None):
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        conn.executescript(_CREATE_TABLE)
        try:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN tools TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        _local.conn = conn
    return _local.conn


def _parse_usage(usage) -> dict:
    """Normalize a usage object (dict / pydantic / attr-bag) to a plain dict."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return {k: getattr(usage, k, None) for k in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cost", "is_byok", "cost_details",
        "completion_tokens_details", "prompt_tokens_details",
    )}


def log_call(
    *,
    session_id: str,
    call_type: str,
    model_name: str,
    base_url: str,
    input_messages: list,
    output_content: str = "",
    tools=None,
    tool_calls=None,
    usage=None,
    duration_ms: float | None = None,
    error: str | None = None,
):
    raw = _parse_usage(usage)
    details = raw.get("cost_details") or {}

    conn = _conn()
    conn.execute(
        """
        INSERT INTO llm_calls (
            ts, session_id, call_type, model_name, base_url,
            input_messages, output_content, tools, tool_calls,
            prompt_tokens, completion_tokens, total_tokens,
            cost, prompt_cost, completion_cost,
            usage_json, duration_ms, error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            time.time(),
            session_id or None,
            call_type,
            model_name,
            base_url,
            json.dumps(input_messages, ensure_ascii=False),
            output_content,
            json.dumps(tools, ensure_ascii=False) if tools else None,
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
            raw.get("prompt_tokens"),
            raw.get("completion_tokens"),
            raw.get("total_tokens"),
            raw.get("cost"),
            details.get("upstream_inference_prompt_cost"),
            details.get("upstream_inference_completions_cost"),
            json.dumps(raw, ensure_ascii=False) if raw else None,
            duration_ms,
            error,
        ),
    )
    conn.commit()
