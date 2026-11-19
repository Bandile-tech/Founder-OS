"""
Phase 7, Stage 2 — the personal finance ledger.

Synthetic figures only. The four tests the brief asks for are marked [brief]:
  1. the three-item message, then a single batch undo
  2. a split with a rounding remainder adds up to the exact original amount
  3. income arriving with no rules set
  4. a client payment creates exactly one ledger entry
The rest covers the money rules (no floats, no stored balance), the one short question, the
tools' other paths, the endpoints behind the Budget view, and the agent permission audit.
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, Float, Integer

import change_log
import finance
import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

TODAY = date.today()
SPLIT = [{"bucket": "tithe", "pct": "10"}, {"bucket": "business", "pct": "40"}, {"bucket": "invest", "pct": "20"},
         {"bucket": "save", "pct": "10"}, {"bucket": "personal", "pct": "20"}]
THREE_ITEMS = {"items": [
    {"type": "spent", "amount": "K50", "note": "lunch", "category": "food"},
    {"type": "spent", "amount": "K120", "note": "data", "category": "data"},
    {"type": "received", "amount": "K800", "from": "Bel"},
]}


def _db():
    return TestSessionLocal()


def _tool(name, args):
    """One tool call in a batch of its own, as a turn would make it."""
    db = _db()
    change_log.begin_batch(db, "assistant")
    try:
        result, summary = ot.run_tool(name, args, db)
        changes = change_log.current_batch_changes(db)
    finally:
        change_log.end_batch(db)
        db.close()
    return result, summary, changes


def _turn(text, calls, reply="Done."):
    """A scripted assistant turn that makes these tool calls, all in one message."""
    rounds = [[{"tool_calls": [{"index": i, "id": f"call_{i}", "name": name, "arguments": json.dumps(args)}
                               for i, (name, args) in enumerate(calls)]}, {"finish_reason": "tool_calls"}],
              [{"content": reply}, {"finish_reason": "stop"}]]
    state = {"i": 0}

    def fake(convo, tools):
        i = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        yield from rounds[i]

    with patch("openai_client._stream_completion", fake):
        r = client.post("/orchestrator", json={"text": text, "session_id": "s"})
    assert r.status_code == 200
    return [json.loads(line[5:]) for line in r.text.split("\n") if line.startswith("data:")]


def _rules(rules=SPLIT, **extra):
    result, _summary, _changes = _tool("set_allocation_rules", {"rules": rules, **extra})
    assert "error" not in result, result
    return result


def _balances() -> dict:
    return {b["name"]: b["balance_minor"] for b in client.get("/finance/summary").json()["buckets"]}


def _ledger():
    db = _db()
    try:
        return db.query(models.LedgerTransaction).order_by(models.LedgerTransaction.id).all()
    finally:
        db.close()


def _count(model):
    db = _db()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _client(name="Bel Baby Shop"):
    result, _summary, _changes = _tool("create_client", {"name": name, "owner": "Test Owner"})
    return result["id"]


# ════════════════════════════════════════════════════════════════
# 1. [brief] The three-item message, then a single batch undo
# ════════════════════════════════════════════════════════════════

def test_three_items_are_one_batch_and_one_undo():
    _rules()
    events = _turn("Spent K50 on lunch and K120 on data, got K800 from Bel", [("log_transactions", THREE_ITEMS)])
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["error"] is False
    assert result["summary"] == "Spent K50 on lunch from Personal; Spent K120 on data from Personal; Received K800 from Bel"

    # K800 split 10/40/20/10/20; the two purchases come out of Personal (the default bucket).
    assert _balances() == {"Tithe": 8000, "Business": 32000, "Invest": 16000, "Save": 8000, "Personal": 16000 - 5000 - 12000}
    rows = _ledger()
    assert [(r.direction, r.amount_minor) for r in rows] == [
        ("out", 5000), ("out", 12000), ("in", 8000), ("in", 32000), ("in", 16000), ("in", 8000), ("in", 16000)]
    assert len({r.entry_id for r in rows}) == 3 and len({r.batch_id for r in rows}) == 1

    changes = events[-1]["changes"]
    assert len(changes) == 7 and {c["table"] for c in changes} == {"ledger_transactions"}
    assert {c["batch_id"] for c in changes} == {rows[0].batch_id}

    undone = client.post(f"/changes/batch/{rows[0].batch_id}/undo").json()
    assert undone["undone"] == 7 and "kept" not in undone
    assert _ledger() == [] and set(_balances().values()) == {0}


def test_the_reply_carries_each_touched_balance_in_one_line():
    _rules()
    result, _summary, _changes = _tool("log_transactions", THREE_ITEMS)
    assert result["balances_now"] == {"Personal": "-K10", "Tithe": "K80", "Business": "K320", "Invest": "K160", "Save": "K80"}
    assert result["say"] == "Logged. Personal -K10, Tithe K80, Business K320, Invest K160, Save K80 left."
    assert result["note"] == "Personal is below zero."
    seen = ot.for_model("log_transactions", result)
    assert "ONE line" in seen and "K320" in seen and "batch" not in seen
    # A second purchase names its bucket; only that bucket is reported.
    again, _s, _c = _tool("log_transactions", {"items": [{"type": "spent", "amount": "K20.50", "note": "printing",
                                                           "bucket": "business"}]})
    assert again["balances_now"] == {"Business": "K299.50"}


def test_three_items_with_bel_on_file_is_still_one_batch_and_one_undo():
    _rules()
    bel = _client()
    events = _turn("Spent K50 on lunch and K120 on data, got K800 from Bel", [("log_transactions", THREE_ITEMS)])
    assert next(e for e in events if e["type"] == "tool_result")["error"] is False
    db = _db()
    payment = db.query(models.Payment).one()
    assert (payment.client_id, payment.amount, payment.status) == (bel, 800.0, "paid")
    assert db.query(models.Revenue).count() == 1
    income = db.query(models.LedgerTransaction).filter(models.LedgerTransaction.direction == "in").all()
    assert {r.payment_id for r in income} == {payment.id} and sum(r.amount_minor for r in income) == 80000
    assert {r.income_type for r in income} == {"client"} and len({r.entry_id for r in income}) == 1
    db.close()
    batches = {c["batch_id"] for c in events[-1]["changes"]}
    assert len(batches) == 1
    client.post(f"/changes/batch/{batches.pop()}/undo")
    assert _ledger() == [] and _count(models.Payment) == 0 and _count(models.Revenue) == 0
    assert _count(models.Client) == 1                       # the client was not part of that message


# ════════════════════════════════════════════════════════════════
# 2. [brief] A split with a rounding remainder adds up exactly
# ════════════════════════════════════════════════════════════════

def test_a_split_with_a_remainder_sums_to_the_exact_amount():
    thirds = [{"bucket": "a", "pct": "33.33"}, {"bucket": "b", "pct": "33.33"}, {"bucket": "c", "pct": "33.34"}]
    _rules(thirds, remainder_bucket="b")
    _tool("log_transactions", {"items": [{"type": "received", "amount": "K100.01", "from": "an aunt"}]})
    rows = _ledger()
    assert sum(r.amount_minor for r in rows) == 10001
    # 10001 ngwee: 3333, 3333 and 3334 by the percentages leave 1 over, which goes to B.
    assert _balances() == {"A": 3333, "B": 3334, "C": 3334}
    assert all(isinstance(r.amount_minor, int) for r in rows)


def test_split_is_exact_for_any_amount_and_any_rules():
    for shares in ([(1, 1000), (2, 4000), (3, 2000), (4, 1000), (5, 2000)], [(1, 3333), (2, 3333), (3, 3334)],
                   [(1, 1), (2, 9999)], [(1, 5000), (2, 5000)], [(1, 1250), (2, 8750)], [(7, 10000)]):
        for amount in (1, 2, 3, 7, 99, 100, 101, 999, 1001, 10001, 123457, 99999999, 100000001):
            for remainder in (None, shares[0][0], shares[-1][0], 42):
                parts = finance.split(amount, shares, remainder)
                assert sum(part for _, part in parts) == amount, (amount, shares, remainder)
                assert all(isinstance(part, int) and part > 0 for _, part in parts)
                rounded_down = {bucket: amount * points // 10000 for bucket, points in shares}
                left = amount - sum(rounded_down.values())
                target = remainder if remainder is not None else max(shares, key=lambda s: s[1])[0]
                for bucket, part in parts:
                    assert part == rounded_down.get(bucket, 0) + (left if bucket == target else 0)


def test_the_remainder_bucket_is_the_largest_share_unless_he_names_one():
    result = _rules()
    assert result["remainder_bucket"] == "Business" and result["spending_comes_from"] == "Personal"
    result, _s, _c = _tool("set_allocation_rules", {"remainder_bucket": "savings", "default_bucket": "business"})
    assert result["remainder_bucket"] == "Save" and result["spending_comes_from"] == "Business"
    db = _db()
    assert [b.name for b in db.query(models.Bucket).filter(models.Bucket.is_remainder.is_(True))] == ["Save"]
    assert [b.name for b in db.query(models.Bucket).filter(models.Bucket.is_default.is_(True))] == ["Business"]
    db.close()
    _tool("log_transactions", {"items": [{"type": "received", "amount": "K0.07"}]})
    # 7 ngwee: the shares round down to 0, 2, 1, 0 and 1, and the 3 left over go to Save.
    assert _balances() == {"Tithe": 0, "Business": 2, "Invest": 1, "Save": 3, "Personal": 1}


# ════════════════════════════════════════════════════════════════
# 3. [brief] Income arriving with no rules set
# ════════════════════════════════════════════════════════════════

def test_income_with_no_rules_waits_in_unallocated_and_asks_once():
    first, summary, _c = _tool("log_transactions", {"items": [{"type": "received", "amount": "K800", "from": "an aunt"}]})
    assert summary == "Received K800 from an aunt"
    assert first["balances_now"] == {"Unallocated": "K800"} and "how he wants income split" in first["ask_once"]
    row, = _ledger()
    assert (row.direction, row.amount_minor, row.income_type) == ("in", 80000, "default")
    db = _db()
    assert db.query(models.Bucket).one().is_unallocated is True
    db.close()

    second, _s, _c = _tool("log_transactions", {"items": [{"type": "received", "amount": "K200"}]})
    assert second["balances_now"] == {"Unallocated": "K1,000"} and "ask_once" not in second      # once, not every time

    # Spending before any bucket exists comes out of the same place rather than failing.
    third, _s, _c = _tool("log_transactions", {"items": [{"type": "spent", "amount": "K50", "note": "lunch"}]})
    assert third["balances_now"] == {"Unallocated": "K950"}

    # Setting the rules creates the buckets and offers to split what is waiting.
    rules = _rules()
    assert rules["buckets_created"] == ["Tithe", "Business", "Invest", "Save", "Personal"]
    assert rules["waiting_in_unallocated"] == "K950" and "split_unallocated true" in rules["next_step"]
    assert _balances()["Unallocated"] == 95000

    moved, summary, changes = _tool("set_allocation_rules", {"split_unallocated": True})
    assert moved["moved_from_unallocated"] == ["K950"] and "waiting_in_unallocated" not in moved
    assert _balances() == {"Tithe": 9500, "Business": 38000, "Invest": 19000, "Save": 9500, "Personal": 19000}
    # The move is neither income nor spending: the month's totals are what really came in and went out.
    money, = client.get("/finance/summary").json()["money"]
    assert (money["in_minor"], money["out_minor"], money["net"]) == (100000, 5000, "K950")
    assert [c["category"] for c in client.get("/finance/summary").json()["by_category"]] == ["other"]
    client.post(f"/changes/batch/{changes[0]['batch_id']}/undo")
    assert _balances()["Unallocated"] == 95000 and _balances()["Business"] == 0


def test_rules_per_income_type_fall_back_to_the_default_rules():
    _rules()
    _rules([{"bucket": "business", "pct": "100"}], income_type="client")
    _tool("log_transactions", {"items": [{"type": "received", "amount": "K100", "income_type": "client"},
                                         {"type": "received", "amount": "K100", "income_type": "gift"}]})
    assert _balances() == {"Tithe": 1000, "Business": 10000 + 4000, "Invest": 2000, "Save": 1000, "Personal": 2000}


# ════════════════════════════════════════════════════════════════
# 4. [brief] A client payment creates exactly one ledger entry
# ════════════════════════════════════════════════════════════════

def _income_entries():
    rows = [r for r in _ledger() if r.direction == "in"]
    return {r.entry_id for r in rows}, rows


def test_a_client_payment_recorded_in_the_crm_writes_one_linked_entry_in_the_same_batch():
    _rules()
    _client()
    result, _summary, changes = _tool("record_payment", {"client": "Bel", "amount": 500})
    entries, rows = _income_entries()
    assert len(entries) == 1 and sum(r.amount_minor for r in rows) == 50000
    assert {r.payment_id for r in rows} == {result["id"]} and {r.revenue_id for r in rows} == {result["revenue_id"]}
    assert {c["batch_id"] for c in changes} == {rows[0].batch_id}            # the same batch as the payment
    assert _count(models.Payment) == 1 and _count(models.Revenue) == 1


@pytest.mark.parametrize("calls", [
    [("log_transactions", {"items": [{"type": "received", "amount": "K500", "from": "Bel"}]})],
    [("record_payment", {"client": "Bel", "amount": 500}),
     ("log_transactions", {"items": [{"type": "received", "amount": "K500", "from": "Bel"}]})],
    [("log_transactions", {"items": [{"type": "received", "amount": "K500", "from": "Bel"}]}),
     ("record_payment", {"client": "Bel", "amount": 500})],
    [("route_brain_dump", {"text": "got K500 from Bel"}),
     ("log_transactions", {"items": [{"type": "received", "amount": "K500", "from": "Bel"}]})],
], ids=["ledger tool", "payment then ledger", "ledger then payment", "brain dump then ledger"])
def test_got_K500_from_Bel_is_one_record_of_the_money_however_it_is_said(calls):
    _client()
    parsed = {"summary": "x", "revenue_updates": [{"amount": 500, "currency": "ZMW", "source": "Bel", "client": "Bel"}]}
    with patch("main.get_parse_response", return_value=parsed):
        events = _turn("Got K500 from Bel", calls)
    assert not [e for e in events if e["type"] == "tool_result" and e["error"]]
    entries, rows = _income_entries()
    assert len(entries) == 1 and sum(r.amount_minor for r in rows) == 50000
    assert _count(models.Payment) == 1 and {r.payment_id for r in rows} == {_ledger()[0].payment_id} != {None}
    assert _balances() == {"Unallocated": 50000}
    # And it is undone as one.
    client.post(f"/changes/batch/{events[-1]['changes'][0]['batch_id']}/undo")
    assert _ledger() == [] and _count(models.Payment) == 0


def test_a_tap_on_paid_settles_the_invoice_and_writes_one_entry():
    _client()
    due, _s, _c = _tool("record_payment", {"client": "Bel", "amount": 1250, "status": "due"})
    assert _ledger() == []                                                     # money owed is not money received
    paid = client.post(f"/payments/{due['id']}/paid").json()
    entries, rows = _income_entries()
    assert len(entries) == 1 and rows[0].payment_id == due["id"] and rows[0].amount_minor == 125000
    client.post(f"/payments/{due['id']}/paid")                                 # tapping again changes nothing
    assert len(_income_entries()[1]) == 1
    # "Got K1,250 from Bel" later the same day settles nothing new and writes nothing new.
    assert paid["status"] == "paid" and _count(models.Payment) == 1


def test_money_said_to_the_ledger_settles_an_open_invoice_instead_of_adding_a_payment():
    _client()
    due, _s, _c = _tool("record_payment", {"client": "Bel", "amount": 450, "status": "due"})
    result, summary, _c = _tool("log_transactions", {"items": [{"type": "received", "amount": "K450", "from": "Bel"}]})
    assert "settling what was due" in summary and _count(models.Payment) == 1
    entries, rows = _income_entries()
    assert len(entries) == 1 and rows[0].payment_id == due["id"]


def test_undoing_only_the_payment_takes_its_income_out_of_the_ledger():
    _client()
    _result, _summary, changes = _tool("record_payment", {"client": "Bel", "amount": 500})
    assert _balances() == {"Unallocated": 50000}
    paid_change = next(c for c in changes if c["table"] == "payments" and c["action"] == "update")
    assert client.post(f"/changes/{paid_change['id']}/undo").status_code == 200
    assert _ledger() == []


def test_revenue_from_a_brain_dump_is_ledger_income_and_reconciling_it_links_not_copies():
    _client()
    _tool("record_payment", {"client": "Bel", "amount": 300, "status": "due"})
    parsed = {"summary": "x", "revenue_updates": [{"amount": 300, "currency": "ZMW", "source": "Bel", "client": "Bel"}]}
    with patch("main.get_parse_response", return_value=parsed):
        _tool("route_brain_dump", {"text": "got K300 from Bel"})
    entries, rows = _income_entries()
    assert len(entries) == 1 and rows[0].revenue_id is not None and rows[0].payment_id is None
    # The housekeeping agent ties the revenue to the open invoice. It may write the ledger.
    import agents
    from agents import runner
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        out = runner.run_agent(agents.REGISTRY["crm_housekeeper"], "manual")
    assert out["status"] == "completed", out
    entries, rows = _income_entries()
    assert len(entries) == 1 and rows[0].payment_id is not None and _balances() == {"Unallocated": 30000}


def test_business_income_from_someone_not_on_file_still_reaches_the_revenue_scoreboard():
    result, _summary, changes = _tool("log_transactions", {"items": [
        {"type": "received", "amount": "K250", "from": "a walk-in customer", "income_type": "business"},
        {"type": "received", "amount": "K100", "from": "my uncle", "income_type": "gift"}]})
    db = _db()
    revenue = db.query(models.Revenue).one()
    assert (revenue.amount, revenue.currency, revenue.source) == (250.0, "ZMW", "a walk-in customer")
    linked = db.query(models.LedgerTransaction).filter(models.LedgerTransaction.revenue_id == revenue.id).one()
    assert linked.amount_minor == 25000 and db.query(models.Payment).count() == 0
    db.close()
    client.post(f"/changes/batch/{changes[0]['batch_id']}/undo")
    assert _ledger() == [] and _count(models.Revenue) == 0


# ════════════════════════════════════════════════════════════════
# The money rules
# ════════════════════════════════════════════════════════════════

def test_no_float_and_no_stored_balance_in_the_money_path():
    ledger, bucket, rule = models.LedgerTransaction.__table__, models.Bucket.__table__, models.AllocationRule.__table__
    assert isinstance(ledger.c.amount_minor.type, BigInteger) and isinstance(rule.c.pct.type, Integer)
    for table in (ledger, bucket, rule):
        assert not [c.name for c in table.columns if isinstance(c.type, Float)]
    assert not [c.name for c in bucket.columns if "balance" in c.name or "total" in c.name]


@pytest.mark.parametrize("said, minor, currency", [
    ("K50", 5000, "ZMW"), ("k 1,250.50", 125050, "ZMW"), ("800 kwacha", 80000, "ZMW"), ("ZMW 12", 1200, "ZMW"),
    ("$20", 2000, "USD"), ("20 USD", 2000, "USD"), ("0.30", 30, "ZMW"), (".5", 50, "ZMW"), (50, 5000, "ZMW"),
    (0.1, 10, "ZMW"), (1250.5, 125050, "ZMW"), ("1000000", 100000000, "ZMW"),
])
def test_amounts_are_read_exactly(said, minor, currency):
    assert finance.parse_money(said) == (minor, currency)


@pytest.mark.parametrize("said", ["", None, "lots", "K0", "-50", "K12.345", "5k", "K1e3", True, "12,3.4.5"])
def test_amounts_that_cannot_be_right_are_refused(said):
    with pytest.raises(ValueError):
        finance.parse_money(said)


def test_tenths_do_not_drift():
    _rules([{"bucket": "personal", "pct": "100"}])
    _tool("log_transactions", {"items": [{"type": "received", "amount": "0.10"}] * 3})
    _tool("log_transactions", {"items": [{"type": "spent", "amount": "0.30"}]})
    assert _balances() == {"Personal": 0}                    # 0.1 + 0.1 + 0.1 - 0.3 is exactly nothing
    assert finance.fmt(125050) == "K1,250.50" and finance.fmt(-2000, "USD") == "-$20" and finance.fmt(5) == "K0.05"


def test_a_balance_is_always_the_sum_of_the_transactions():
    _rules()
    _tool("log_transactions", THREE_ITEMS)
    db = _db()
    row = db.query(models.LedgerTransaction).filter(models.LedgerTransaction.direction == "out").first()
    row.amount_minor = 100                                   # change a row behind the app's back
    db.commit()
    db.close()
    assert _balances()["Personal"] == 16000 - 100 - 12000    # the balance follows; there is nothing stale to fix


# ════════════════════════════════════════════════════════════════
# One short question, and nothing written
# ════════════════════════════════════════════════════════════════

def test_an_unclear_bucket_asks_one_question_and_writes_nothing():
    _rules([{"bucket": "business", "pct": "50"}, {"bucket": "school", "pct": "50"}])     # no default to fall back on
    result, summary, changes = _tool("log_transactions", {"items": [
        {"type": "received", "amount": "K100"}, {"type": "spent", "amount": "K50", "note": "lunch"}]})
    assert result["question"] == "The K50 for lunch: which bucket did it come out of: Business, School?"
    assert _ledger() == [] and changes == []                 # the income in the same message waits for the answer too
    seen = ot.for_model("log_transactions", result)
    assert "Nothing was written" in seen and "exactly this question" in seen

    named, _s, _c = _tool("log_transactions", {"items": [
        {"type": "received", "amount": "K100"}, {"type": "spent", "amount": "K50", "note": "lunch", "bucket": "School"}]})
    assert named["balances_now"] == {"Business": "K50", "School": "K0"}
    wrong, _s, _c = _tool("log_transactions", {"items": [{"type": "spent", "amount": "K5", "bucket": "holidays"}]})
    assert wrong["question"] == "The K5: there is no 'holidays' bucket. Which one did it come out of: Business, School?"


def test_a_bad_item_stops_the_whole_list():
    _rules()
    for items in ([{"type": "spent", "amount": "K50"}, {"type": "spent", "amount": "lots"}],
                  [{"type": "spent", "amount": "K50"}, {"amount": "K5"}],
                  [{"type": "spent", "amount": "K50", "date": "the day of the eclipse"}], [], "K50"):
        result, _summary, _changes = _tool("log_transactions", {"items": items})
        assert "error" in result and _ledger() == []


def test_a_date_and_a_named_bucket_are_respected():
    _rules()
    _tool("log_transactions", {"items": [{"type": "spent", "amount": "K30", "note": "taxi", "category": "Transport",
                                          "bucket": "savings", "date": "yesterday"}]})
    row, = _ledger()
    assert row.occurred_at == TODAY - timedelta(days=1) and row.category == "transport"
    assert _balances()["Save"] == -3000


# ════════════════════════════════════════════════════════════════
# Rules
# ════════════════════════════════════════════════════════════════

def test_rules_must_add_up_and_replacing_them_can_be_undone():
    result, _s, _c = _tool("set_allocation_rules", {"rules": SPLIT[:4]})
    assert result["error"] == "that adds up to 80%, not 100%: where does the other 20% go?" and _count(models.Bucket) == 0
    over = SPLIT + [{"bucket": "fun", "pct": "5"}]
    assert "5% over" in _tool("set_allocation_rules", {"rules": over})[0]["error"]
    assert "twice" in _tool("set_allocation_rules", {"rules": [{"bucket": "save", "pct": "50"},
                                                               {"bucket": "savings", "pct": "50"}]})[0]["error"]
    assert "not a usable percentage" in _tool("set_allocation_rules", {"rules": [{"bucket": "a", "pct": "33.333"}]})[0]["error"]

    first = _rules()
    assert first["split"] == ["10% Tithe", "40% Business", "20% Invest", "10% Save", "20% Personal"]
    second, _summary, changes = _tool("set_allocation_rules", {"rules": [
        {"bucket": "Savings", "pct": "62.5"}, {"bucket": "personal", "pct": "37.5"}]})
    assert second["split"] == ["62.5% Save", "37.5% Personal"] and "buckets_created" not in second
    assert _count(models.AllocationRule) == 2 and _count(models.Bucket) == 5     # "Savings" is the Save bucket
    client.post(f"/changes/batch/{changes[0]['batch_id']}/undo")
    db = _db()
    assert [r.pct for r in finance.rules_for(db, None)] == [1000, 4000, 2000, 1000, 2000]
    db.close()


# ════════════════════════════════════════════════════════════════
# Reading: the tool and the endpoints behind the Budget view
# ════════════════════════════════════════════════════════════════

def test_get_finance_views():
    empty, summary, _c = _tool("get_finance", {})
    assert summary == "no buckets yet"
    _rules()
    _tool("log_transactions", THREE_ITEMS)
    _tool("log_transactions", {"items": [{"type": "spent", "amount": "K10", "note": "bread", "category": "food",
                                          "date": (TODAY.replace(day=1) - timedelta(days=1)).isoformat()}]})
    balances, summary, _c = _tool("get_finance", {"view": "balances"})
    assert balances["balances"]["Business"] == "K320" and "Personal -K20" in summary
    spending, summary, _c = _tool("get_finance", {"view": "spending"})
    assert spending["spending_by_category"] == {"data": "K120", "food": "K50"} and spending["total_spent"] == "K170"
    food, _s, _c = _tool("get_finance", {"view": "spending", "category": "Food", "period": "last_month"})
    assert food["spending_by_category"] == {"food": "K10"}
    whole, _s, _c = _tool("get_finance", {"view": "summary"})
    assert whole["money"] == {"in": "K800", "out": "K170", "net": "K630"}
    assert whole["income_split"] == {"default": ["10% Tithe", "40% Business", "20% Invest", "10% Save", "20% Personal"]}
    listed, summary, _c = _tool("get_finance", {"view": "transactions", "bucket": "personal"})
    assert len(listed["transactions"]) == 3 and "spent K50 on lunch" in listed["transactions"][-1]
    assert "error" in _tool("get_finance", {"view": "spending", "period": "the ice age"})[0]
    assert "error" in _tool("get_finance", {"view": "forecast"})[0]


def test_summary_and_transactions_endpoints():
    _rules()
    _tool("log_transactions", THREE_ITEMS)
    body = client.get("/finance/summary").json()
    assert body["period"]["label"] == TODAY.strftime("%B %Y") and body["has_rules"] is True
    personal = next(b for b in body["buckets"] if b["name"] == "Personal")
    assert personal == {"id": personal["id"], "name": "Personal", "currency": "ZMW", "balance_minor": -1000,
                        "balance": "-K10", "is_default": True, "is_remainder": False, "is_unallocated": False}
    assert body["money"] == [{"currency": "ZMW", "in_minor": 80000, "out_minor": 17000, "net_minor": 63000,
                              "in": "K800", "out": "K170", "net": "K630"}]
    assert [(c["category"], c["total"], c["count"]) for c in body["by_category"]] == [("data", "K120", 1), ("food", "K50", 1)]
    assert body["rules"]["default"][1] == {"bucket_id": body["rules"]["default"][1]["bucket_id"], "bucket": "Business",
                                           "pct_bp": 4000, "pct": "40%"}
    assert client.get("/finance/summary?period=nonsense").status_code == 400

    rows = client.get("/finance/transactions").json()
    assert len(rows) == 7 and rows[0]["direction"] == "in" and rows[0]["legs"] == 5 and rows[-1]["note"] == "lunch"
    assert rows[-1]["amount"] == "K50" and rows[-1]["bucket"] == "Personal" and rows[-1]["legs"] == 1
    assert len(client.get("/finance/transactions?direction=out").json()) == 2
    assert len(client.get("/finance/transactions?category=food").json()) == 1
    assert len(client.get(f"/finance/transactions?bucket_id={personal['id']}&limit=2").json()) == 2
    assert client.get("/finance/transactions?period=last_month").json() == []


def test_one_tap_edit_delete_and_undo():
    _rules()
    _tool("log_transactions", THREE_ITEMS)
    rows = client.get("/finance/transactions").json()
    lunch = rows[-1]
    business = next(b for b in client.get("/finance/summary").json()["buckets"] if b["name"] == "Business")

    edited = client.patch(f"/finance/transactions/{lunch['id']}", json={"bucket_id": business["id"], "category": "Client meals"}).json()
    assert edited["transaction"]["bucket"] == "Business" and edited["transaction"]["category"] == "client meals"
    assert edited["change"]["table"] == "ledger_transactions" and _balances()["Business"] == 32000 - 5000
    client.patch(f"/finance/transactions/{lunch['id']}", json={"amount": "K65.50", "note": "lunch with a client"})
    assert _balances()["Business"] == 32000 - 6550
    assert client.post(f"/changes/{edited['change']['id']}/undo").status_code == 200       # one tap back
    assert _balances()["Personal"] == 16000 - 6550 - 12000

    leg = rows[0]                                            # one leg of the split K800
    assert client.patch(f"/finance/transactions/{leg['id']}", json={"amount": "K1"}).status_code == 409
    assert client.patch(f"/finance/transactions/{leg['id']}", json={"bucket_id": 9999}).status_code == 400
    assert client.patch(f"/finance/transactions/{leg['id']}", json={}).status_code == 400
    assert client.patch(f"/finance/transactions/{leg['id']}", json={"category": "transfer"}).status_code == 400
    assert client.patch("/finance/transactions/9999", json={"note": "x"}).status_code == 404

    removed = client.delete(f"/finance/transactions/{leg['id']}").json()
    assert removed["deleted"] == 5 and _balances()["Tithe"] == 0          # the whole income, not one leg of it
    client.post(f"/changes/batch/{removed['changes'][0]['batch_id']}/undo")
    assert _balances()["Tithe"] == 8000

    made = client.post(f"/finance/buckets/{business['id']}/default").json()
    assert made["bucket"]["is_default"] is True and len(made["changes"]) == 2          # Business on, Personal off
    _tool("log_transactions", {"items": [{"type": "spent", "amount": "K1"}]})
    assert _balances()["Business"] == 32000 - 100


def test_a_client_payment_cannot_be_edited_or_removed_from_the_ledger_side():
    _client()
    _tool("record_payment", {"client": "Bel", "amount": 500})
    row = client.get("/finance/transactions").json()[0]
    assert client.patch(f"/finance/transactions/{row['id']}", json={"amount": "K1"}).status_code == 409
    assert client.delete(f"/finance/transactions/{row['id']}").status_code == 409
    assert client.patch(f"/finance/transactions/{row['id']}", json={"note": "deposit"}).status_code == 200


def test_the_budget_view_and_a_ledger_delete_go_through_the_usual_gates():
    result, summary, _c = _tool("show_view", {"view": "budget"})
    assert result["ui_directive"] == {"view": "budget", "params": {"period": "this_month"}} and "budget" in summary
    assert _tool("show_view", {"view": "money"})[0]["view"] == "budget"
    _tool("log_transactions", {"items": [{"type": "spent", "amount": "K50", "note": "lunch"}]})
    row, = _ledger()
    pending, _s, _c = _tool("propose_delete", {"table": "ledger_transactions", "record_id": row.id})
    assert pending["requires_confirmation"] is True and len(_ledger()) == 1          # asked first, nothing removed


def test_usd_stays_usd():
    _rules([{"bucket": "business", "pct": "100"}])
    _tool("log_transactions", {"items": [{"type": "received", "amount": "$50"}, {"type": "received", "amount": "K100"},
                                         {"type": "spent", "amount": "$5", "note": "hosting", "bucket": "business"}]})
    buckets = client.get("/finance/summary").json()["buckets"]
    assert [(b["currency"], b["balance"]) for b in buckets] == [("ZMW", "K100"), ("USD", "$45")]
    balances, _s, _c = _tool("get_finance", {})
    assert balances["balances"] == {"Business (ZMW)": "K100", "Business (USD)": "$45"}
