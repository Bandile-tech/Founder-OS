"""
The live vault (Phase 7, Stage 3).

  POST /vault/webhook   GitHub tells Founder OS the vault repository was pushed to.

Obsidian commits and pushes on its own every few minutes; GitHub calls this endpoint for
each push; only the files the push added, changed or removed are read again. So the vault
the assistant searches is minutes old, not "whenever the app last restarted".

The endpoint takes no session. It checks GitHub's own signature instead: the header
``X-Hub-Signature-256`` must be the HMAC-SHA256 of the raw body under
``GITHUB_WEBHOOK_SECRET``. With the secret unset, or shorter than 16 characters, it answers
503 and reads nothing. The manual sync (``POST /vault/sync``) stays as the fallback.

GitHub waits ten seconds for an answer, and a pull can take longer, so the work runs on a
thread and the endpoint answers 202 at once. A push that arrives while the Render instance
is asleep wakes it; if GitHub gives up on that first delivery, the startup sync that
follows reads the whole vault anyway.
"""

import hashlib
import hmac
import json
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import vault_sync

router = APIRouter()

MIN_SECRET_LENGTH = 16
MAX_COMMITS_LISTED = 20           # GitHub lists at most this many commits in a push payload


def webhook_secret() -> str:
    return os.getenv("GITHUB_WEBHOOK_SECRET") or ""


def signature_for(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def plan_push(payload: dict) -> dict:
    """What a push means for the index: ``{"changed", "removed"}``, ``{"full": True}`` when
    the payload cannot list everything, or ``{"ignored": reason}``."""
    repo = (os.getenv("VAULT_REPO") or "").strip().lower()
    pushed_to = str((payload.get("repository") or {}).get("full_name") or "").lower()
    if repo and pushed_to and pushed_to != repo:
        return {"ignored": "a different repository"}
    default = (payload.get("repository") or {}).get("default_branch")
    branch = os.getenv("VAULT_BRANCH") or default or "main"
    if payload.get("ref") != f"refs/heads/{branch}":
        return {"ignored": f"not the {branch} branch"}
    if payload.get("deleted"):
        return {"ignored": "the branch was deleted"}
    commits = payload.get("commits") or []
    if payload.get("forced") or len(commits) >= MAX_COMMITS_LISTED:
        return {"full": True}                    # history was rewritten, or the list is cut short
    changed, removed = [], []
    for commit in commits:
        changed += [p for p in (commit.get("added") or []) + (commit.get("modified") or []) if isinstance(p, str)]
        removed += [p for p in (commit.get("removed") or []) if isinstance(p, str)]
    changed = [p for p in dict.fromkeys(changed) if vault_sync.indexable(p)]
    removed = [p for p in dict.fromkeys(removed) if vault_sync.indexable(p)]
    if not changed and not removed:
        return {"ignored": "no notes changed"}
    return {"changed": changed, "removed": removed}


def process(plan: dict) -> dict:
    """Do the sync a push asked for, in a session of its own. Never raises."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        if plan.get("full"):
            return vault_sync.sync_vault(db, source="webhook")
        return vault_sync.sync_paths(db, plan.get("changed") or [], plan.get("removed") or [], source="webhook")
    except Exception as exc:  # noqa: BLE001
        print(f"[vault_webhook] sync failed: {vault_sync.redact_credentials(exc)}")
        return {"errors": [type(exc).__name__]}
    finally:
        db.close()


def _start(plan: dict) -> None:
    threading.Thread(target=process, args=(plan,), daemon=True, name="vault-webhook-sync").start()


@router.post("/vault/webhook")
async def vault_webhook(request: Request):
    secret = webhook_secret()
    if len(secret) < MIN_SECRET_LENGTH:
        raise HTTPException(503, "GITHUB_WEBHOOK_SECRET is not set, or is shorter than 16 characters.")
    body = await request.body()
    given = request.headers.get("X-Hub-Signature-256") or ""
    if not hmac.compare_digest(given.encode("utf-8"), signature_for(secret, body).encode("utf-8")):
        raise HTTPException(401, "Bad signature")
    event = request.headers.get("X-GitHub-Event") or ""
    if event == "ping":
        return {"ok": True, "pong": True}
    if event != "push":
        return {"ok": True, "ignored": f"event '{event}'"}
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "The body is not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "The body is not a push event")
    plan = plan_push(payload)
    if plan.get("ignored"):
        return {"ok": True, "ignored": plan["ignored"]}
    _start(plan)
    return JSONResponse(status_code=202, content={
        "ok": True, "accepted": True, "full": bool(plan.get("full")),
        "changed": len(plan.get("changed") or []), "removed": len(plan.get("removed") or [])})
