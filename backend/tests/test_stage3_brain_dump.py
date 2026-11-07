"""
Rebuild Stage 3 — brain-dump rework, notes, retrieval, new tools.

OpenAI is mocked throughout. Embedding tests patch ``llm.embed_texts`` with a
deterministic fake so similarity ranking is testable offline.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import notes
import retrieval
import memory_service
import syllabus
import vault_sync
import orchestrator_tools as ot
from migrations.m012_notes_currency_retrieval import parse_due
from tests.conftest import TestSessionLocal, client

TODAY = date.today()


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZMW_PER_USD", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _parsed(**overrides):
    base = {
        "summary": "logged", "kpi_updates": [], "todos_add": [], "todos_complete": [],
        "roadmap_complete": [], "habits_done": [], "annual_updates": [], "reading_updates": [],
        "bible_log": [], "revenue_updates": [], "log_entry": None, "advisory": None,
    }
    base.update(overrides)
    return base


def _parse(payload, text="brain dump"):
    with patch("main.get_parse_response", return_value=payload):
        r = client.post("/parse", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def _todo(text, due=None, done=False):
    db = _db()
    t = models.Todo(text=text, priority=5, category="personal", due=due or str(TODAY),
                    due_date=parse_due(due or str(TODAY)), source="manual", done=done)
    db.add(t)
    db.commit()
    tid = t.id
    db.close()
    return tid


# ════════════════════════════════════════════════════════════════
# 1. Completing tasks by free text (acceptance test 1)
# ════════════════════════════════════════════════════════════════

class TestCompleteTask:

    def test_finished_business_essay_completes_logs_and_undoes(self):
        tid = _todo("Write the Business essay")
        _todo("Sprint drills")
        data = _parse(_parsed(todos_complete=["Business essay"]), text="I finished the Business essay")
        assert data["updates"]["todos_completed"] == [{"id": tid, "text": "Write the Business essay"}]
        db = _db()
        t = db.query(models.Todo).filter(models.Todo.id == tid).first()
        assert t.done is True and t.completed_at == TODAY
        db.close()
        change = next(c for c in data["updates"]["changes"] if c["table"] == "todos")
        assert change["action"] == "update"
        client.post(f"/changes/{change['id']}/undo")
        db = _db()
        t = db.query(models.Todo).filter(models.Todo.id == tid).first()
        assert t.done is False and t.completed_at is None
        db.close()

    def test_full_sentence_fragment_still_matches(self):
        tid = _todo("Write the Business essay")
        data = _parse(_parsed(todos_complete=["finished the business essay"]))
        assert data["updates"]["todos_completed"][0]["id"] == tid

    def test_ambiguous_match_becomes_suggestions(self):
        a = _todo("Business essay draft")
        b = _todo("Business essay references")
        data = _parse(_parsed(todos_complete=["Business essay"]))
        assert data["updates"]["todos_completed"] == []
        sugg = data["updates"]["suggestions"]
        assert {s["kind"] for s in sugg} == {"todo_complete"}
        assert {s["payload"]["todo_id"] for s in sugg} == {a, b}
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.done == True).count() == 0
        db.close()
        # one tap completes the right one
        pick = next(s for s in sugg if s["payload"]["todo_id"] == b)
        r = client.post(f"/suggestions/{pick['id']}/accept").json()
        assert r["applied"] is True
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == b).first().done is True
        assert db.query(models.Todo).filter(models.Todo.id == a).first().done is False
        db.close()

    def test_no_match_is_reported_not_invented(self):
        _todo("Sprint drills")
        data = _parse(_parsed(todos_complete=["Business essay"]))
        assert data["updates"]["todos_completed"] == []
        assert data["updates"]["todos_complete_unmatched"] == ["Business essay"]
        assert data["updates"]["suggestions"] == []

    def test_complete_task_tool(self):
        tid = _todo("Email the insurance CEO")
        db = _db()
        result, summary = ot.run_tool("complete_task", {"query": "CEO email"}, db)
        db.close()
        assert result["completed"] is True and result["id"] == tid
        db = _db()
        again, _ = ot.run_tool("complete_task", {"todo_id": tid}, db)
        db.close()
        assert again["already_done"] is True

    def test_complete_task_tool_ambiguous(self):
        _todo("Business essay draft")
        _todo("Business essay references")
        db = _db()
        result, summary = ot.run_tool("complete_task", {"query": "Business essay"}, db)
        db.close()
        assert result["ambiguous"] is True and len(result["candidates"]) == 2
        assert client.get("/suggestions").json()


# ════════════════════════════════════════════════════════════════
# 2. Currency (acceptance test 9)
# ════════════════════════════════════════════════════════════════

class TestCurrency:

    def test_k500_stores_500_zmw(self):
        data = _parse(_parsed(revenue_updates=[{"amount": 500, "currency": "ZMW", "source": "client", "client": None}]),
                      text="Got K500 from a client")
        assert data["revenue_logged"] == [{"amount": 500.0, "currency": "ZMW", "source": "client"}]
        db = _db()
        r = db.query(models.Revenue).first()
        db.close()
        assert r.amount == 500 and r.currency == "ZMW"

    def test_missing_currency_defaults_usd_and_k_normalises(self):
        _parse(_parsed(revenue_updates=[
            {"amount": 100, "source": "a", "client": None},
            {"amount": 200, "currency": "K", "source": "b", "client": None},
        ]))
        db = _db()
        cur = {r.source: r.currency for r in db.query(models.Revenue).all()}
        db.close()
        assert cur == {"a": "USD", "b": "ZMW"}

    def test_scoreboard_converts_with_env_rate(self, monkeypatch):
        monkeypatch.setenv("ZMW_PER_USD", "25")
        db = _db()
        db.add(models.Revenue(amount=250, currency="ZMW", source="x", date=TODAY))
        db.add(models.Revenue(amount=40, currency="USD", source="y", date=TODAY))
        db.commit()
        db.close()
        board = client.get("/scoreboard/ten-k").json()
        assert board["ai_revenue_usd"] == 50.0           # 250/25 + 40
        assert board["zmw_per_usd"] == 25.0
        rev = client.get("/revenue").json()
        assert rev["by_currency"] == {"ZMW": 250.0, "USD": 40.0}
        assert rev["total_usd"] == 50.0
        assert rev["records"][0]["currency"] in ("ZMW", "USD")

    def test_post_revenue_accepts_currency(self):
        r = client.post("/revenue", json={"amount": 300, "currency": "zmw", "source": "s", "date": str(TODAY)})
        assert r.status_code == 200 and r.json()["currency"] == "ZMW"

    def test_prompt_example_keeps_kwacha(self):
        import openai_client, inspect
        src = inspect.getsource(openai_client.get_parse_response)
        assert '"amount": 500, "currency": "ZMW"' in src
        assert "trade_logs" not in src


# ════════════════════════════════════════════════════════════════
# 3. Trading removed from parser, tools and rules
# ════════════════════════════════════════════════════════════════

class TestTradingRemoved:

    def test_trade_logs_in_payload_are_ignored(self):
        data = _parse(_parsed(trade_logs=[{"type": "backtest", "pair": "EURUSD", "direction": "long", "r_multiple": 2}]))
        assert "trades_created" not in data["updates"]
        db = _db()
        assert db.query(models.BacktestTrade).count() == 0
        db.close()

    def test_no_trading_tool_or_rule(self):
        assert "backtest_stalled" not in {r["id"] for r in ot._load_rules()}
        assert "backtest_stalled" not in ot._RULE_CHECKS
        db = _db()
        state, _ = ot.get_dashboard_state(db)
        db.close()
        assert "gate" not in state and "ten_k_progress" not in state
        assert "trading" not in json.dumps(state).lower()

    def test_trading_endpoints_untouched(self):
        assert client.get("/trading/gate").status_code == 200


# ════════════════════════════════════════════════════════════════
# 4. Real due dates
# ════════════════════════════════════════════════════════════════

class TestDueDates:

    def test_parse_due_formats(self):
        assert parse_due("2026-11-10") == date(2026, 11, 10)
        assert parse_due("10/11/2026") == date(2026, 11, 10)
        assert parse_due("10 Nov 2026") == date(2026, 11, 10)
        assert parse_due("someday") is None
        assert parse_due("") is None and parse_due(None) is None

    def test_parser_sets_due_date(self):
        _parse(_parsed(todos_add=[{"text": "Pay fees", "due": "2026-12-01"}]))
        db = _db()
        t = db.query(models.Todo).first()
        db.close()
        assert t.due_date == date(2026, 12, 1) and t.due == "2026-12-01"

    def test_post_todo_sets_due_date(self):
        r = client.post("/todos", json={"text": "x", "due": "2026-12-02"})
        assert r.json()["due_date"] == "2026-12-02"

    def test_get_deadlines_tool(self):
        _todo("Overdue thing", due=str(TODAY - timedelta(days=3)))
        _todo("Today thing", due=str(TODAY))
        _todo("Soon thing", due=str(TODAY + timedelta(days=5)))
        _todo("Far thing", due=str(TODAY + timedelta(days=40)))
        _todo("Done thing", due=str(TODAY - timedelta(days=9)), done=True)
        db = _db()
        result, summary = ot.get_deadlines(db, 14)
        readable, _ = ot.run_tool("get_deadlines", {"days": 14}, db)
        db.close()
        assert readable["overdue"] == [readable["overdue"][0]] and "Overdue thing — 3 days overdue" in readable["overdue"][0]
        assert "Today thing" in readable["due today"][0]
        assert [t["text"] for t in result["overdue"]] == ["Overdue thing"]
        assert result["overdue"][0]["days"] == -3
        assert [t["text"] for t in result["due_today"]] == ["Today thing"]
        assert [t["text"] for t in result["upcoming"]] == ["Soon thing"]
        assert "1 overdue" in summary


# ════════════════════════════════════════════════════════════════
# 5. Notes (acceptance test 3)
# ════════════════════════════════════════════════════════════════

class TestNotes:

    def test_finished_chapter_6_creates_and_appends_book_note(self):
        data = _parse(_parsed(reading_updates=[{"title_fragment": "Deep Work", "progress": "finished chapter 6"}]),
                      text="Finished chapter 6 of Deep Work")
        assert data["updates"]["reading_notes_appended"] == ["Deep Work"]
        db = _db()
        note = notes.find_note(db, "Deep Work", "book")
        assert note is not None and f"- {TODAY.isoformat()}: finished chapter 6" in note.body
        db.close()
        # second entry appends to the same note
        _parse(_parsed(reading_updates=[{"title_fragment": "deep work", "progress": "chapter 7, on deliberate practice"}]))
        db = _db()
        all_notes = db.query(models.Note).filter(models.Note.kind == "book").all()
        db.close()
        assert len(all_notes) == 1
        assert "chapter 7" in all_notes[0].body and "chapter 6" in all_notes[0].body

    def test_book_in_queue_uses_its_full_title(self):
        db = _db()
        db.add(models.Book(title="Deep Work: Rules for Focused Success", author="Cal Newport", status="reading"))
        db.commit()
        db.close()
        data = _parse(_parsed(reading_updates=[{"title_fragment": "deep work", "progress": "finished chapter 6"}]))
        assert data["updates"]["reading_updates_applied"] == ["Deep Work: Rules for Focused Success"]
        db = _db()
        note = db.query(models.Note).filter(models.Note.kind == "book").first()
        db.close()
        assert note.title == "Deep Work: Rules for Focused Success"

    def test_read_romans_8_appends_to_bible_note(self):
        data = _parse(_parsed(bible_log=[{"book": "Romans", "chapter": 8, "notes": "nothing can separate us"}]),
                      text="read Romans 8")
        assert data["updates"]["bible_log_entries_created"] == 1
        db = _db()
        note = notes.find_note(db, "Bible", "bible")
        db.close()
        assert note is not None and "Romans 8 — nothing can separate us" in note.body

    def test_notes_endpoints(self):
        r = client.post("/notes", json={"title": "Journal", "kind": "journal", "body": "day one", "tags": ["a", "b"]})
        assert r.status_code == 201
        nid = r.json()["id"]
        assert r.json()["tags"] == ["a", "b"]
        r = client.post(f"/notes/{nid}/append", json={"text": "day two"})
        assert "day two" in r.json()["body"]
        r = client.patch(f"/notes/{nid}", json={"title": "My Journal"})
        assert r.json()["title"] == "My Journal"
        assert client.get(f"/notes/{nid}").json()["title"] == "My Journal"
        listed = client.get("/notes", params={"kind": "journal"}).json()
        assert listed[0]["id"] == nid and "preview" in listed[0]
        found = client.get("/notes", params={"q": "day two"}).json()
        assert found and found[0]["id"] == nid
        assert client.get("/changes").json()[0]["table"] == "notes"

    def test_notes_tools(self):
        db = _db()
        created, _ = ot.run_tool("notes_create", {"title": "Aether pricing", "kind": "general", "body": "Start at K2000/mo"}, db)
        again, s = ot.run_tool("notes_create", {"title": "Aether pricing"}, db)
        assert again["already_exists"] is True
        ot.run_tool("notes_append", {"title": "aether pricing", "text": "Insurance CEO balked at K2000"}, db)
        read, _ = ot.run_tool("notes_read", {"title": "Aether pricing"}, db)
        assert "balked" in read["body"] and "K2000/mo" in read["body"]
        search, summary = ot.run_tool("notes_search", {"query": "pricing"}, db)
        db.close()
        assert search["results"][0]["title"] == "Aether pricing"

    def test_migration_copies_books_and_bible_once(self):
        from migrations.m012_notes_currency_retrieval import _copy_reading_into_notes
        db = _db()
        db.add(models.Book(title="Zero to One", author="Peter Thiel", status="queue", page=0, total_pages=195))
        db.add(models.DailyBibleLog(date=TODAY, book="Proverbs", chapter=3, notes="trust"))
        db.commit()
        db.close()
        with patch("migrations.m012_notes_currency_retrieval.SessionLocal", TestSessionLocal):
            first = _copy_reading_into_notes()
            second = _copy_reading_into_notes()
        assert first == {"books": 1, "bible_entries": 1}
        assert second == {"books": 0, "bible_entries": 0}
        db = _db()
        assert db.query(models.Note).count() == 2
        assert db.query(models.Book).count() == 1 and db.query(models.DailyBibleLog).count() == 1
        bible = notes.find_note(db, "Bible", "bible")
        db.close()
        assert "Proverbs 3 — trust" in bible.body


# ════════════════════════════════════════════════════════════════
# 6. Meaning-based retrieval
# ════════════════════════════════════════════════════════════════

def _fake_embed(texts, db=None, purpose="x"):
    """Deterministic 4-d vectors: topic words light up a dimension each."""
    topics = ["sprint", "maths", "aether", "bible"]
    out = []
    for t in texts:
        tl = t.lower()
        vec = [1.0 if topic in tl else 0.0 for topic in topics]
        if not any(vec):
            vec = [0.1, 0.1, 0.1, 0.1]
        out.append(vec)
    return out


class TestRetrieval:

    def test_no_key_means_keyword_fallback(self):
        db = _db()
        assert retrieval.search(db, "anything") is None
        assert retrieval.index_items(db, "vault", [(1, 0, "x")])["skipped"] is True
        memory_service.remember(db, "Sprint PB is 11.2 seconds", kind="fact")
        hits = memory_service.recall(db, "sprint personal best")
        db.close()
        assert hits and "11.2" in hits[0].context_data

    def test_embed_only_new_or_changed(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        db = _db()
        with patch("llm.embed_texts", side_effect=_fake_embed) as emb:
            r1 = retrieval.index_items(db, "vault", [(1, 0, "sprint drills plan"), (2, 0, "maths revision")])
            assert r1["embedded"] == 2 and emb.call_count == 1
            # same text, new ids (vault re-index) → pointers move, nothing re-embedded
            r2 = retrieval.index_items(db, "vault", [(10, 0, "sprint drills plan"), (11, 0, "maths revision")])
            assert r2["embedded"] == 0 and r2["reused"] == 2 and emb.call_count == 1
            # one changed, one dropped → one embed, one prune
            r3 = retrieval.index_items(db, "vault", [(10, 0, "sprint drills plan"), (12, 0, "aether pricing")])
            assert r3["embedded"] == 1 and r3["pruned"] == 1 and emb.call_count == 2
        db.commit()
        assert db.query(models.Embedding).count() == 2
        assert {e.source_id for e in db.query(models.Embedding).all()} == {10, 12}
        db.close()

    def test_vault_semantic_search_ranks_by_meaning(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        db = _db()
        for i, text in enumerate(["sprint block: acceleration drills", "maths: integration by parts", "aether client pipeline"]):
            db.add(models.ColdArchiveChunk(file_path=f"n{i}.md", file_title=f"note {i}", content=text, chunk_index=0))
        db.commit()
        with patch("llm.embed_texts", side_effect=_fake_embed):
            retrieval.index_vault(db)
            hits = vault_sync.search_vault("my sprint training", db)
            result, summary = ot.run_tool("query_cold_archive", {"query": "sprint"}, db)
        db.close()
        assert hits[0]["file_title"] == "note 0" and hits[0]["score"] == 1.0
        assert result["mode"] == "semantic" and result["results"][0]["file_title"] == "note 0"

    def test_vault_keyword_when_nothing_indexed(self):
        db = _db()
        db.add(models.ColdArchiveChunk(file_path="a.md", file_title="Aether plan", content="pricing notes", chunk_index=0))
        db.commit()
        result, summary = ot.run_tool("query_cold_archive", {"query": "pricing"}, db)
        db.close()
        assert result["mode"] == "keyword" and result["results"][0]["file_title"] == "Aether plan"

    def test_memory_and_notes_share_retrieval(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        db = _db()
        with patch("llm.embed_texts", side_effect=_fake_embed):
            memory_service.remember(db, "Aether first client is an insurance CEO", kind="fact")
            memory_service.remember(db, "Bible plan: one chapter a day", kind="preference")
            notes.create_note(db, "Maths plan", "general", "integration first")
            notes.create_note(db, "Sprint plan", "general", "acceleration")
            db.commit()
            recalled = memory_service.recall(db, "aether")
            found = notes.search_notes(db, "sprint")
        db.close()
        assert recalled and "insurance CEO" in recalled[0].context_data
        assert found[0]["title"] == "Sprint plan"

    def test_sync_embeds_without_breaking_on_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", str(tmp_path))
        (tmp_path / "a.md").write_text("sprint notes here", encoding="utf-8")
        monkeypatch.setattr(vault_sync.subprocess, "run",
                            lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 0, "", ""))
        db = _db()
        result = vault_sync.sync_vault(db)
        db.close()
        assert result["chunks_created"] == 1
        assert result["embeddings"]["skipped"] is True     # no key: nothing embedded, sync fine


# ════════════════════════════════════════════════════════════════
# 7. Exam dates + prompt
# ════════════════════════════════════════════════════════════════

class TestExamDatesAndPrompt:

    def _seed_subjects(self):
        db = _db()
        db.add(models.Subject(name="Further Maths 9231", code="9231", exam_date=date(2026, 11, 1)))
        db.add(models.Subject(name="Maths 9709", code="9709", exam_date=date(2026, 11, 1)))
        db.commit()
        db.close()

    def test_set_exam_date_tool_per_paper_with_fallback(self):
        self._seed_subjects()
        db = _db()
        result, summary = ot.run_tool("set_exam_date", {"subject": "Further Maths", "paper": "P1", "date": "12 May"}, db)
        assert result["subject_code"] == "9231" and result["paper"] == "P1"
        assert result["exam_date"].endswith("-05-12")
        assert syllabus.get_exam_date(db, "9231", "P1").month == 5
        assert syllabus.get_exam_date(db, "9231", "P3") == date(2026, 11, 1)   # falls back to the subject
        assert syllabus.get_exam_date(db, "9709") == date(2026, 11, 1)
        # upsert, not duplicate
        ot.run_tool("set_exam_date", {"subject": "9231", "paper": "paper 1", "date": "2027-05-13"}, db)
        assert db.query(models.ExamDate).count() == 1
        assert syllabus.get_exam_date(db, "9231", "P1") == date(2027, 5, 13)
        assert db.query(models.ChangeLog).count() == 2
        db.close()

    def test_unknown_subject_is_tool_error(self):
        db = _db()
        result, _ = ot.run_tool("set_exam_date", {"subject": "Latin", "date": "12 May"}, db)
        db.close()
        assert "error" in result

    def test_prompt_no_longer_gates_vault_on_explicit_request(self):
        text = Path(__file__).resolve().parent.parent.parent.joinpath("context", "core", "orchestrator_prompt.md").read_text()
        assert "only read on explicit command" not in text
        assert "NEVER call this tool" not in text
        assert "CONTEXT IS PROACTIVE" in text
        names = {t["function"]["name"] for t in ot.TOOL_SCHEMAS}
        assert {"complete_task", "notes_search", "notes_read", "notes_append", "notes_create",
                "get_deadlines", "set_exam_date"} <= names
