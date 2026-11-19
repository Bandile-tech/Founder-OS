"""
Anthropic (Claude) adapter. Nothing outside this file touches the Anthropic SDK.

Converts the app's OpenAI-shaped messages and tools to the Messages API and back:
  * system messages become the ``system`` blocks; the first one (the stable prompt) carries
    ``cache_control``, and so does the last tool, so the tool definitions and the prompt are
    read from the prompt cache on every call after the first. The render order is tools,
    then system, then messages, and both are byte-stable between calls.
  * an assistant turn with tool calls becomes ``tool_use`` blocks; each tool result becomes
    a ``tool_result`` block in the next user turn.
  * ``thinking`` comes from the route's parameters and from nowhere else. The seeded routes
    switch it off. When a route turns it on, the thinking blocks of a turn are handed back
    unchanged for the rest of that turn (``provider_raw``).

The client is created on first use, so the app boots with no ``ANTHROPIC_API_KEY``.
"""

import json
import os
import re

from llm_adapters import Completion, content_text, json_object_text

NAME = "anthropic"
LABEL = "Claude"
KEY_ENV = "ANTHROPIC_API_KEY"

DEFAULT_MAX_TOKENS = 16000          # the API requires a cap; this leaves room and bills only what is written
JSON_INSTRUCTION = "Respond with one JSON object and nothing else: no prose before or after it, no code fences."
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
         "refusal": "refusal", "pause_turn": "stop"}
_ID = re.compile(r"[^a-zA-Z0-9_-]")

_client = None


def key() -> str | None:
    return os.getenv(KEY_ENV) or None


def configured() -> bool:
    return key() is not None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=key(), timeout=_timeout(), max_retries=1)
    return _client


def reset() -> None:
    global _client
    _client = None


def _timeout() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT_SECONDS") or 90)
    except ValueError:
        return 90.0


def _safe_id(value) -> str:
    return _ID.sub("_", str(value or "call")) or "call"


def convert_tools(tools) -> list:
    out = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        out.append({"name": fn.get("name"), "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if out:
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def convert_messages(messages: list) -> tuple[list, list]:
    """``(system blocks, messages)`` for the Messages API."""
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        text = content_text(m)
        if role == "system":
            if text.strip():
                system.append(text)
        elif role == "user":
            push("user", [{"type": "text", "text": text if text.strip() else "(no text)"}])
        elif role == "assistant":
            raw = m.get("provider_raw")
            if isinstance(raw, dict) and raw.get("provider") == NAME and raw.get("content"):
                blocks = list(raw["content"])
            else:
                blocks = [{"type": "text", "text": text}] if text.strip() else []
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": _safe_id(call.get("id")), "name": fn.get("name"),
                                   "input": args if isinstance(args, dict) else {}})
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": _safe_id(m.get("tool_call_id")),
                           "content": text if text.strip() else "(empty)"}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(earlier conversation not shown)"}]})
    blocks = [{"type": "text", "text": part} for part in system]
    if blocks:
        blocks[0] = {**blocks[0], "cache_control": {"type": "ephemeral"}}
    return blocks, out


def _kwargs(model: str, messages: list, tools, params: dict | None, json_mode: bool, max_output) -> dict:
    params = params or {}
    system, converted = convert_messages(messages)
    if json_mode:
        system.append({"type": "text", "text": JSON_INSTRUCTION})
    kwargs = {"model": model, "messages": converted,
              "max_tokens": int(max_output or params.get("max_tokens") or DEFAULT_MAX_TOKENS)}
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = convert_tools(tools)
    if params.get("thinking"):
        kwargs["thinking"] = params["thinking"]
    if params.get("effort"):
        kwargs["output_config"] = {"effort": params["effort"]}
    return kwargs


def _read_usage(usage, into: dict) -> None:
    if usage is None:
        return
    read = int(getattr(usage, "cache_read_input_tokens", None) or 0)
    write = int(getattr(usage, "cache_creation_input_tokens", None) or 0)
    fresh = getattr(usage, "input_tokens", None)
    if fresh is not None:
        # Claude reports the three separately; the app counts input as everything read.
        into["input"], into["cached"], into["cache_write"] = int(fresh) + read + write, read, write
    produced = getattr(usage, "output_tokens", None)
    if produced is not None:
        into["output"] = int(produced)


def _block_dict(block) -> dict | None:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text or ""}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input or {})}
    if kind == "thinking":
        return {"type": "thinking", "thinking": getattr(block, "thinking", "") or "",
                "signature": getattr(block, "signature", "") or ""}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": getattr(block, "data", "")}
    return None


def _raw(blocks: list) -> dict | None:
    """The turn's own blocks, kept only when there is thinking to hand back."""
    if any(b["type"] in ("thinking", "redacted_thinking") for b in blocks):
        return {"provider": NAME, "content": blocks}
    return None


def complete(model: str, messages: list, *, tools=None, params: dict | None = None,
             json_mode: bool = False, max_output: int | None = None) -> Completion:
    response = client().messages.create(**_kwargs(model, messages, tools, params, json_mode, max_output))
    usage = {"input": 0, "cached": 0, "cache_write": 0, "output": 0}
    _read_usage(getattr(response, "usage", None), usage)
    out = Completion(input_tokens=usage["input"], cached_input_tokens=usage["cached"],
                     cache_write_tokens=usage["cache_write"], output_tokens=usage["output"],
                     provider=NAME, model=model, stop=_STOP.get(response.stop_reason, "stop"))
    blocks = [b for b in (_block_dict(block) for block in response.content or []) if b is not None]
    out.text = "".join(b["text"] for b in blocks if b["type"] == "text")
    out.tool_calls = [{"id": b["id"], "name": b["name"], "arguments": json.dumps(b["input"])}
                      for b in blocks if b["type"] == "tool_use"]
    if json_mode:
        out.text = json_object_text(out.text)
    out.provider_raw = _raw(blocks)
    return out


def stream(model: str, messages: list, tools=None, params: dict | None = None, max_output: int | None = None):
    """Yield the normalized deltas described in ``llm_adapters``."""
    kwargs = _kwargs(model, messages, tools, params, False, max_output)
    usage = {"input": 0, "cached": 0, "cache_write": 0, "output": 0}
    blocks: dict[int, dict] = {}        # content block index → the block as it builds up
    ordinal: dict[int, int] = {}        # content block index → position among this turn's tool calls
    stop = None
    for event in client().messages.create(stream=True, **kwargs):
        kind = event.type
        if kind == "message_start":
            _read_usage(getattr(event.message, "usage", None), usage)
        elif kind == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                ordinal[event.index] = len(ordinal)
                blocks[event.index] = {"type": "tool_use", "id": block.id, "name": block.name, "json": ""}
                yield {"tool_calls": [{"index": ordinal[event.index], "id": block.id, "name": block.name,
                                       "arguments": None}]}
            elif block.type == "text":
                blocks[event.index] = {"type": "text", "text": ""}
            else:
                shaped = _block_dict(block)
                if shaped is not None:
                    blocks[event.index] = shaped
        elif kind == "content_block_delta":
            delta, block = event.delta, blocks.get(event.index)
            if block is None:
                continue
            if delta.type == "text_delta":
                block["text"] += delta.text
                yield {"content": delta.text}
            elif delta.type == "input_json_delta":
                block["json"] += delta.partial_json
                yield {"tool_calls": [{"index": ordinal[event.index], "id": None, "name": None,
                                       "arguments": delta.partial_json}]}
            elif delta.type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.thinking
            elif delta.type == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.signature
        elif kind == "message_delta":
            stop = getattr(event.delta, "stop_reason", None) or stop
            _read_usage(getattr(event, "usage", None), usage)
    yield {"finish_reason": _STOP.get(stop, "stop")}
    content = []
    for index in sorted(blocks):
        block = blocks[index]
        if block["type"] == "tool_use":
            try:
                parsed = json.loads(block["json"] or "{}")
            except ValueError:
                parsed = {}
            content.append({"type": "tool_use", "id": block["id"], "name": block["name"],
                            "input": parsed if isinstance(parsed, dict) else {}})
        elif block["type"] != "text" or block["text"]:
            content.append(block)
    raw = _raw(content)
    if raw is not None:
        yield {"provider_raw": raw}
    yield {"usage": usage}
