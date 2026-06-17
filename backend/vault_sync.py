"""
Vault sync — clone/pull the Obsidian vault from GitHub and index markdown into
the cold_archive_chunks table.

Three public functions:
  sync_vault(db)    — clone or pull, then index. Returns a result dict.
  index_vault(db)   — walk the local clone and upsert chunks.
  search_vault(...) — case-insensitive keyword search across chunks.
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

import models

# ── ENV ──────────────────────────────────────────────────────
VAULT_REPO_URL = os.environ.get(
    "VAULT_REPO_URL",
    "https://github.com/Bandile-tech/bandile-vault.git",
)
VAULT_LOCAL_PATH = os.environ.get("VAULT_LOCAL_PATH", "/tmp/bandile-vault")
VAULT_SYNC_INTERVAL_HOURS = int(os.environ.get("VAULT_SYNC_INTERVAL_HOURS", "6"))

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

def sync_vault(db: Session) -> dict:
    """Clone or pull the vault, then index markdown. Never raises."""
    local = Path(VAULT_LOCAL_PATH)
    errors = []

    try:
        if not local.exists():
            print(f"[vault_sync] Cloning {VAULT_REPO_URL}")
            result = subprocess.run(
                ["git", "clone", VAULT_REPO_URL, str(local)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
        else:
            print(f"[vault_sync] Pulling {VAULT_LOCAL_PATH}")
            result = subprocess.run(
                ["git", "-C", str(local), "pull", "origin", "main"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "unknown git error").strip()
            print(f"[vault_sync] git error: {msg}")
            errors.append(msg)
            return {
                "files_processed": 0,
                "chunks_created": 0,
                "chunks_updated": 0,
                "errors": errors,
            }

    except Exception as exc:
        msg = str(exc)
        print(f"[vault_sync] Exception during git operation: {msg}")
        return {
            "files_processed": 0,
            "chunks_created": 0,
            "chunks_updated": 0,
            "errors": [msg],
        }

    counts = index_vault(db)
    counts["errors"] = errors
    return counts


# ── INDEX ────────────────────────────────────────────────────

_SKIP_DIRS = {".git", ".obsidian"}


def index_vault(db: Session) -> dict:
    """Walk all .md files in VAULT_LOCAL_PATH and upsert chunks."""
    local = Path(VAULT_LOCAL_PATH)
    if not local.exists():
        return {"files_processed": 0, "chunks_created": 0, "chunks_updated": 0}

    files_processed = 0
    chunks_created = 0
    chunks_updated = 0
    now = datetime.utcnow()

    for md_path in local.rglob("*.md"):
        # Skip files inside .git or .obsidian
        parts = set(md_path.relative_to(local).parts[:-1])
        if parts & _SKIP_DIRS:
            continue

        rel_path = str(md_path.relative_to(local)).replace("\\", "/")
        file_title = md_path.stem

        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"[vault_sync] Cannot read {rel_path}: {exc}")
            continue

        # Delete existing chunks for this file
        existing = db.query(models.ColdArchiveChunk).filter(
            models.ColdArchiveChunk.file_path == rel_path
        ).count()
        if existing:
            db.query(models.ColdArchiveChunk).filter(
                models.ColdArchiveChunk.file_path == rel_path
            ).delete()
            chunks_updated += existing

        chunks = _chunk_text(content)
        for i, chunk in enumerate(chunks):
            db.add(models.ColdArchiveChunk(
                file_path=rel_path,
                file_title=file_title,
                content=chunk,
                chunk_index=i,
                word_count=len(chunk.split()),
                last_synced=now,
                created_at=now,
            ))
            chunks_created += 1

        files_processed += 1

    db.commit()
    print(
        f"[vault_sync] Indexed {files_processed} files, "
        f"{chunks_created} chunks created ({chunks_updated} replaced)."
    )
    return {
        "files_processed": files_processed,
        "chunks_created": chunks_created,
        "chunks_updated": chunks_updated,
    }


# ── SEARCH ───────────────────────────────────────────────────

def search_vault(query: str, db: Session, limit: int = 8) -> list:
    """Case-insensitive keyword search. Title matches returned first."""
    if not query or len(query) < 2:
        return []

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
