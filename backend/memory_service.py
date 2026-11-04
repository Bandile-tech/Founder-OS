"""
Long-term memory on top of the ``ai_memory`` table.

* ``remember`` stores a durable fact, decision or preference
  (context_type='memory', text in ``context_data``).
* ``recall`` returns the memories most relevant to a query. Stage 1 ranks by keyword
  overlap; Stage 3 routes through the shared embeddings retrieval when a key exists.
* ``get_recent_memories`` is the legacy accessor used by /memory/recent.
"""

import re
from datetime import datetime

from sqlalchemy.orm import Session

from models import AIMemory

MEMORY_CONTEXT_TYPE = "memory"
MEMORY_KINDS = ("fact", "decision", "preference")
_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "have", "what", "when", "about",
    "your", "you", "are", "was", "were", "will", "into", "than", "then", "them", "they",
    "should", "would", "could", "there", "their", "which", "where", "while", "been", "being",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def get_recent_memories(db: Session, limit: int = 5, context_type: str | None = None):
    query = db.query(AIMemory)
    if context_type:
        query = query.filter(AIMemory.context_type == context_type)
    return query.order_by(AIMemory.created_at.desc()).limit(limit).all()


def remember(db: Session, text: str, kind: str = "fact", tags: list[str] | None = None,
             commit: bool = True) -> AIMemory:
    text = (text or "").strip()
    if not text:
        raise ValueError("memory text is empty")
    if kind not in MEMORY_KINDS:
        kind = "fact"
    row = AIMemory(
        context_type=MEMORY_CONTEXT_TYPE,
        context_data=text,
        response=None,
        kind=kind,
        tags=",".join(t.strip() for t in tags if t and t.strip()) if tags else None,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def all_memories(db: Session) -> list[AIMemory]:
    return (
        db.query(AIMemory)
        .filter(AIMemory.context_type == MEMORY_CONTEXT_TYPE)
        .order_by(AIMemory.created_at.desc())
        .all()
    )


def recall_keyword(db: Session, query: str, limit: int = 5) -> list[AIMemory]:
    """Keyword-overlap ranking. Ties broken by recency."""
    q = _tokens(query)
    rows = all_memories(db)
    if not q:
        return rows[:limit]
    scored = []
    for r in rows:
        hay = _tokens(f"{r.context_data} {r.tags or ''}")
        overlap = len(q & hay)
        if overlap:
            scored.append((overlap, r.created_at or datetime.min, r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def recall(db: Session, query: str, limit: int = 5) -> list[AIMemory]:
    """Relevant memories for a query. Uses embeddings when the retrieval module can,
    keyword overlap otherwise."""
    try:
        import retrieval  # Stage 3 — optional until then
        hits = retrieval.search(db, query, source_types=["memory"], limit=limit)
        if hits is not None:
            ids = [h["source_id"] for h in hits]
            if ids:
                rows = db.query(AIMemory).filter(AIMemory.id.in_(ids)).all()
                by_id = {r.id: r for r in rows}
                return [by_id[i] for i in ids if i in by_id]
            return []
    except ImportError:
        pass
    return recall_keyword(db, query, limit)


def format_memory(row: AIMemory) -> str:
    kind = row.kind or "fact"
    when = row.created_at.date().isoformat() if row.created_at else ""
    return f"({kind}, {when}) {row.context_data}"
