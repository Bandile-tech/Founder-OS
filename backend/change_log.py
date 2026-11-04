"""
Change batches for assistant turns (Stage 1 scaffold; Stage 2 adds the change_log
table, recording helpers and undo).

One assistant turn is one batch. ``begin_batch`` opens it, every write made during the
turn is appended via ``add_change``, and ``current_batch_changes`` is what the final
SSE event and the saved assistant message carry.
"""

import contextvars
import uuid

_batch_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("change_batch_id", default=None)
_batch_changes: contextvars.ContextVar[list | None] = contextvars.ContextVar("change_batch_changes", default=None)


def begin_batch(batch_id: str | None = None) -> str:
    bid = batch_id or uuid.uuid4().hex
    _batch_id.set(bid)
    _batch_changes.set([])
    return bid


def end_batch() -> list:
    changes = list(_batch_changes.get() or [])
    _batch_id.set(None)
    _batch_changes.set(None)
    return changes


def current_batch_id() -> str | None:
    return _batch_id.get()


def current_batch_changes() -> list:
    return list(_batch_changes.get() or [])


def add_change(entry: dict) -> None:
    changes = _batch_changes.get()
    if changes is None:
        return
    changes.append(entry)
