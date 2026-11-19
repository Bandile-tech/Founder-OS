"""
The daily export to the vault (Phase 7, Stage 3).

Once a day the notes the assistant created or changed are written into one folder of the
Obsidian vault, ``Founder OS/``, through the GitHub API. Only the assistant writes there
and Obsidian only reads it, so nothing conflicts: a file edited by hand in that folder is
overwritten on the next export.

  export(db)   write what changed since the last export; returns counts. Never raises.

Needs ``VAULT_REPO`` ("owner/name") and ``VAULT_WRITE_TOKEN`` (a fine-grained token with
Contents: read and write on that one repository). With either unset the export is skipped.
``vault_exports`` remembers, per note, the path, the content hash and the file's blob sha
from the last export, so an unchanged note costs no request and a renamed or deleted note
has its old file removed. The file's text depends only on the note, so the same note
always produces the same file.
"""

import base64
import hashlib
import os
import re
from datetime import datetime
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

import integrations
import models
from vault_sync import EXPORT_FOLDER, vault_branch

API = "https://api.github.com"
TIMEOUT = 20
MAX_FILES_PER_RUN = 40
KIND_FOLDERS = {"book": "Books", "bible": "Bible", "journal": "Journal", "inbox": "Inbox", "general": "Notes"}
FOOTER = "Written by Founder OS. Changes made here are overwritten: tell Founder OS instead."


def repo() -> str:
    return (os.getenv("VAULT_REPO") or "").strip().strip("/")


def token() -> str:
    return os.getenv("VAULT_WRITE_TOKEN") or ""


def configured() -> bool:
    return bool(repo() and token())


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:80] or "note"


def note_path(note: models.Note, taken: set | None = None) -> str:
    folder = KIND_FOLDERS.get(note.kind or "general", "Notes")
    path = f"{EXPORT_FOLDER}/{folder}/{_slug(note.title)}.md"
    if taken is not None and path in taken:
        path = f"{EXPORT_FOLDER}/{folder}/{_slug(note.title)}-{note.id}.md"
    return path


def render(note: models.Note) -> str:
    updated = note.updated_at.date().isoformat() if note.updated_at else ""
    kind = (note.kind or "general").capitalize()
    return (f"# {note.title}\n\n{(note.body or '').rstrip()}\n\n---\n"
            f"{kind} note" + (f", last changed {updated}" if updated else "") + f". {FOOTER}\n")


def assistant_note_ids(db: Session) -> set[int]:
    """Notes the assistant, the parser or an agent created or changed (anything but a tap
    on a screen)."""
    rows = db.query(models.ChangeLog.record_id).filter(
        models.ChangeLog.table_name == "notes", models.ChangeLog.source != "ui",
        models.ChangeLog.undone_at.is_(None)).distinct().all()
    return {record_id for (record_id,) in rows if record_id is not None}


# ── GitHub ───────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "founder-os"}


def _url(path: str) -> str:
    return f"{API}/repos/{repo()}/contents/{quote(path)}"


def _problem(resp) -> str:
    try:
        detail = resp.json().get("message")
    except Exception:  # noqa: BLE001
        detail = None
    return f"GitHub answered {resp.status_code}" + (f": {detail}" if detail else "")


def _current_sha(path: str) -> str | None:
    resp = httpx.get(_url(path), headers=_headers(), params={"ref": vault_branch()}, timeout=TIMEOUT)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("sha") if isinstance(data, dict) else None
    return None


def _put(path: str, text: str, sha: str | None, title: str) -> str:
    """Create or update one file. Returns the new blob sha. Raises RuntimeError with a
    message that carries no secret."""
    body = {"message": f"Founder OS: {title}"[:120], "branch": vault_branch(),
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    resp = httpx.put(_url(path), headers=_headers(), json=body, timeout=TIMEOUT)
    if resp.status_code in (409, 422):
        # The file is there with a sha other than the one on record (it was edited by hand,
        # or this is the first export to a database that has no record of it). The folder
        # is the assistant's: take the current sha and write over it.
        current = _current_sha(path)
        if current and current != sha:
            body["sha"] = current
            resp = httpx.put(_url(path), headers=_headers(), json=body, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise RuntimeError(_problem(resp))
    return ((resp.json() or {}).get("content") or {}).get("sha") or ""


def _delete(path: str, sha: str | None, title: str) -> None:
    sha = sha or _current_sha(path)
    if not sha:
        return                                     # already gone
    resp = httpx.request("DELETE", _url(path), headers=_headers(), timeout=TIMEOUT,
                         json={"message": f"Founder OS: remove {title}"[:120], "branch": vault_branch(), "sha": sha})
    if resp.status_code == 409:
        current = _current_sha(path)
        if current and current != sha:
            resp = httpx.request("DELETE", _url(path), headers=_headers(), timeout=TIMEOUT,
                                 json={"message": f"Founder OS: remove {title}"[:120], "branch": vault_branch(),
                                       "sha": current})
    if resp.status_code not in (200, 404):
        raise RuntimeError(_problem(resp))


# ── The export ───────────────────────────────────────────────

def export(db: Session, limit: int = MAX_FILES_PER_RUN) -> dict:
    out = {"exported": 0, "removed": 0, "unchanged": 0, "errors": [], "skipped": None}
    if not configured():
        out["skipped"] = "VAULT_REPO and VAULT_WRITE_TOKEN are not set"
        return out
    wanted = assistant_note_ids(db)
    notes = {n.id: n for n in db.query(models.Note).filter(models.Note.id.in_(wanted)).all()} if wanted else {}
    records = {r.note_id: r for r in db.query(models.VaultExport).all()}
    taken = {r.path for nid, r in records.items() if nid not in notes}
    budget = limit

    def fail(what: str, exc: Exception) -> None:
        out["errors"].append(integrations.redact(f"{what}: {exc}", db)[:200])

    for note_id in sorted(notes):
        note, record = notes[note_id], records.get(note_id)
        path = record.path if record and record.path == note_path(note) else note_path(note, taken)
        taken.add(path)
        text = render(note)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if record and record.path == path and record.content_hash == digest:
            out["unchanged"] += 1
            continue
        if budget <= 0:
            continue                                   # the rest go out with the next run
        budget -= 1
        try:
            if record and record.path != path:         # the note was renamed: its old file goes
                _delete(record.path, record.sha, note.title)
                record.sha = None
            sha = _put(path, text, record.sha if record and record.path == path else None, note.title)
        except Exception as exc:  # noqa: BLE001 — one file must not stop the rest
            fail(path, exc)
            continue
        if record is None:
            record = models.VaultExport(note_id=note_id)
            db.add(record)
        record.path, record.content_hash, record.sha, record.exported_at = path, digest, sha, datetime.utcnow()
        db.commit()
        out["exported"] += 1

    for note_id, record in records.items():            # the note is gone, or no longer the assistant's
        if note_id in notes or budget <= 0:
            continue
        budget -= 1
        try:
            _delete(record.path, record.sha, record.path.rsplit("/", 1)[-1])
        except Exception as exc:  # noqa: BLE001
            fail(record.path, exc)
            continue
        db.delete(record)
        db.commit()
        out["removed"] += 1
    return out
