"""
Phase 7 live check — how the assistant answers from the vault.

Three things the live test showed, each driven through a whole assistant turn with the model
scripted (no network, no git; embeddings are small fixed vectors):
  1. a search whose matches are weak says nothing relevant was found, and names no note
  2. a question about a note gets the best match's whole text, so it is answered, not offered
  3. vault_recent lists the files added, changed or removed in the last N days
"""

import hashlib
import hmac
import json
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest

import models
import orchestrator_tools as ot
import user_clock
import vault_sync
import vault_webhook
from tests.conftest import TestSessionLocal, client

SECRET = "a-synthetic-webhook-secret-of-32-chars!!"
# A note long enough to span several chunks: 300 different words, about 2,700 characters.
LONG_NOTE = " ".join(f"price{i:04d}" for i in range(300))


def _db():
    return TestSessionLocal()


def _embed(texts, db=None, purpose="x"):
    """Each note points its own way. Any other text (a question about tax, say) leans a
    little toward all of them (cosine 0.25 with each): a weak match, not an answer."""
    out = []
    for text in texts:
        low = text.lower()
        if "pric" in low:
            out.append([1.0, 0.0, 0.0, 0.0])
        elif "sprint" in low:
            out.append([0.0, 1.0, 0.0, 0.0])
        elif "website" in low:
            out.append([0.0, 0.0, 1.0, 0.0])
        else:
            out.append([0.25, 0.25, 0.25, 0.9014])
    return out


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault on disk with three notes, indexed and embedded. The pull is a no-op."""
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / "Pricing.md").write_text(LONG_NOTE, encoding="utf-8")
    (root / "Projects" / "Website.md").write_text("Website project. Launch checklist and copy.", encoding="utf-8")
    (root / "Journal.md").write_text("A quiet week. Sprint training went well.", encoding="utf-8")
    monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", str(root))
    monkeypatch.setattr(vault_sync, "_git_refresh", lambda: (True, None, False))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(vault_webhook, "_start", vault_webhook.process)
    with patch("database.SessionLocal", TestSessionLocal), patch("llm.embed_texts", side_effect=_embed):
        db = _db()
        vault_sync.sync_vault(db, source="startup")
        db.close()
        yield root


def _ask(text, tool, args):
    """One turn in which the scripted model calls ``tool`` once. Returns what the model was
    given back for that call (the tool message, as text) and the system prompt it ran under."""
    rounds = [[{"tool_calls": [{"index": 0, "id": "call_0", "name": tool, "arguments": json.dumps(args)}]},
               {"finish_reason": "tool_calls"}],
              [{"content": "Done."}, {"finish_reason": "stop"}]]
    seen = []

    def fake(convo, tools):
        seen.append(([dict(m) for m in convo], [t["function"]["name"] for t in tools]))
        yield from rounds[min(len(seen), len(rounds)) - 1]

    with patch("openai_client._stream_completion", fake):
        r = client.post("/orchestrator", json={"text": text, "session_id": "s"})
    assert r.status_code == 200 and len(seen) == 2
    convo, tool_names = seen[1]
    assert tool in tool_names                                   # the model is offered the tool
    given = [m["content"] for m in convo if m.get("role") == "tool"]
    assert len(given) == 1
    return given[0], convo[0]["content"]


def _push(added=(), modified=(), removed=()):
    payload = {"ref": "refs/heads/main", "repository": {"full_name": "someone/vault", "default_branch": "main"},
               "commits": [{"added": list(added), "modified": list(modified), "removed": list(removed)}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/vault/webhook", content=body, headers={
        "X-GitHub-Event": "push", "Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert r.status_code == 202
    return r


def _recent(days=1) -> dict:
    db = _db()
    try:
        result, _summary = ot.run_tool("vault_recent", {"days": days}, db)
    finally:
        db.close()
    assert "error" not in result, result
    return result


def _paths(rows) -> list:
    return sorted(row["file_path"] for row in rows)


# ════════════════════════════════════════════════════════════════
# 1. Weak matches are not answers
# ════════════════════════════════════════════════════════════════

def test_a_search_with_only_weak_matches_says_nothing_relevant_was_found(vault):
    given, _prompt = _ask("What did I write about my tax return?", "query_cold_archive", {"query": "tax return"})
    assert "Nothing relevant" in given
    # No unrelated note reaches the model, by title or by text, so none can be presented.
    for unrelated in ("Pricing", "Journal", "Website", "price0000", "Sprint training"):
        assert unrelated not in given

    db = _db()
    result, summary = ot.run_tool("query_cold_archive", {"query": "tax return"}, db)
    db.close()
    assert result["results"] == [] and "best_match" not in result and summary.startswith("nothing relevant")


def test_exact_words_still_find_a_note_when_the_meaning_match_is_weak(vault):
    """Nothing is close in meaning here, but the words are in a note: that is a match."""
    db = _db()
    result, _summary = ot.run_tool("query_cold_archive", {"query": "Launch checklist"}, db)
    db.close()
    assert result["mode"] == "keyword" and result["best_match"]["file_path"] == "Projects/Website.md"


def test_the_prompt_tells_the_assistant_to_say_so(vault):
    _given, prompt = _ask("What did I write about my tax return?", "query_cold_archive", {"query": "tax return"})
    assert "nothing relevant" in prompt.lower()


# ════════════════════════════════════════════════════════════════
# 2. A question about a note is answered from the note
# ════════════════════════════════════════════════════════════════

def test_asking_about_a_note_hands_the_model_the_whole_best_match(vault):
    given, prompt = _ask("What does my pricing note say?", "query_cold_archive", {"query": "pricing note"})
    # The end of the note is seven chunks in: far past any excerpt.
    assert "price0000" in given and "price0299" in given
    assert "Do not offer to read it" in given
    assert "never offer to read" in prompt.lower()

    db = _db()
    result, _summary = ot.run_tool("query_cold_archive", {"query": "pricing note"}, db)
    db.close()
    best = result["best_match"]
    assert (best["file_title"], best["file_path"], best["truncated"]) == ("Pricing", "Pricing.md", False)
    assert best["text"] == LONG_NOTE                  # the chunks' overlapping words appear once


def test_a_very_long_note_is_cut_and_says_so(vault, monkeypatch):
    monkeypatch.setattr(ot, "VAULT_BEST_MATCH_CHARS", 500)
    db = _db()
    result, _summary = ot.run_tool("query_cold_archive", {"query": "pricing note"}, db)
    db.close()
    best = result["best_match"]
    assert best["truncated"] is True and len(best["text"]) <= 500 and best["text"].startswith("price0000")


# ════════════════════════════════════════════════════════════════
# 3. vault_recent: what changed in the last N days
# ════════════════════════════════════════════════════════════════

def test_what_did_i_add_to_my_vault_today(vault):
    (vault / "Projects" / "Clinic.md").write_text("Clinic booking project. First call on Friday.", encoding="utf-8")
    (vault / "Pricing.md").write_text("Pricing notes, rewritten. Anchor high.", encoding="utf-8")
    (vault / "Journal.md").unlink()
    _push(added=["Projects/Clinic.md"], modified=["Pricing.md"], removed=["Journal.md"])

    given, _prompt = _ask("What did I add to my vault today?", "vault_recent", {"days": 1})
    shown = json.loads(given)
    assert [f["file path"] for f in shown["added"]] == ["Projects/Clinic.md"]
    assert [f["file path"] for f in shown["changed"]] == ["Pricing.md"]
    assert [f["file path"] for f in shown["removed"]] == ["Journal.md"]
    assert shown["added"][0]["file title"] == "Clinic" and shown["added"][0]["when"].startswith("today at ")
    assert "Website" not in given                                # untouched, so not listed

    result = _recent()
    assert (result["days"], result["count"]) == (1, 3)


def test_the_first_index_of_an_empty_vault_table_is_not_a_change(vault):
    """The fixture indexed three notes into an empty table: that is the vault arriving, not
    three notes being added today."""
    result = _recent()
    assert result["count"] == 0 and result["added"] == []
    given, _prompt = _ask("What did I add to my vault today?", "vault_recent", {"days": 1})
    assert "No vault files were added, changed or removed" in given and "last synced" in given


def test_only_the_last_n_days_are_listed_and_a_day_is_the_users_day(vault):
    midnight = user_clock.to_utc(datetime.combine(user_clock.today(), time.min).replace(tzinfo=user_clock.tz()))
    db = _db()
    db.add(models.VaultChange(file_path="Old/Yesterday.md", file_title="Yesterday", change="added",
                              changed_at=midnight - timedelta(minutes=1), source="webhook"))
    db.add(models.VaultChange(file_path="Early.md", file_title="Early", change="added",
                              changed_at=midnight, source="webhook"))        # 00:00 here, still yesterday in UTC
    db.add(models.VaultChange(file_path="Old/LastWeek.md", file_title="LastWeek", change="modified",
                              changed_at=midnight - timedelta(days=5), source="webhook"))
    db.commit()
    db.close()
    assert _paths(_recent(1)["added"]) == ["Early.md"]
    two = _recent(2)
    assert _paths(two["added"]) == ["Early.md", "Old/Yesterday.md"] and two["changed"] == []
    assert _paths(two["added"])[1] and [f["when"] for f in two["added"] if f["file_path"] == "Old/Yesterday.md"][0].startswith("yesterday at ")
    assert _paths(_recent(7)["changed"]) == ["Old/LastWeek.md"]


def test_a_file_pushed_several_times_is_listed_once_and_unchanged_text_is_no_change(vault):
    (vault / "New.md").write_text("A new note.", encoding="utf-8")
    _push(added=["New.md"])
    (vault / "New.md").write_text("A new note, with a second line.", encoding="utf-8")
    _push(modified=["New.md"])
    _push(modified=["New.md", "Projects/Website.md"])            # listed, but the text is the same
    result = _recent()
    assert _paths(result["added"]) == ["New.md"] and result["changed"] == [] and result["count"] == 1

    (vault / "New.md").unlink()                                  # added and removed in the same window: gone
    _push(removed=["New.md"])
    assert _recent()["count"] == 0


def test_a_full_sync_records_only_the_files_whose_text_changed(vault):
    """A push that arrived while the server slept is picked up by the startup sync, which
    reads everything: only what is different counts."""
    (vault / "Projects" / "Clinic.md").write_text("Clinic booking project.", encoding="utf-8")
    (vault / "Journal.md").write_text("A quiet week. Sprint training went well. And a rest day.", encoding="utf-8")
    (vault / "Projects" / "Website.md").unlink()
    db = _db()
    vault_sync.sync_vault(db, source="startup")
    db.close()
    result = _recent()
    assert _paths(result["added"]) == ["Projects/Clinic.md"]
    assert _paths(result["changed"]) == ["Journal.md"]
    assert _paths(result["removed"]) == ["Projects/Website.md"]


def test_old_changes_are_dropped_after_ninety_days(vault):
    db = _db()
    db.add(models.VaultChange(file_path="Ancient.md", file_title="Ancient", change="added",
                              changed_at=datetime.utcnow() - timedelta(days=120), source="webhook"))
    db.commit()
    db.close()
    (vault / "New.md").write_text("A new note.", encoding="utf-8")
    _push(added=["New.md"])
    db = _db()
    kept = [c.file_path for c in db.query(models.VaultChange).all()]
    db.close()
    assert kept == ["New.md"]
    assert _recent(400)["days"] == 90                             # N is capped at what is kept
