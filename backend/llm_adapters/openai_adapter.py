"""
OpenAI adapter: chat (streamed and not), embeddings, transcription, speech and the
real-time voice client secret. Nothing outside this file touches the OpenAI SDK or API.

The client is created on first use, so the app boots with no ``OPENAI_API_KEY``.
"""

import os

import httpx

from llm_adapters import Completion

NAME = "openai"
LABEL = "OpenAI"
KEY_ENV = "OPENAI_API_KEY"
REALTIME_BASE = os.getenv("OPENAI_REALTIME_BASE", "https://api.openai.com/v1/realtime")

# Route parameters this provider understands. Anything else in a route's params is ignored
# here; nothing is sent that the route does not list.
_PASS_THROUGH = ("temperature", "top_p", "reasoning_effort", "verbosity", "seed", "service_tier",
                 "prompt_cache_key")
_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")

_client = None


def key() -> str | None:
    return os.getenv(KEY_ENV) or None


def configured() -> bool:
    return key() is not None


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=key(), timeout=_timeout(), max_retries=1)
    return _client


def reset() -> None:
    global _client
    _client = None


def _timeout() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT_SECONDS") or 90)
    except ValueError:
        return 90.0


def _messages(messages: list) -> list:
    """The messages with only the keys the chat API accepts (``provider_raw`` and anything
    else internal is dropped)."""
    return [{k: m[k] for k in _MESSAGE_KEYS if k in m} for m in messages]


def _kwargs(model: str, messages: list, tools, params: dict | None, json_mode: bool, max_output) -> dict:
    params = params or {}
    kwargs = {"model": model, "messages": _messages(messages)}
    if tools:
        kwargs["tools"] = tools
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    cap = max_output if max_output is not None else params.get("max_tokens")
    if cap is not None:
        # The GPT-5 family rejects ``max_tokens``; the cap is always sent under this name.
        kwargs["max_completion_tokens"] = int(cap)
    for name in _PASS_THROUGH:
        if params.get(name) is not None:
            kwargs[name] = params[name]
    return kwargs


def _usage(usage) -> dict:
    if usage is None:
        return {"input": 0, "cached": 0, "cache_write": 0, "output": 0}
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    return {"input": int(getattr(usage, "prompt_tokens", None) or 0), "cached": int(cached), "cache_write": 0,
            "output": int(getattr(usage, "completion_tokens", None) or 0)}


def complete(model: str, messages: list, *, tools=None, params: dict | None = None,
             json_mode: bool = False, max_output: int | None = None) -> Completion:
    response = client().chat.completions.create(**_kwargs(model, messages, tools, params, json_mode, max_output))
    usage = _usage(getattr(response, "usage", None))
    out = Completion(input_tokens=usage["input"], cached_input_tokens=usage["cached"],
                     output_tokens=usage["output"], provider=NAME, model=model)
    if not response.choices:
        return out
    choice = response.choices[0]
    message = choice.message
    out.text = message.content or ""
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        if fn is not None:
            out.tool_calls.append({"id": call.id, "name": fn.name, "arguments": fn.arguments or "{}"})
    if getattr(message, "refusal", None) and not out.text:
        out.stop = "refusal"
    elif out.tool_calls:
        out.stop = "tool_calls"
    elif getattr(choice, "finish_reason", None) == "length":
        out.stop = "length"
    elif getattr(choice, "finish_reason", None) == "content_filter":
        out.stop = "refusal"
    return out


def stream(model: str, messages: list, tools=None, params: dict | None = None, max_output: int | None = None):
    """Yield the normalized deltas described in ``llm_adapters``. The request puts the
    system prompt and the tool definitions first and unchanged, which is all OpenAI's
    automatic prompt caching needs."""
    kwargs = _kwargs(model, messages, tools, params, False, max_output)
    events = client().chat.completions.create(stream=True, stream_options={"include_usage": True}, **kwargs)
    usage = None
    refused = False
    said = False
    for chunk in events:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if getattr(delta, "content", None):
            said = True
            yield {"content": delta.content}
        if getattr(delta, "refusal", None):
            refused = True
        if getattr(delta, "tool_calls", None):
            said = True
            yield {"tool_calls": [{
                "index": tc.index, "id": tc.id,
                "name": tc.function.name if tc.function else None,
                "arguments": tc.function.arguments if tc.function else None,
            } for tc in delta.tool_calls]}
        if choice.finish_reason:
            reason = choice.finish_reason
            if reason == "content_filter" or (refused and not said):
                reason = "refusal"
            yield {"finish_reason": reason}
    yield {"usage": _usage(usage)}


def embed(model: str, texts: list[str]) -> tuple[list[list[float]], dict]:
    response = client().embeddings.create(model=model, input=texts)
    ordered = sorted(response.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered], _usage(getattr(response, "usage", None))


def transcribe(model: str, file_bytes: bytes, filename: str) -> str:
    result = client().audio.transcriptions.create(model=model, file=(filename, file_bytes))
    return (result.text or "").strip()


def speech(model: str, voice: str, text: str) -> bytes:
    return client().audio.speech.create(model=model, voice=voice, input=text).content


def realtime_client_secret(body: dict, timeout: float = 20.0):
    """Mint the short-lived secret the browser uses for WebRTC voice. Returns the HTTP
    response; the caller decides what a refusal means. Raises ``httpx.HTTPError`` when
    OpenAI cannot be reached."""
    return httpx.post(f"{REALTIME_BASE}/client_secrets",
                      headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
                      json=body, timeout=timeout)
