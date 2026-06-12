"""
Session 2 backend tests.

Bug 4 — Book reorder:
  - New book gets position = max + 1
  - Reorder endpoint updates positions
  - Regression: move book 1 up (positions 0,1,2 → 1,0,2)
  - Regression: move book 0 down after above (positions → 1,0,2, same)

Bug 6 — Reading plan CRUD + mark-today:
  - Create plan with new fields
  - GET /reading-plans returns serialised fields
  - PATCH updates fields
  - DELETE soft-archives (is_active=False)
  - mark-today increments current_chapter by daily_target_chapters
"""

import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from tests.conftest import client


# ── Bug 4: book reorder ───────────────────────────────────────

def _add_book(title, author="A"):
    r = client.post("/books", json={"title": title, "author": author, "status": "queue", "page": 0, "total_pages": 0})
    assert r.status_code == 200
    return r.json()


def test_new_book_gets_sequential_position():
    b1 = _add_book("Book One")
    b2 = _add_book("Book Two")
    b3 = _add_book("Book Three")
    assert b1["position"] < b2["position"] < b3["position"]


def test_reorder_updates_positions():
    b1 = _add_book("Alpha")
    b2 = _add_book("Beta")
    b3 = _add_book("Gamma")
    # Reverse the order
    new_order = [b3["id"], b2["id"], b1["id"]]
    r = client.post("/books/reorder", json=new_order)
    assert r.status_code == 200

    books = {b["id"]: b for b in client.get("/books").json()}
    assert books[b3["id"]]["position"] < books[b2["id"]]["position"] < books[b1["id"]]["position"]


def test_reorder_regression_move_up():
    """Seed positions 0,1,2; move book at index 1 up → positions become 1,0,2."""
    b0 = _add_book("R-Zero")
    b1 = _add_book("R-One")
    b2 = _add_book("R-Two")

    # Ensure sequential positions by ordering id list as-is
    client.post("/books/reorder", json=[b0["id"], b1["id"], b2["id"]])

    # Move b1 up (swap b0 and b1)
    client.post("/books/reorder", json=[b1["id"], b0["id"], b2["id"]])

    books = {b["id"]: b for b in client.get("/books").json()}
    assert books[b1["id"]]["position"] < books[b0["id"]]["position"]
    assert books[b0["id"]]["position"] < books[b2["id"]]["position"]


def test_reorder_regression_move_down():
    """After move-up, moving b0 down gives same relative order 1,0,2."""
    b0 = _add_book("D-Zero")
    b1 = _add_book("D-One")
    b2 = _add_book("D-Two")

    client.post("/books/reorder", json=[b1["id"], b0["id"], b2["id"]])

    # b0 is now at index 1; moving it down swaps with b2
    client.post("/books/reorder", json=[b1["id"], b2["id"], b0["id"]])

    books = {b["id"]: b for b in client.get("/books").json()}
    assert books[b1["id"]]["position"] < books[b2["id"]]["position"] < books[b0["id"]]["position"]


# ── Bug 6: reading plan CRUD ──────────────────────────────────

def _create_plan(**kwargs):
    payload = {"name": "Test Plan", "current_book": "Genesis", "current_chapter": 1,
                "daily_target_chapters": 3, **kwargs}
    r = client.post("/reading-plans", json=payload)
    assert r.status_code == 201
    return r.json()


def test_create_reading_plan_returns_all_fields():
    p = _create_plan(notes="Start from scratch", target_completion_date="2026-12-31")
    assert p["name"] == "Test Plan"
    assert p["current_book"] == "Genesis"
    assert p["current_chapter"] == 1
    assert p["daily_target_chapters"] == 3
    assert p["status"] == "active"
    assert p["notes"] == "Start from scratch"
    assert p["target_completion_date"] == "2026-12-31"


def test_list_reading_plans():
    _create_plan()
    plans = client.get("/reading-plans").json()
    assert len(plans) >= 1
    assert "current_book" in plans[0]
    assert "days_remaining" in plans[0]


def test_patch_reading_plan():
    p = _create_plan()
    r = client.patch(f"/reading-plans/{p['id']}", json={"current_book": "Exodus", "current_chapter": 5})
    assert r.status_code == 200
    data = r.json()
    assert data["current_book"] == "Exodus"
    assert data["current_chapter"] == 5


def test_delete_soft_archives():
    p = _create_plan()
    r = client.delete(f"/reading-plans/{p['id']}")
    assert r.status_code == 200
    plans = client.get("/reading-plans").json()
    match = next((x for x in plans if x["id"] == p["id"]), None)
    assert match is not None
    assert match["status"] == "archived"
    assert match["is_active"] is False


def test_mark_today_increments_chapter():
    p = _create_plan()  # daily_target_chapters=3, current_chapter=1
    r = client.post(f"/reading-plans/{p['id']}/mark-today")
    assert r.status_code == 200
    data = r.json()
    assert data["current_chapter"] == 4  # 1 + 3


def test_mark_today_idempotent_increments():
    p = _create_plan(current_chapter=10, daily_target_chapters=2)
    client.post(f"/reading-plans/{p['id']}/mark-today")
    r = client.post(f"/reading-plans/{p['id']}/mark-today")
    assert r.json()["current_chapter"] == 14  # 10 + 2 + 2


def test_mark_today_persists_across_get():
    p = _create_plan(current_chapter=1, daily_target_chapters=5)
    client.post(f"/reading-plans/{p['id']}/mark-today")
    plans = client.get("/reading-plans").json()
    match = next(x for x in plans if x["id"] == p["id"])
    assert match["current_chapter"] == 6
