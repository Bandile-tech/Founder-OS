"""
Provider adapters. The only modules that import a provider SDK or call a provider's API.

Everything else talks to ``llm`` (the gateway), and the gateway talks to an adapter. Both
adapters speak one internal shape, so nothing above them knows which provider answered.

Messages in: the OpenAI chat shape the app already stores and builds
    {"role": "system" | "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "type": "function", "function": {"name", "arguments"}}],   (assistant)
     "tool_call_id": str}                                                             (tool)
  An assistant message may also carry ``provider_raw``: the provider's own content for that
  turn, kept in memory for the length of one turn so a provider that needs its own blocks
  back (Claude's thinking blocks) gets them. It is never stored and never sent to the
  other provider.

Tools in: the OpenAI function-tool shape (``orchestrator_tools.TOOL_SCHEMAS``).

``complete`` returns a ``Completion``. ``stream`` yields dicts:
    {"content": "<text>"}                                    text delta
    {"tool_calls": [{"index", "id", "name", "arguments"}]}   tool-call fragment(s)
    {"finish_reason": "stop" | "tool_calls" | "length" | "refusal"}
    {"provider_raw": {...}}                                  optional, see above
    {"usage": {"input", "cached", "cache_write", "output"}}  last; consumed by the gateway
The first three are exactly what the orchestrator loop has always read, so the events the
frontend receives do not change.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Completion:
    text: str = ""
    tool_calls: list = field(default_factory=list)    # [{"id", "name", "arguments": "<json>"}]
    input_tokens: int = 0                             # everything read, cached or not
    cached_input_tokens: int = 0                      # read from the provider's prompt cache
    cache_write_tokens: int = 0                       # written to it (Claude bills these separately)
    output_tokens: int = 0
    stop: str = "stop"                                # stop | tool_calls | length | refusal
    provider: str = ""
    model: str = ""
    provider_raw: Any = None


def content_text(message: dict) -> str:
    """A message's text, whatever shape its content has."""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"))
    return str(content)


def json_object_text(text: str) -> str:
    """The JSON object inside ``text``: code fences and stray prose around it removed.
    Returns the text unchanged when it holds no braces (the caller's parse then fails as it
    would have)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text
