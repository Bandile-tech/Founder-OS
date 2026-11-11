"""Phase 5 shared helpers — business_common."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import business_common as bc


def _rows(*names):
    return [SimpleNamespace(id=i + 1, name=n) for i, n in enumerate(names)]


def test_fuzzy_pick_accepts_one_clear_match():
    rows = _rows("Sunrise Pharmacy", "Golden Crust Bakery", "Hope Academy")
    picked, candidates = bc.fuzzy_pick(rows, lambda r: r.name, "the sunrise pharmacy")
    assert picked is rows[0]
    assert candidates[0][1] is rows[0] and candidates[0][0] >= 0.75


def test_fuzzy_pick_is_ambiguous_when_two_are_close():
    rows = _rows("Banda Pharmacy Lusaka", "Banda Pharmacy Kitwe", "Hope Academy")
    picked, candidates = bc.fuzzy_pick(rows, lambda r: r.name, "banda pharmacy")
    assert picked is None
    assert {c[1].name for c in candidates} == {"Banda Pharmacy Lusaka", "Banda Pharmacy Kitwe"}
    assert candidates[0][0] - candidates[1][0] < 0.2


def test_fuzzy_pick_finds_nothing():
    rows = _rows("Sunrise Pharmacy", "Hope Academy")
    assert bc.fuzzy_pick(rows, lambda r: r.name, "quantum logistics") == (None, [])
    assert bc.fuzzy_pick(rows, lambda r: r.name, "") == (None, [])
    assert bc.fuzzy_pick([], lambda r: r.name, "anything") == (None, [])


def test_fuzzy_pick_takes_the_best_of_several_labels():
    rows = [SimpleNamespace(id=1, name="Mercy Banda", company="Sunrise Pharmacy"),
            SimpleNamespace(id=2, name="Ruth Zulu", company="Hope Academy")]
    picked, _ = bc.fuzzy_pick(rows, lambda r: [r.name, r.company], "hope academy")
    assert picked is rows[1]


def test_money_converts_kwacha_at_the_configured_rate(monkeypatch):
    monkeypatch.setenv("ZMW_PER_USD", "25")
    assert bc.money(2500, "K") == {"amount": 2500.0, "currency": "ZMW", "usd": 100.0}
    assert bc.money(40, "usd") == {"amount": 40.0, "currency": "USD", "usd": 40.0}
    assert bc.money(None, None) == {"amount": 0.0, "currency": "USD", "usd": 0.0}


def test_sum_usd_mixes_currencies(monkeypatch):
    monkeypatch.setenv("ZMW_PER_USD", "25")
    rows = [SimpleNamespace(amount=2500, currency="ZMW"), SimpleNamespace(amount=60, currency="USD"),
            SimpleNamespace(amount=None, currency="USD")]
    assert bc.sum_usd(rows) == 160.0
    priced = [SimpleNamespace(value=500, cur="ZMW")]
    assert bc.sum_usd(priced, amount_attr="value", currency_attr="cur") == 20.0


def test_days_since():
    assert bc.days_since(None) is None
    assert bc.days_since(date.today() - timedelta(days=9)) == 9
    assert bc.days_since(datetime.utcnow() - timedelta(days=3, hours=2)) == 3
