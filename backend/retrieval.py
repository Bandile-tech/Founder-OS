"""
Meaning-based retrieval shared by the vault, notes and long-term memory.

* One ``embeddings`` table. Vectors are JSON in a TEXT column; cosine similarity is
  computed in Python. The corpus is small (~715 vault chunks), so no pgvector.
* Keyed by content hash: re-indexing text that has not changed never re-embeds it.
* With no API key every ``search`` returns None and callers fall back to keyword search.
"""

import hashlib
import json
import math
from datetime import datetime

from sqlalchemy.orm import Session

import models
import llm

EMBED_BATCH = 64
MAX_CHARS = 8000   # keep each embedded text comfortably inside the model's input limit


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def available() -> bool:
    return llm.is_configured()


# ── Indexing ─────────────────────────────────────────────────

def index_items(db: Session, source_type: str, items: list[tuple[int, int, str]],
                *, prune: bool = True) -> dict:
    """Embed ``items`` = [(source_id, chunk_index, text)] for one source type.

    Only texts whose hash is not already stored are sent to the embedding model; an
    unchanged text whose id moved (vault re-index) just has its pointer updated. When
    ``prune`` is set, stored rows whose hash is no longer present are removed.
    Returns counts. Does nothing (and embeds nothing) when no key is configured."""
    result = {"embedded": 0, "reused": 0, "pruned": 0, "skipped": not available()}
    if not available():
        return result

    model = llm.embedding_model()
    wanted: dict[str, tuple[int, int, str]] = {}
    for source_id, chunk_index, text in items:
        text = (text or "").strip()[:MAX_CHARS]
        if text:
            wanted[content_hash(text)] = (source_id, chunk_index, text)

    existing = {
        row.content_hash: row
        for row in db.query(models.Embedding).filter(models.Embedding.source_type == source_type).all()
    }

    to_embed = []
    for h, (source_id, chunk_index, text) in wanted.items():
        row = existing.get(h)
        if row is not None and row.model == model:
            if row.source_id != source_id or row.chunk_index != chunk_index:
                row.source_id, row.chunk_index = source_id, chunk_index
                row.updated_at = datetime.utcnow()
            result["reused"] += 1
        else:
            to_embed.append((h, source_id, chunk_index, text))

    for start in range(0, len(to_embed), EMBED_BATCH):
        batch = to_embed[start:start + EMBED_BATCH]
        vectors = llm.embed_texts([t for _, _, _, t in batch], db=db, purpose=f"embed_{source_type}")
        for (h, source_id, chunk_index, _), vec in zip(batch, vectors):
            row = existing.get(h)
            if row is None:
                row = models.Embedding(source_type=source_type, content_hash=h)
                db.add(row)
            row.source_id, row.chunk_index = source_id, chunk_index
            row.model, row.vector_json = model, json.dumps(vec)
            row.updated_at = datetime.utcnow()
            result["embedded"] += 1

    if prune:
        for h, row in existing.items():
            if h not in wanted:
                db.delete(row)
                result["pruned"] += 1

    db.flush()   # the caller owns the transaction (parser batch, endpoint, sync)
    return result


def index_one(db: Session, source_type: str, source_id: int, text: str) -> dict:
    """Embed a single record (a note or a memory). Other rows of the type are untouched."""
    if not available():
        return {"embedded": 0, "reused": 0, "pruned": 0, "skipped": True}
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return {"embedded": 0, "reused": 0, "pruned": 0, "skipped": False}
    h = content_hash(text)
    model = llm.embedding_model()
    rows = db.query(models.Embedding).filter(
        models.Embedding.source_type == source_type, models.Embedding.source_id == source_id,
    ).all()
    for row in rows:
        if row.content_hash == h and row.model == model:
            return {"embedded": 0, "reused": 1, "pruned": 0, "skipped": False}
    for row in rows:
        db.delete(row)
    vec = llm.embed_texts([text], db=db, purpose=f"embed_{source_type}")[0]
    db.add(models.Embedding(source_type=source_type, source_id=source_id, chunk_index=0,
                            content_hash=h, model=model, vector_json=json.dumps(vec),
                            updated_at=datetime.utcnow()))
    db.flush()
    return {"embedded": 1, "reused": 0, "pruned": len(rows), "skipped": False}


def forget(db: Session, source_type: str, source_id: int) -> None:
    db.query(models.Embedding).filter(
        models.Embedding.source_type == source_type, models.Embedding.source_id == source_id,
    ).delete()


def index_vault(db: Session) -> dict:
    """Embed every vault chunk that is new or changed since the last sync. Commits."""
    chunks = db.query(models.ColdArchiveChunk).all()
    result = index_items(db, "vault", [(c.id, c.chunk_index, c.content) for c in chunks])
    db.commit()
    return result


def index_notes(db: Session) -> dict:
    notes = db.query(models.Note).all()
    return index_items(db, "note", [(n.id, 0, f"{n.title}\n{n.body}") for n in notes])


def index_memories(db: Session) -> dict:
    from memory_service import all_memories
    return index_items(db, "memory", [(m.id, 0, f"{m.context_data} {m.tags or ''}") for m in all_memories(db)])


def index_crm(db: Session) -> dict:
    """Backfill: communications as ``crm`` and project notes as ``crm_project``."""
    comms = db.query(models.Communication).all()
    result = index_items(db, "crm", [(c.id, 0, c.summary) for c in comms])
    projects = db.query(models.Project).filter(models.Project.notes.isnot(None)).all()
    more = index_items(db, "crm_project", [(p.id, 0, f"{p.title}\n{p.notes}") for p in projects])
    return {k: result[k] + more[k] for k in ("embedded", "reused", "pruned")}


# ── Search ───────────────────────────────────────────────────

def search(db: Session, query: str, source_types: list[str] | None = None,
           limit: int = 8, min_score: float = 0.2) -> list[dict] | None:
    """Rank stored vectors by cosine similarity to ``query``.

    Returns None when embeddings are unavailable (no key) so callers can fall back
    to keyword search. Each hit: {source_type, source_id, chunk_index, score}."""
    if not available() or not (query or "").strip():
        return None
    try:
        qvec = llm.embed_texts([query.strip()[:MAX_CHARS]], db=db, purpose="embed_query")[0]
    except Exception as exc:  # noqa: BLE001 — degrade to keyword search
        print(f"[retrieval] query embedding failed: {exc}")
        return None
    q = db.query(models.Embedding)
    if source_types:
        q = q.filter(models.Embedding.source_type.in_(source_types))
    hits = []
    for row in q.all():
        try:
            vec = json.loads(row.vector_json)
        except (ValueError, TypeError):
            continue
        score = _cosine(qvec, vec)
        if score >= min_score:
            hits.append({"source_type": row.source_type, "source_id": row.source_id,
                         "chunk_index": row.chunk_index, "score": round(score, 4)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]
