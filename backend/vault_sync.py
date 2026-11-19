"""
Vault sync — clone/pull the Obsidian vault from GitHub and index markdown into
the cold_archive_chunks table.

Public functions:
  sync_vault(db)          — clone or pull, then index everything. Returns a result dict.
  sync_paths(db, …)       — pull, then re-read only the files a push changed or removed
                            (the GitHub webhook, ``vault_webhook.py``).
  index_vault(db)         — walk the local clone and upsert chunks.
  search_vault(...)       — case-insensitive keyword search across chunks.
  redact_credentials(...) — strip tokens from text before it is logged or returned.

Every sync records when it ran and how it went on the ``vault`` integration row, which is
what Settings shows as "synced X ago". The folder ``Founder OS/`` is never indexed: the
assistant writes it (``vault_export.py``) from notes it already has.
"""

import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

import models

# ── ENV ──────────────────────────────────────────────────────
VAULT_REPO_URL = os.environ.get(
    "VAULT_REPO_URL",
    "https://github.com/Bandile-tech/bandile-vault.git",
)
VAULT_LOCAL_PATH = os.environ.get("VAULT_LOCAL_PATH", "/tmp/bandile-vault")
VAULT_SYNC_INTERVAL_HOURS = int(os.environ.get("VAULT_SYNC_INTERVAL_HOURS", "6"))
EXPORT_FOLDER = "Founder OS"        # written by the assistant; never read back into the index

# One git operation at a time: a push webhook, the scheduled sync and a manual sync can
# arrive together, and two pulls in one clone corrupt each other.
_git_lock = threading.Lock()


def vault_branch() -> str:
    return os.environ.get("VAULT_BRANCH") or "main"

# ── REDACTION ────────────────────────────────────────────────
# In production VAULT_REPO_URL embeds a GitHub token
# (https://x-access-token:<token>@github.com/...). The URL, git's output and
# exception text can all contain it, so every one of them goes through
# redact_credentials() before it is printed or returned.

_REDACTED = "***"
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@'\"]+@")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)")


def _configured_secrets() -> list[str]:
    """The token in VAULT_REPO_URL: its password, or a username used as the token."""
    try:
        parts = urlsplit(VAULT_REPO_URL)
    except ValueError:
        return []
    secret = parts.password or parts.username
    return [secret] if secret and len(secret) >= 8 else []


def redact_credentials(text) -> str:
    """Remove credentials from any text before it is logged or returned."""
    text = str(text)
    for secret in _configured_secrets():
        text = text.replace(secret, _REDACTED)
    text = _URL_USERINFO.sub(rf"\1{_REDACTED}@", text)
    return _GITHUB_TOKEN.sub(_REDACTED, text)

# ── CHUNK SETTINGS (match main._chunk_text) ──────────────────
_CHUNK_SIZE = 600
_OVERLAP_RATIO = 0.10


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping ~600-char chunks (10% overlap)."""
    words = text.split()
    chunks, buf = [], []
    char_count = 0
    for word in words:
        buf.append(word)
        char_count += len(word) + 1
        if char_count >= _CHUNK_SIZE:
            chunks.append(" ".join(buf))
            overlap = max(1, int(len(buf) * _OVERLAP_RATIO))
            buf = buf[-overlap:]
            char_count = sum(len(w) + 1 for w in buf)
    if buf:
        chunks.append(" ".join(buf))
    return chunks or [text]


# ── SYNC ─────────────────────────────────────────────────────

def _git_refresh() -> tuple[bool, str | None, bool]:
    """Clone the vault, or pull it when the clone exists. ``(ok, error, cloned)``. Never raises."""
    local = Path(VAULT_LOCAL_PATH)
    try:
        with _git_lock:
            cloned = not local.exists()
            if cloned:
                print(f"[vault_sync] Cloning {redact_credentials(VAULT_REPO_URL)}")
                result = subprocess.run(
                    ["git", "clone", VAULT_REPO_URL, str(local)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                )
            else:
                print(f"[vault_sync] Pulling {VAULT_LOCAL_PATH}")
                result = subprocess.run(
                    ["git", "-C", str(local), "pull", "origin", vault_branch()],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                )
        if result.returncode != 0:
            msg = redact_credentials((result.stderr or result.stdout or "unknown git error").strip())
            print(f"[vault_sync] git error: {msg}")
            return False, msg, cloned
        return True, None, cloned
    except Exception as exc:
        msg = redact_credentials(exc)  # TimeoutExpired's text includes the clone URL
        print(f"[vault_sync] Exception during git operation: {msg}")
        return False, msg, False


def record_sync(db: Session, source: str, result: dict) -> None:
    """Note on the ``vault`` integration row when the vault was last synced and how it went.
    Never raises: a sync that worked must not fail on its own bookkeeping."""
    try:
        import integrations
        row = integrations.get_or_create(db, "vault")
        errors = [e for e in (result.get("errors") or []) if e]
        now = datetime.utcnow()
        if errors:
            integrations.set_error(db, row, redact_credentials("; ".join(str(e) for e in errors)))
        else:
            row.status, row.last_error, row.last_sync_at = "connected", None, now
            row.connected_at = row.connected_at or now
        integrations.set_config(row, last_source=source, last_attempt_at=now.isoformat(),
                                last_files=int(result.get("files_processed") or 0),
                                last_removed=int(result.get("files_removed") or 0))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"[vault_sync] could not record the sync: {type(exc).__name__}")


def _embed(db: Session, counts: dict) -> None:
    """Meaning-based search: embed only chunks whose text is new or changed."""
    try:
        import retrieval
        counts["embeddings"] = retrieval.index_vault(db)
    except Exception as exc:  # noqa: BLE001 — embedding problems never fail a sync
        print(f"[vault_sync] embedding skipped: {redact_credentials(exc)}")
        counts["embeddings"] = {"error": redact_credentials(exc)}


def sync_vault(db: Session, source: str = "manual") -> dict:
    """Clone or pull the vault, then index every markdown file. Never raises."""
    ok, error, _cloned = _git_refresh()
    if not ok:
        counts = {"files_processed": 0, "chunks_created": 0, "chunks_updated": 0, "errors": [error]}
        record_sync(db, source, counts)
        return counts
    counts = index_vault(db)
    counts["errors"] = []
    _embed(db, counts)
    record_sync(db, source, counts)
    return counts


def sync_paths(db: Session, changed: list[str], removed: list[str], source: str = "webhook") -> dict:
    """Pull the vault, then re-read only these files: ``changed`` were added or modified,
    ``removed`` were deleted. What the working copy holds after the pull decides (a file
    listed as changed that is gone is treated as removed). Only chunks whose text is new are
    embedded. A vault that was not cloned yet is cloned and indexed in full. Never raises."""
    ok, error, cloned = _git_refresh()
    if not ok:
        counts = {"files_processed": 0, "files_removed": 0, "chunks_created": 0, "chunks_updated": 0,
                  "errors": [error]}
        record_sync(db, source, counts)
        return counts
    if cloned:
        counts = index_vault(db)
        counts["files_removed"] = 0
    else:
        local = Path(VAULT_LOCAL_PATH).resolve()
        counts = {"files_processed": 0, "files_removed": 0, "chunks_created": 0, "chunks_updated": 0}
        now = datetime.utcnow()
        for rel_path in dict.fromkeys(list(changed or []) + list(removed or [])):
            rel_path = str(rel_path or "").replace("\\", "/").lstrip("/")
            if not indexable(rel_path):
                continue
            target = (local / rel_path).resolve()
            if local not in target.parents:               # a path that climbs out of the clone
                continue
            if target.is_file():
                created, replaced = _index_file(db, target, rel_path, now)
                counts["files_processed"] += 1
                counts["chunks_created"] += created
                counts["chunks_updated"] += replaced
            else:
                if _remove_file(db, rel_path):
                    counts["files_removed"] += 1
        db.commit()
        print(f"[vault_sync] Push: {counts['files_processed']} file(s) re-read, "
              f"{counts['files_removed']} removed.")
    counts["errors"] = []
    _embed(db, counts)
    record_sync(db, source, counts)
    return counts


# ── INDEX ────────────────────────────────────────────────────

_SKIP_DIRS = {".git", ".obsidian"}


def indexable(rel_path: str) -> bool:
    """A markdown file outside the folders that are never indexed."""
    parts = [part for part in str(rel_path).replace("\\", "/").split("/") if part]
    if not parts or not parts[-1].lower().endswith(".md") or ".." in parts:
        return False
    return not (set(parts[:-1]) & _SKIP_DIRS) and parts[0] != EXPORT_FOLDER


def _remove_file(db: Session, rel_path: str) -> int:
    return db.query(models.ColdArchiveChunk).filter(models.ColdArchiveChunk.file_path == rel_path).delete()


def _index_file(db: Session, md_path: Path, rel_path: str, now: datetime) -> tuple[int, int]:
    """Replace one file's chunks with its current text. ``(chunks created, chunks replaced)``."""
    try:
        content = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[vault_sync] Cannot read {rel_path}: {exc}")
        return 0, 0
    replaced = _remove_file(db, rel_path)
    chunks = _chunk_text(content)
    for i, chunk in enumerate(chunks):
        db.add(models.ColdArchiveChunk(
            file_path=rel_path,
            file_title=md_path.stem,
            content=chunk,
            chunk_index=i,
            word_count=len(chunk.split()),
            last_synced=now,
            created_at=now,
        ))
    return len(chunks), replaced


def index_vault(db: Session) -> dict:
    """Walk all .md files in VAULT_LOCAL_PATH and upsert chunks. Chunks of a file that is no
    longer in the vault are removed."""
    local = Path(VAULT_LOCAL_PATH)
    if not local.exists():
        return {"files_processed": 0, "chunks_created": 0, "chunks_updated": 0}

    files_processed = 0
    chunks_created = 0
    chunks_updated = 0
    now = datetime.utcnow()
    seen = set()

    for md_path in local.rglob("*.md"):
        rel_path = str(md_path.relative_to(local)).replace("\\", "/")
        if not indexable(rel_path):
            continue
        created, replaced = _index_file(db, md_path, rel_path, now)
        seen.add(rel_path)
        chunks_created += created
        chunks_updated += replaced
        files_processed += 1

    stale = [path for (path,) in db.query(models.ColdArchiveChunk.file_path).distinct().all() if path not in seen]
    for path in stale:
        _remove_file(db, path)

    db.commit()
    print(
        f"[vault_sync] Indexed {files_processed} files, "
        f"{chunks_created} chunks created ({chunks_updated} replaced, {len(stale)} file(s) gone)."
    )
    return {
        "files_processed": files_processed,
        "chunks_created": chunks_created,
        "chunks_updated": chunks_updated,
        "files_removed": len(stale),
    }


# ── SEARCH ───────────────────────────────────────────────────

def search_vault(query: str, db: Session, limit: int = 8) -> list:
    """Meaning-based search when embeddings are available (API key set and chunks
    embedded), otherwise case-insensitive keyword search with title matches first."""
    if not query or len(query) < 2:
        return []

    semantic = search_vault_semantic(query, db, limit)
    if semantic:
        return semantic
    return search_vault_keyword(query, db, limit)


def search_vault_semantic(query: str, db: Session, limit: int = 8) -> list | None:
    """Cosine-ranked chunks, or None when embeddings are unavailable / nothing is indexed."""
    try:
        import retrieval
        hits = retrieval.search(db, query, source_types=["vault"], limit=limit)
    except Exception as exc:  # noqa: BLE001
        print(f"[vault_sync] semantic search failed: {redact_credentials(exc)}")
        return None
    if not hits:
        return None
    ids = [h["source_id"] for h in hits]
    rows = {c.id: c for c in db.query(models.ColdArchiveChunk).filter(models.ColdArchiveChunk.id.in_(ids)).all()}
    out = []
    for h in hits:
        c = rows.get(h["source_id"])
        if c is not None:
            out.append({"file_path": c.file_path, "file_title": c.file_title, "content": c.content,
                        "chunk_index": c.chunk_index, "score": h["score"]})
    return out or None


def search_vault_keyword(query: str, db: Session, limit: int = 8) -> list:
    """Case-insensitive keyword search. Title matches returned first."""

    from database import engine as _engine
    dialect = _engine.dialect.name

    if dialect == "postgresql":
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT file_path, file_title, content, chunk_index,
                   CASE WHEN file_title ILIKE :q THEN 0 ELSE 1 END AS rank
            FROM cold_archive_chunks
            WHERE content ILIKE :qw OR file_title ILIKE :q
            ORDER BY rank ASC, chunk_index ASC
            LIMIT :lim
        """), {"q": f"%{query}%", "qw": f"%{query}%", "lim": limit}).fetchall()
        return [
            {
                "file_path": r[0],
                "file_title": r[1],
                "content": r[2],
                "chunk_index": r[3],
            }
            for r in rows
        ]

    # SQLite — pull all and filter in Python (simpler, dataset is small)
    q_lower = query.lower()
    title_matches = []
    body_matches = []

    chunks = db.query(models.ColdArchiveChunk).all()
    for chunk in chunks:
        title_hit = q_lower in chunk.file_title.lower()
        body_hit = q_lower in chunk.content.lower()
        if title_hit or body_hit:
            entry = {
                "file_path": chunk.file_path,
                "file_title": chunk.file_title,
                "content": chunk.content,
                "chunk_index": chunk.chunk_index,
            }
            if title_hit:
                title_matches.append(entry)
            else:
                body_matches.append(entry)

    results = title_matches + body_matches
    return results[:limit]
