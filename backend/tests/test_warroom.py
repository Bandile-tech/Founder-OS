"""
pytest suite for Phase 5 — War Room module.

Covers:
  1. Annual Targets — new schema (numeric + descriptive, no lower_is_better/year/category)
  2. Document CRUD + chunking engine
  3. Non-Negotiables CRUD
  4. Reading Plan + Entries CRUD
  5. /context/core and /context/search endpoints
  6. Book position + is_currently_reading
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


# ════════════════════════════════════════════════════════════════
# 1. Annual Targets — new schema
# ════════════════════════════════════════════════════════════════

class TestAnnualTargetsNewSchema:

    def test_create_numeric_target(self):
        r = client.post("/annual-targets", json={
            "name": "AI revenue",
            "current_value": 0,
            "target_value": 2000,
            "unit": "USD",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "AI revenue"
        assert d["is_numeric"] is True
        assert d["target_value"] == 2000
        assert d["progress_pct"] == 0

    def test_create_descriptive_target(self):
        r = client.post("/annual-targets", json={
            "name": "Hire first VA",
            "display_value": "Not started",
            "is_complete": False,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["is_numeric"] is False
        assert d["display_value"] == "Not started"
        assert d["is_complete"] is False

    def test_patch_numeric_target_progress(self):
        r = client.post("/annual-targets", json={
            "name": "Followers",
            "current_value": 0,
            "target_value": 1000,
            "unit": "followers",
        })
        tid = r.json()["id"]
        r2 = client.patch(f"/annual-targets/{tid}", json={"current_value": 500})
        assert r2.status_code == 200
        assert r2.json()["progress_pct"] == 50

    def test_patch_descriptive_target_complete(self):
        r = client.post("/annual-targets", json={
            "name": "Build website",
            "display_value": "In progress",
        })
        tid = r.json()["id"]
        r2 = client.patch(f"/annual-targets/{tid}", json={"is_complete": True})
        assert r2.status_code == 200
        assert r2.json()["is_complete"] is True
        assert r2.json()["status"] == "done"

    def test_no_lower_is_better_or_year_or_category_in_response(self):
        r = client.post("/annual-targets", json={"name": "Test target", "target_value": 100})
        d = r.json()
        assert "lower_is_better" not in d
        assert "year" not in d
        assert "category" not in d

    def test_list_only_active_targets(self):
        client.post("/annual-targets", json={"name": "Active one", "target_value": 100})
        r2 = client.post("/annual-targets", json={"name": "Inactive one", "target_value": 50, "is_active": False})
        tid = r2.json()["id"]
        r3 = client.get("/annual-targets")
        names = [t["name"] for t in r3.json()]
        assert "Active one" in names
        assert "Inactive one" not in names

    def test_delete_target(self):
        r = client.post("/annual-targets", json={"name": "Delete me", "target_value": 10})
        tid = r.json()["id"]
        assert client.delete(f"/annual-targets/{tid}").status_code == 200
        names = [t["name"] for t in client.get("/annual-targets").json()]
        assert "Delete me" not in names


# ════════════════════════════════════════════════════════════════
# 2. Documents + chunking engine
# ════════════════════════════════════════════════════════════════

class TestDocuments:

    def test_create_document_generates_chunks(self):
        content = " ".join(["word"] * 200)   # 200 words — fits in one chunk
        r = client.post("/documents", json={"title": "Test Doc", "content": content})
        assert r.status_code == 201
        d = r.json()
        assert d["title"] == "Test Doc"
        assert d["chunk_count"] >= 1

    def test_get_document_returns_content_and_chunks(self):
        r = client.post("/documents", json={"title": "Doctrine", "content": "Fear God and keep His commandments."})
        doc_id = r.json()["id"]
        r2 = client.get(f"/documents/{doc_id}")
        assert r2.status_code == 200
        d = r2.json()
        assert d["content"] == "Fear God and keep His commandments."
        assert len(d["chunks"]) >= 1

    def test_update_document_rebuilds_chunks(self):
        r = client.post("/documents", json={"title": "Old", "content": "old content"})
        doc_id = r.json()["id"]
        r2 = client.patch(f"/documents/{doc_id}", json={"content": "new content here"})
        assert r2.status_code == 200
        r3 = client.get(f"/documents/{doc_id}")
        assert r3.json()["content"] == "new content here"

    def test_delete_document(self):
        r = client.post("/documents", json={"title": "Temp", "content": "temp"})
        doc_id = r.json()["id"]
        assert client.delete(f"/documents/{doc_id}").status_code == 200
        assert client.get(f"/documents/{doc_id}").status_code == 404

    def test_list_documents(self):
        client.post("/documents", json={"title": "Doc A", "content": "alpha"})
        client.post("/documents", json={"title": "Doc B", "content": "beta"})
        r = client.get("/documents")
        titles = [d["title"] for d in r.json()]
        assert "Doc A" in titles
        assert "Doc B" in titles

    def test_large_document_produces_multiple_chunks(self):
        content = " ".join([f"word{i}" for i in range(2000)])  # ~12000 chars
        r = client.post("/documents", json={"title": "Big Doc", "content": content})
        assert r.json()["chunk_count"] > 1


# ════════════════════════════════════════════════════════════════
# 3. Non-Negotiables
# ════════════════════════════════════════════════════════════════

class TestNonNegotiables:

    def test_create_non_negotiable(self):
        r = client.post("/non-negotiables", json={"key": "cold_shower", "label": "Cold shower"})
        assert r.status_code == 201
        d = r.json()
        assert d["key"] == "cold_shower"
        assert d["label"] == "Cold shower"

    def test_duplicate_key_rejected(self):
        client.post("/non-negotiables", json={"key": "unique_key", "label": "First"})
        r2 = client.post("/non-negotiables", json={"key": "unique_key", "label": "Duplicate"})
        assert r2.status_code == 400

    def test_patch_label_and_active(self):
        r = client.post("/non-negotiables", json={"key": "nn_patch_test", "label": "Old"})
        nid = r.json()["id"]
        r2 = client.patch(f"/non-negotiables/{nid}", json={"label": "New", "is_active": False})
        assert r2.status_code == 200
        assert r2.json()["label"] == "New"
        assert r2.json()["is_active"] is False

    def test_delete_non_negotiable(self):
        r = client.post("/non-negotiables", json={"key": "delete_nn", "label": "Delete me"})
        nid = r.json()["id"]
        assert client.delete(f"/non-negotiables/{nid}").status_code == 200
        keys = [n["key"] for n in client.get("/non-negotiables").json()]
        assert "delete_nn" not in keys

    def test_list_non_negotiables_ordered(self):
        client.post("/non-negotiables", json={"key": "nn_b", "label": "B", "sort_order": 2})
        client.post("/non-negotiables", json={"key": "nn_a", "label": "A", "sort_order": 1})
        rows = client.get("/non-negotiables").json()
        orders = [r["sort_order"] for r in rows]
        assert orders == sorted(orders)


# ════════════════════════════════════════════════════════════════
# 4. Reading Plan + Entries
# ════════════════════════════════════════════════════════════════

class TestReadingPlan:

    def _create_plan(self, name="Bible 2026"):
        r = client.post("/reading-plans", json={"name": name})
        assert r.status_code == 201
        return r.json()["id"]

    def test_create_reading_plan(self):
        plan_id = self._create_plan()
        r = client.get(f"/reading-plans/{plan_id}/entries")
        assert r.status_code == 200
        assert r.json()["name"] == "Bible 2026"

    def test_add_entries_to_plan(self):
        plan_id = self._create_plan()
        r = client.post(f"/reading-plans/{plan_id}/entries", json={"ref": "Proverbs 1", "day_number": 1})
        assert r.status_code == 201
        assert r.json()["ref"] == "Proverbs 1"

    def test_toggle_entry_done(self):
        plan_id = self._create_plan("Toggle Plan")
        r = client.post(f"/reading-plans/{plan_id}/entries", json={"ref": "Psalm 23", "day_number": 1})
        eid = r.json()["id"]
        r2 = client.patch(f"/reading-plans/entries/{eid}/toggle")
        assert r2.status_code == 200
        assert r2.json()["done"] is True
        # Toggle back
        r3 = client.patch(f"/reading-plans/entries/{eid}/toggle")
        assert r3.json()["done"] is False

    def test_plan_entry_count(self):
        plan_id = self._create_plan("Count Plan")
        for i in range(3):
            client.post(f"/reading-plans/{plan_id}/entries", json={"ref": f"Ref {i}", "day_number": i + 1})
        plans = client.get("/reading-plans").json()
        plan = next((p for p in plans if p["id"] == plan_id), None)
        assert plan["entry_count"] == 3

    def test_done_count_updates(self):
        plan_id = self._create_plan("Done Count Plan")
        r1 = client.post(f"/reading-plans/{plan_id}/entries", json={"ref": "A", "day_number": 1})
        r2 = client.post(f"/reading-plans/{plan_id}/entries", json={"ref": "B", "day_number": 2})
        client.patch(f"/reading-plans/entries/{r1.json()['id']}/toggle")
        plans = client.get("/reading-plans").json()
        plan = next(p for p in plans if p["id"] == plan_id)
        assert plan["done_count"] == 1


# ════════════════════════════════════════════════════════════════
# 5. Context endpoints
# ════════════════════════════════════════════════════════════════

class TestContextEndpoints:

    def test_core_context_returns_structure(self):
        r = client.get("/context/core")
        assert r.status_code == 200
        d = r.json()
        assert "documents" in d
        assert "non_negotiables" in d
        assert "annual_targets" in d

    def test_core_context_includes_active_non_negotiables(self):
        client.post("/non-negotiables", json={"key": "ctx_nn", "label": "Context NN"})
        r = client.get("/context/core")
        keys = [n["key"] for n in r.json()["non_negotiables"]]
        assert "ctx_nn" in keys

    def test_search_finds_document_chunk(self):
        client.post("/documents", json={"title": "Search Doc", "content": "The gates of wisdom are opened by discipline."})
        r = client.get("/context/search?q=wisdom")
        assert r.status_code == 200
        d = r.json()
        assert len(d["results"]) >= 1
        assert any("wisdom" in c["content"].lower() for c in d["results"])

    def test_search_short_query_rejected(self):
        r = client.get("/context/search?q=x")
        assert r.status_code == 400

    def test_search_no_match_returns_empty(self):
        r = client.get("/context/search?q=xyzzyqwerty")
        assert r.status_code == 200
        assert r.json()["results"] == []


# ════════════════════════════════════════════════════════════════
# 6. Books — position + is_currently_reading
# ════════════════════════════════════════════════════════════════

class TestBooksNewFields:

    def _add_book(self, title):
        r = client.post("/books", json={"title": title, "author": "Author", "status": "queue"})
        assert r.status_code == 200
        return r.json()["id"]

    def test_book_has_position_and_currently_reading_fields(self):
        bid = self._add_book("Test Book")
        books = client.get("/books").json()
        b = next(b for b in books if b["id"] == bid)
        assert "position" in b
        assert "is_currently_reading" in b
        assert b["is_currently_reading"] is False

    def test_toggle_currently_reading(self):
        bid = self._add_book("Toggle Book")
        r = client.patch(f"/books/{bid}/currently-reading")
        assert r.status_code == 200
        assert r.json()["is_currently_reading"] is True
        r2 = client.patch(f"/books/{bid}/currently-reading")
        assert r2.json()["is_currently_reading"] is False

    def test_reorder_books(self):
        b1 = self._add_book("Book 1")
        b2 = self._add_book("Book 2")
        b3 = self._add_book("Book 3")
        r = client.post("/books/reorder", json=[b3, b1, b2])
        assert r.status_code == 200
        assert r.json()["reordered"] == 3
        books = client.get("/books").json()
        id_order = [b["id"] for b in books]
        assert id_order.index(b3) < id_order.index(b1) < id_order.index(b2)

    def test_patch_book_position(self):
        bid = self._add_book("Positioned Book")
        r = client.patch(f"/books/{bid}", json={"position": 5})
        assert r.status_code == 200
        assert r.json()["position"] == 5
