"""Monkey-patch the OpenAI LLM provider to log every call to SQLite.

Reimplements `response` and `response_with_functions` because the upstream
versions don't surface chunk-level usage to a hook point. Keeping the
duplicated logic here (instead of editing upstream) is the trade-off for a
rebase-safe layout — when upstream changes its OpenAI provider, update this
file, not the original.
"""

import time

from config.logger import setup_logging
from .logger import log_call

TAG = __name__
logger = setup_logging()

_APPLIED = False


def apply_patches() -> None:
    """Patch the OpenAI LLM provider's stream methods. Idempotent."""
    global _APPLIED
    if _APPLIED:
        return

    import core.providers.llm.openai.openai as oai_mod

    def response(self, session_id, dialogue, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        request_params = _build_request_params(self, dialogue, kwargs)
        self._apply_thinking_disabled(request_params)
        request_params["stream_options"] = {"include_usage": True}

        responses = self.client.chat.completions.create(**request_params)

        is_active = True
        output_parts: list[str] = []
        usage_obj = None
        error_msg: str | None = None
        t0 = time.monotonic()
        try:
            for chunk in responses:
                if getattr(chunk, "usage", None):
                    usage_obj = chunk.usage
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        output_parts.append(content)
                        yield content
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            responses.close()
            _safe_log(
                session_id=session_id,
                call_type="response",
                model_name=self.model_name,
                base_url=self.base_url,
                input_messages=dialogue,
                output_content="".join(output_parts),
                usage=usage_obj,
                duration_ms=(time.monotonic() - t0) * 1000,
                error=error_msg,
            )

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        request_params = _build_request_params(self, dialogue, kwargs, tools=functions)
        self._apply_thinking_disabled(request_params)
        request_params["stream_options"] = {"include_usage": True}

        stream = self.client.chat.completions.create(**request_params)

        output_parts: list[str] = []
        all_tool_calls: dict[int, dict] = {}
        usage_obj = None
        error_msg: str | None = None
        t0 = time.monotonic()
        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_obj = chunk.usage
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    if content:
                        output_parts.append(content)
                    if tool_calls:
                        _merge_tool_call_deltas(all_tool_calls, tool_calls)
                    yield content, tool_calls
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            stream.close()
            _safe_log(
                session_id=session_id,
                call_type="response_with_functions",
                model_name=self.model_name,
                base_url=self.base_url,
                input_messages=dialogue,
                output_content="".join(output_parts),
                tools=functions,
                tool_calls=list(all_tool_calls.values()) or None,
                usage=usage_obj,
                duration_ms=(time.monotonic() - t0) * 1000,
                error=error_msg,
            )

    oai_mod.LLMProvider.response = response
    oai_mod.LLMProvider.response_with_functions = response_with_functions
    _APPLIED = True
    logger.bind(tag=TAG).info("LLM admin: OpenAI provider patched for call logging")


def _build_request_params(self, dialogue, kwargs, *, tools=None) -> dict:
    params = {
        "model": self.model_name,
        "messages": dialogue,
        "stream": True,
    }
    if tools is not None:
        params["tools"] = tools
    optional = {
        "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        "temperature": kwargs.get("temperature", self.temperature),
        "top_p": kwargs.get("top_p", self.top_p),
        "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
    }
    for key, value in optional.items():
        if value is not None:
            params[key] = value
    return params


def _merge_tool_call_deltas(acc: dict[int, dict], deltas) -> None:
    for tc in deltas:
        d = tc.model_dump() if hasattr(tc, "model_dump") else tc
        idx = d.get("index", 0)
        if idx not in acc:
            acc[idx] = {"index": idx, "id": None, "type": None,
                        "function": {"name": None, "arguments": ""}}
        entry = acc[idx]
        if d.get("id"):
            entry["id"] = d["id"]
        if d.get("type"):
            entry["type"] = d["type"]
        fn = d.get("function") or {}
        if fn.get("name"):
            entry["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            entry["function"]["arguments"] += fn["arguments"]


def _safe_log(**kwargs) -> None:
    try:
        log_call(**kwargs)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"LLM admin: log_call failed: {e}")
