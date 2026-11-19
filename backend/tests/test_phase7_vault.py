"""
Phase 7, Stage 3 — the live vault.

No network and no git: the pull is patched, the vault is a temporary folder, and GitHub's
contents API is a small fake. Covers
  1. the webhook's own check (HMAC under GITHUB_WEBHOOK_SECRET, no session)
  2. a push re-reads and re-embeds only the files it lists
  3. pushes that are ignored, and pushes that force a full sync
  4. when the vault was last synced, for Settings; the manual sync as the fallback
  5. the daily export into ``Founder OS/``: what is written, what is skipped, what is removed
"""

import base64
import hashlib
import hmac
import json
from unittest.mock import patch
from urllib.parse import unquote

import pytest

import auth
import change_log
import models
import orchestrator_tools as ot
import vault_export
import vault_sync
import vault_webhook
from tests.conftest import TestSessionLocal, client

SECRET = "a-synthetic-webhook-secret-of-32-chars!!"
WRITE_TOKEN = "github_pat_SYNTHETICTOKEN0000"


def _db():
    return TestSessionLocal()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault on disk with three notes, already indexed. The pull is a no-op."""
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "Founder OS" / "Books").mkdir(parents=True)
    (root / "Pricing.md").write_text("Pricing notes. Charge for the outcome, not the hours.", encoding="utf-8")
    (root / "Projects" / "Website.md").write_text("Website project. Launch checklist and copy.", encoding="utf-8")
    (root / "Journal.md").write_text("A quiet week. Sprint training went well.", encoding="utf-8")
    (root / ".obsidian" / "workspace.md").write_text("editor state, not a note", encoding="utf-8")
    (root / "Founder OS" / "Books" / "deep-work.md").write_text("# Deep Work\n\nwritten by the assistant", encoding="utf-8")
    monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", str(root))
    monkeypatch.setattr(vault_sync, "_git_refresh", lambda: (True, None, False))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    db = _db()
    vault_sync.index_vault(db)
    db.close()
    return root


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    """Run the webhook's work in the request, against the test database."""
    monkeypatch.setattr(vault_webhook, "_start", vault_webhook.process)
    with patch("database.SessionLocal", TestSessionLocal):
        yield


def _push(payload: dict, *, secret: str = SECRET, event: str = "push", signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature is None:
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if signature:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/vault/webhook", content=body, headers=headers)


def _payload(added=(), modified=(), removed=(), ref="refs/heads/main", **extra):
    return {"ref": ref, "repository": {"full_name": "someone/vault", "default_branch": "main"},
            "commits": [{"added": list(added), "modified": list(modified), "removed": list(removed)}], **extra}


def _chunks() -> dict:
    db = _db()
    try:
        return {c.file_path: (c.id, c.content) for c in db.query(models.ColdArchiveChunk).all()}
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════
# 1. The webhook checks GitHub's signature, and nothing else lets a request in
# ════════════════════════════════════════════════════════════════

def test_the_webhook_takes_no_session_and_checks_the_signature(vault, monkeypatch):
    assert auth.is_exempt("/vault/webhook", "POST") is True
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "a-password")       # a session is required everywhere else
    assert client.get("/vault/status").status_code == 401
    assert _push({"zen": "x"}, event="ping").json() == {"ok": True, "pong": True}

    payload = _payload(modified=["Pricing.md"])
    assert _push(payload, signature="").status_code == 401                       # no signature
    assert _push(payload, signature="sha256=" + "0" * 64).status_code == 401     # a wrong one
    assert _push(payload, secret="another-secret-of-thirty-two-chars!!").status_code == 401
    assert _push(payload).status_code == 202


@pytest.mark.parametrize("secret", ["", "short-secret"])
def test_without_a_real_secret_the_webhook_refuses_everything(vault, monkeypatch, secret):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    before = _chunks()
    r = _push(_payload(removed=["Pricing.md"]), secret=secret or "x")
    assert r.status_code == 503 and "GITHUB_WEBHOOK_SECRET" in r.json()["detail"]
    assert _chunks() == before


def test_a_body_that_is_not_a_push_is_refused_or_ignored(vault):
    assert _push({"action": "opened"}, event="issues").json() == {"ok": True, "ignored": "event 'issues'"}
    body = b"not json"
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/vault/webhook", content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": signature})
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════
# 2. A push re-reads and re-embeds only the files it lists
# ════════════════════════════════════════════════════════════════

def test_a_push_rereads_only_the_listed_files(vault, monkeypatch):
    before = _chunks()
    assert set(before) == {"Pricing.md", "Projects/Website.md", "Journal.md"}      # not .obsidian, not Founder OS/

    (vault / "Pricing.md").write_text("Pricing notes, rewritten. Anchor high, then discount once.", encoding="utf-8")
    (vault / "Projects" / "Clinic.md").write_text("Clinic booking project. First call on Friday.", encoding="utf-8")
    (vault / "Journal.md").unlink()
    # Changed on disk but NOT in the push: it must be left exactly as it was.
    (vault / "Projects" / "Website.md").write_text("changed outside this push", encoding="utf-8")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    embedded = []
    with patch("llm.embed_texts", side_effect=lambda texts, **_: embedded.append(list(texts)) or [[1.0, 0.0] for _t in texts]):
        r = _push(_payload(added=["Projects/Clinic.md"], modified=["Pricing.md"], removed=["Journal.md"]))
    assert r.status_code == 202 and r.json() == {"ok": True, "accepted": True, "full": False, "changed": 2, "removed": 1}

    after = _chunks()
    assert set(after) == {"Pricing.md", "Projects/Website.md", "Projects/Clinic.md"}
    assert after["Projects/Website.md"] == before["Projects/Website.md"]            # same row, same text
    assert "rewritten" in after["Pricing.md"][1] and after["Projects/Clinic.md"][1].startswith("Clinic booking")
    # Embeddings: the two files the push changed, and the untouched file once (it had none
    # before, because the first index ran without a key). Nothing is embedded twice.
    flat = [text for batch in embedded for text in batch]
    assert sorted(flat) == sorted([after["Pricing.md"][1], after["Projects/Clinic.md"][1], after["Projects/Website.md"][1]])

    embedded.clear()
    (vault / "Pricing.md").write_text("Pricing notes, a third version.", encoding="utf-8")
    with patch("llm.embed_texts", side_effect=lambda texts, **_: embedded.append(list(texts)) or [[1.0, 0.0] for _t in texts]):
        _push(_payload(modified=["Pricing.md"]))
    assert embedded == [["Pricing notes, a third version."]]                         # only the changed text
    db = _db()
    assert db.query(models.Embedding).filter(models.Embedding.source_type == "vault").count() == 3
    db.close()


def test_a_file_listed_as_changed_but_gone_is_removed(vault):
    (vault / "Pricing.md").unlink()
    _push(_payload(modified=["Pricing.md"]))
    assert "Pricing.md" not in _chunks()


def test_several_commits_in_one_push_are_read_together(vault):
    (vault / "New.md").write_text("A new note.", encoding="utf-8")
    payload = _payload()
    payload["commits"] = [{"added": ["New.md"], "modified": [], "removed": []},
                          {"added": [], "modified": ["New.md", "Pricing.md"], "removed": []}]
    assert _push(payload).json()["changed"] == 2
    assert "New.md" in _chunks()


# ════════════════════════════════════════════════════════════════
# 3. Ignored pushes, and pushes that need the whole vault read again
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("payload, why", [
    (_payload(modified=["Pricing.md"], ref="refs/heads/drafts"), "not the main branch"),
    (_payload(modified=["Founder OS/Books/deep-work.md"]), "no notes changed"),       # the assistant's own export
    (_payload(added=[".obsidian/workspace.md", "attachments/photo.png", "notes.txt"]), "no notes changed"),
    (_payload(modified=["../outside.md", "Projects/../../secrets.md"]), "no notes changed"),
    (_payload(modified=["Pricing.md"], deleted=True), "the branch was deleted"),
    (_payload(), "no notes changed"),
])
def test_pushes_that_change_no_note_are_ignored(vault, payload, why):
    before = _chunks()
    with patch.object(vault_sync, "_git_refresh", side_effect=AssertionError("nothing should be pulled")):
        r = _push(payload)
    assert r.status_code == 200 and r.json() == {"ok": True, "ignored": why}
    assert _chunks() == before


def test_a_push_to_another_repository_is_ignored(vault, monkeypatch):
    monkeypatch.setenv("VAULT_REPO", "bandile/vault")
    assert _push(_payload(modified=["Pricing.md"])).json()["ignored"] == "a different repository"
    monkeypatch.setenv("VAULT_REPO", "Someone/Vault")
    assert _push(_payload(modified=["Pricing.md"])).status_code == 202


def test_a_forced_push_or_a_cut_off_commit_list_reads_the_whole_vault(vault):
    (vault / "Journal.md").unlink()
    (vault / "Projects" / "Website.md").write_text("rewritten outside any listed commit", encoding="utf-8")
    r = _push(_payload(forced=True))
    assert r.json()["full"] is True
    after = _chunks()
    assert "Journal.md" not in after and after["Projects/Website.md"][1].startswith("rewritten")

    long_push = _payload()
    long_push["commits"] = [{"added": [], "modified": ["Pricing.md"], "removed": []}] * 20
    assert _push(long_push).json()["full"] is True


def test_the_full_index_never_reads_the_export_folder_and_drops_files_that_are_gone(vault):
    db = _db()
    db.add(models.ColdArchiveChunk(file_path="Old.md", file_title="Old", content="left over", chunk_index=0, word_count=2))
    db.commit()
    out = vault_sync.index_vault(db)
    db.close()
    assert out["files_processed"] == 3 and out["files_removed"] == 1
    assert set(_chunks()) == {"Pricing.md", "Projects/Website.md", "Journal.md"}
    assert not vault_sync.indexable("Founder OS/Inbox/inbox.md") and vault_sync.indexable("Founder OS notes.md")


# ════════════════════════════════════════════════════════════════
# 4. When the vault was last synced; the manual sync still works
# ════════════════════════════════════════════════════════════════

def _vault_row():
    return next(i for i in client.get("/integrations").json() if i["provider"] == "vault")


def test_every_sync_records_when_it_ran_and_what_started_it(vault, monkeypatch):
    assert _vault_row()["last_sync_at"] is None and client.get("/vault/status").json()["live"] is True
    _push(_payload(modified=["Pricing.md"]))
    status = client.get("/vault/status").json()
    assert status["last_sync_at"] and status["last_source"] == "webhook" and status["last_error"] is None
    row = _vault_row()
    assert row["status"] == "connected" and row["last_sync_at"] and row["account_label"] == "updates on every push · daily export off"

    manual = client.post("/vault/sync").json()                                # the fallback
    assert manual["files_processed"] == 3 and manual["errors"] == []
    assert client.get("/vault/status").json()["last_source"] == "manual"


def test_a_failed_pull_is_recorded_without_the_token(vault, monkeypatch):
    _push(_payload(modified=["Pricing.md"]))
    good = client.get("/vault/status").json()["last_sync_at"]
    token = "ghp_SYNTHETIC0000TOKEN"
    monkeypatch.setattr(vault_sync, "VAULT_REPO_URL", f"https://x-access-token:{token}@github.com/someone/vault.git")
    monkeypatch.setattr(vault_sync, "_git_refresh", lambda: (False, vault_sync.redact_credentials(
        f"fatal: unable to access 'https://x-access-token:{token}@github.com/someone/vault.git/'"), False))
    before = _chunks()
    assert _push(_payload(modified=["Pricing.md"])).status_code == 202
    row, status = _vault_row(), client.get("/vault/status").json()
    assert row["status"] == "error" and "unable to access" in row["last_error"]
    assert token not in json.dumps(row) + json.dumps(status)
    assert status["last_sync_at"] == good and _chunks() == before             # the last good sync still stands


# ════════════════════════════════════════════════════════════════
# 5. The daily export into Founder OS/
# ════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeGitHub:
    """The contents API of one repository: files by path, each with a blob sha."""

    def __init__(self):
        self.files, self.calls, self.fail = {}, [], None

    def _path(self, url):
        assert url.startswith("https://api.github.com/repos/someone/vault/contents/")
        return unquote(url.split("/contents/", 1)[1])

    def put(self, url, headers=None, json=None, timeout=None):
        path = self._path(url)
        self.calls.append(("PUT", path, json, headers))
        if self.fail:
            return _Resp(*self.fail)
        current = self.files.get(path)
        if (current and json.get("sha") != current["sha"]) or (not current and json.get("sha")):
            return _Resp(409 if json.get("sha") else 422, {"message": "sha does not match"})
        text = base64.b64decode(json["content"]).decode("utf-8")
        sha = hashlib.sha1(text.encode()).hexdigest()
        self.files[path] = {"sha": sha, "text": text}
        return _Resp(200 if current else 201, {"content": {"sha": sha}})

    def get(self, url, headers=None, params=None, timeout=None):
        path = self._path(url)
        self.calls.append(("GET", path, None, headers))
        return _Resp(200, {"sha": self.files[path]["sha"]}) if path in self.files else _Resp(404, {"message": "Not Found"})

    def request(self, method, url, headers=None, json=None, timeout=None):
        path = self._path(url)
        self.calls.append((method, path, json, headers))
        if path not in self.files:
            return _Resp(404, {"message": "Not Found"})
        if json.get("sha") != self.files[path]["sha"]:
            return _Resp(409, {"message": "sha does not match"})
        del self.files[path]
        return _Resp(200, {})

    def done(self):
        calls, self.calls = [(method, path) for method, path, _json, _headers in self.calls], []
        return calls


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv("VAULT_REPO", "someone/vault")
    monkeypatch.setenv("VAULT_WRITE_TOKEN", WRITE_TOKEN)
    fake = FakeGitHub()
    with patch("vault_export.httpx.put", fake.put), patch("vault_export.httpx.get", fake.get), \
            patch("vault_export.httpx.request", fake.request):
        yield fake


def _assistant(tool, args):
    db = _db()
    change_log.begin_batch(db, "assistant")
    try:
        result, _summary = ot.run_tool(tool, args, db)
    finally:
        change_log.end_batch(db)
        db.close()
    assert "error" not in result, result
    return result


def _export():
    db = _db()
    try:
        return vault_export.export(db)
    finally:
        db.close()


def test_the_export_is_skipped_until_the_repository_and_the_token_are_set(monkeypatch):
    _assistant("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1"})
    with patch("vault_export.httpx.put", side_effect=AssertionError("nothing may be sent")):
        assert _export()["skipped"] == "VAULT_REPO and VAULT_WRITE_TOKEN are not set"
        monkeypatch.setenv("VAULT_REPO", "someone/vault")
        assert _export()["skipped"]                                           # the token is still missing
    import agents
    from agents import runner
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        out = runner.run_agent(agents.REGISTRY["vault_export"], "manual")
    assert out["status"] == "skipped" and "not set" in out["summary"]


def test_the_export_writes_the_assistants_notes_into_one_folder(github):
    _assistant("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1: rules for focus."})
    _assistant("notes_create", {"title": "Morning pages", "kind": "journal", "body": "Slept well."})
    db = _db()                                                # a note only ever touched on a screen stays out
    by_hand = models.Note(title="Typed by hand", kind="general", body="not the assistant's")
    db.add(by_hand)
    change_log.record_create(db, by_hand, "Created note", source="ui")
    db.commit()
    db.close()

    out = _export()
    assert (out["exported"], out["removed"], out["unchanged"], out["errors"]) == (2, 0, 0, [])
    assert set(github.files) == {"Founder OS/Books/deep-work.md", "Founder OS/Journal/morning-pages.md"}
    text = github.files["Founder OS/Books/deep-work.md"]["text"]
    assert text.startswith("# Deep Work\n\nChapter 1: rules for focus.\n\n---\nBook note")
    assert "Changes made here are overwritten" in text
    method, path, body, headers = github.calls[0]
    assert (method, body["branch"], headers["Authorization"]) == ("PUT", "main", f"Bearer {WRITE_TOKEN}")
    assert body["message"] == "Founder OS: Deep Work" and "sha" not in body
    github.done()

    assert _export()["unchanged"] == 2 and github.done() == []               # nothing changed: no request at all


def test_a_changed_renamed_or_deleted_note_follows_through(github):
    note = _assistant("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1"})
    _export()
    first_sha = github.files["Founder OS/Books/deep-work.md"]["sha"]
    github.done()

    _assistant("notes_append", {"title": "Deep Work", "text": "Finished chapter 6", "kind": "book"})
    assert _export()["exported"] == 1
    assert github.calls[0][2]["sha"] == first_sha                             # an update names the file it replaces
    assert "Finished chapter 6" in github.files["Founder OS/Books/deep-work.md"]["text"]
    github.done()

    db = _db()
    row = db.query(models.Note).filter(models.Note.id == note["id"]).one()
    row.title = "Deep Work (Newport)"
    db.commit()
    db.close()
    assert _export()["exported"] == 1
    assert github.done() == [("DELETE", "Founder OS/Books/deep-work.md"), ("PUT", "Founder OS/Books/deep-work-newport.md")]
    assert set(github.files) == {"Founder OS/Books/deep-work-newport.md"}

    db = _db()
    db.query(models.Note).filter(models.Note.id == note["id"]).delete()
    db.commit()
    db.close()
    out = _export()
    assert out["removed"] == 1 and github.files == {} and _export()["removed"] == 0


def test_a_file_edited_by_hand_in_the_folder_is_written_over(github):
    _assistant("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1"})
    _export()
    github.files["Founder OS/Books/deep-work.md"] = {"sha": "edited-by-hand", "text": "my own edit"}
    _assistant("notes_append", {"title": "Deep Work", "text": "Chapter 2", "kind": "book"})
    github.done()
    assert _export()["exported"] == 1
    assert github.done() == [("PUT", "Founder OS/Books/deep-work.md"), ("GET", "Founder OS/Books/deep-work.md"),
                             ("PUT", "Founder OS/Books/deep-work.md")]
    assert "Chapter 2" in github.files["Founder OS/Books/deep-work.md"]["text"]


def test_two_notes_with_one_title_get_two_files_and_the_file_text_is_stable(github):
    one = _assistant("notes_create", {"title": "Ideas", "kind": "general", "body": "first"})
    db = _db()
    twin = models.Note(title="Ideas!", kind="general", body="second")
    db.add(twin)
    change_log.record_create(db, twin, "Created note", source="assistant")
    db.commit()
    assert vault_export.render(twin) == vault_export.render(twin)
    db.close()
    assert _export()["exported"] == 2
    assert set(github.files) == {"Founder OS/Notes/ideas.md", f"Founder OS/Notes/ideas-{one['id'] + 1}.md"}


def test_a_refusal_from_github_is_reported_without_the_token_and_tried_again_next_time(github):
    _assistant("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1"})
    github.fail = (403, {"message": f"Resource not accessible by token {WRITE_TOKEN}"})
    out = _export()
    assert out["exported"] == 0 and len(out["errors"]) == 1
    assert "GitHub answered 403" in out["errors"][0] and WRITE_TOKEN not in out["errors"][0]
    github.fail = None
    assert _export()["exported"] == 1

    import agents
    from agents import runner
    _assistant("notes_append", {"title": "Deep Work", "text": "more", "kind": "book"})
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        ran = runner.run_agent(agents.REGISTRY["vault_export"], "manual")
    assert ran["status"] == "completed" and ran["summary"] == "1 note(s) written to the vault"
    spec = agents.REGISTRY["vault_export"]
    assert spec.schedule.at_time == "19:30" and spec.permissions.max_llm_calls == 0 and not spec.permissions.may_notify
