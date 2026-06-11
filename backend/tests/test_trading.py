"""
pytest suite for Phase 4 — Trading Desk module.

Covers:
  - PropFirmAccount CRUD + soft-delete (status=withdrawn)
  - Peak balance auto-update on PATCH
  - BacktestTrade CRUD
  - GET /trading/gate calculation (count + adherence math)
  - POST /trading/live returns 423 when gate LOCKED (full payload included)
  - POST /trading/live succeeds when gate CLEARED
  - LiveTrade CRUD
  - rule_broken=True requires rule_broken_description
  - GET /trading/stats/backtest correctness
  - GET /trading/stats/live + total P/L
  - GET /trading/scoreboard/monthly filters correctly
  - GET /scoreboard/ten-k combines AI revenue + trading P/L from TEN_K_START_DATE
  - Brain dump creates BacktestTrade
  - Brain dump for live trade refuses when gate LOCKED (gate_locked_warning in response)
  - Brain dump for live trade succeeds when gate CLEARED
  - Brain dump auto-matches PropFirmAccount by case-insensitive name
  - Brain dump skips trade_log entry with missing r_multiple
  - Drawdown calculation accuracy via _serialize_prop_account
  - All existing Phase 1/2/3 tests unaffected (no regression)
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
from main import app, TEN_K_START_DATE
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


def _fake_parse_response(overrides: dict) -> dict:
    base = {
        "summary": "test",
        "kpi_updates": [],
        "todos_add": [],
        "todos_complete": [],
        "roadmap_complete": [],
        "habits_done": [],
        "annual_updates": [],
        "revenue_updates": [],
        "log_entry": "test",
        "advisory": None,
    }
    base.update(overrides)
    return base


def _seed_backtests(n: int, adherence_all: bool = True):
    """Seed n BacktestTrade rows. Used to unlock/lock the gate."""
    db = _db()
    for i in range(n):
        db.add(models.BacktestTrade(
            date=date.today() - timedelta(days=i),
            pair="EURUSD", direction="long",
            r_multiple=1.0, rule_adherence=adherence_all,
            outcome="win",
        ))
    db.commit()
    db.close()


def _seed_account(**kwargs) -> dict:
    defaults = {
        "name": "Blue Guardian Starter A",
        "firm": "Blue Guardian",
        "account_size_usd": 5000,
        "challenge_type": "Instant Funded",
        "starting_balance": 5000,
        "current_balance": 5000,
        "profit_target_pct": 8,
        "max_drawdown_pct": 6,
        "start_date": str(date.today()),
        "status": "active",
    }
    defaults.update(kwargs)
    resp = client.post("/trading/accounts", json=defaults)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ════════════════════════════════════════════════════════════════
# 1. PropFirmAccount CRUD
# ════════════════════════════════════════════════════════════════

class TestPropFirmAccountCRUD:

    def test_create_account(self):
        acct = _seed_account()
        assert acct["name"] == "Blue Guardian Starter A"
        assert acct["status"] == "active"
        assert acct["peak_balance"] == 5000.0

    def test_list_accounts(self):
        _seed_account()
        _seed_account(name="FundedNext $10K", firm="FundedNext", account_size_usd=10000,
                      starting_balance=10000, current_balance=10000)
        resp = client.get("/trading/accounts")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_patch_updates_current_balance(self):
        acct = _seed_account()
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5200.0})
        assert resp.status_code == 200
        assert resp.json()["current_balance"] == 5200.0

    def test_peak_balance_auto_update_when_balance_grows(self):
        acct = _seed_account()
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5400.0})
        assert resp.json()["peak_balance"] == 5400.0

    def test_peak_balance_not_lowered_when_balance_falls(self):
        acct = _seed_account()
        client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5400.0})
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5100.0})
        # peak must still be 5400, not 5100
        assert resp.json()["peak_balance"] == 5400.0

    def test_soft_delete_sets_withdrawn(self):
        acct = _seed_account()
        resp = client.delete(f"/trading/accounts/{acct['id']}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "withdrawn"

        db = _db()
        row = db.query(models.PropFirmAccount).filter(models.PropFirmAccount.id == acct["id"]).first()
        assert row.status == "withdrawn"
        db.close()

    def test_delete_nonexistent_account_404(self):
        assert client.delete("/trading/accounts/9999").status_code == 404

    def test_patch_nonexistent_account_404(self):
        assert client.patch("/trading/accounts/9999", json={"current_balance": 100}).status_code == 404

    def test_drawdown_calculation_in_response(self):
        acct = _seed_account()
        # Push peak to 5400, then drop to 5100 → drawdown = 300/5400 = 5.56%
        client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5400.0})
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5100.0})
        dd = resp.json()["drawdown_from_peak_pct"]
        expected = round((5400 - 5100) / 5400 * 100, 2)
        assert abs(dd - expected) < 0.01

    def test_gain_pct_calculation(self):
        acct = _seed_account()
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5400.0})
        assert resp.json()["gain_pct"] == round((5400 - 5000) / 5000 * 100, 2)

    def test_target_progress_pct(self):
        # profit_target_pct=8 on $5000 → target = $400 profit
        # After gaining $200, target_progress_pct = 50%
        acct = _seed_account()
        resp = client.patch(f"/trading/accounts/{acct['id']}", json={"current_balance": 5200.0})
        assert resp.json()["target_progress_pct"] == 50.0


# ════════════════════════════════════════════════════════════════
# 2. BacktestTrade CRUD
# ════════════════════════════════════════════════════════════════

class TestBacktestTradeCRUD:

    def _bt_payload(self, **kwargs):
        defaults = {
            "date": str(date.today()),
            "pair": "EURUSD",
            "direction": "long",
            "r_multiple": 1.5,
            "rule_adherence": True,
            "outcome": "win",
            "entry_reason": "MSS after London sweep, FVG entry",
        }
        defaults.update(kwargs)
        return defaults

    def test_create_backtest(self):
        resp = client.post("/trading/backtest", json=self._bt_payload())
        assert resp.status_code == 201
        data = resp.json()
        assert data["pair"] == "EURUSD"
        assert data["r_multiple"] == 1.5
        assert data["rule_adherence"] is True
        assert data["outcome"] == "win"

    def test_list_backtests(self):
        client.post("/trading/backtest", json=self._bt_payload())
        client.post("/trading/backtest", json=self._bt_payload(pair="GBPUSD"))
        resp = client.get("/trading/backtest")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_recent_backtests_limit(self):
        for _ in range(5):
            client.post("/trading/backtest", json=self._bt_payload())
        resp = client.get("/trading/backtest/recent?n=3")
        assert len(resp.json()) == 3

    def test_patch_backtest(self):
        r = client.post("/trading/backtest", json=self._bt_payload())
        tid = r.json()["id"]
        resp = client.patch(f"/trading/backtest/{tid}", json={"r_multiple": 2.0, "outcome": "win"})
        assert resp.status_code == 200
        assert resp.json()["r_multiple"] == 2.0

    def test_delete_backtest(self):
        r = client.post("/trading/backtest", json=self._bt_payload())
        tid = r.json()["id"]
        resp = client.delete(f"/trading/backtest/{tid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == tid

    def test_delete_backtest_404(self):
        assert client.delete("/trading/backtest/9999").status_code == 404

    def test_patch_backtest_404(self):
        assert client.patch("/trading/backtest/9999", json={"r_multiple": 1.0}).status_code == 404


# ════════════════════════════════════════════════════════════════
# 3. Gate logic
# ════════════════════════════════════════════════════════════════

class TestGate:

    def test_gate_locked_when_no_backtests(self):
        resp = client.get("/trading/gate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "LOCKED"
        assert data["total_backtests"] == 0
        assert data["adherence_pct"] == 0.0
        assert data["missing"]["trades"] == 50

    def test_gate_locked_when_insufficient_count(self):
        _seed_backtests(30, adherence_all=True)
        data = client.get("/trading/gate").json()
        assert data["status"] == "LOCKED"
        assert data["total_backtests"] == 30
        assert data["missing"]["trades"] == 20

    def test_gate_locked_when_adherence_too_low(self):
        # 50 trades but only 80% adherence
        db = _db()
        for i in range(40):
            db.add(models.BacktestTrade(date=date.today(), pair="EURUSD", direction="long",
                                         r_multiple=1.0, rule_adherence=True, outcome="win"))
        for i in range(10):
            db.add(models.BacktestTrade(date=date.today(), pair="EURUSD", direction="long",
                                         r_multiple=-1.0, rule_adherence=False, outcome="loss"))
        db.commit()
        db.close()
        data = client.get("/trading/gate").json()
        assert data["status"] == "LOCKED"
        assert data["adherence_pct"] == 80.0
        assert data["missing"]["adherence_pct_gap"] == 10.0

    def test_gate_cleared_at_50_trades_90pct_adherence(self):
        _seed_backtests(50, adherence_all=True)
        data = client.get("/trading/gate").json()
        assert data["status"] == "CLEARED"
        assert data["missing"]["trades"] == 0
        assert data["missing"]["adherence_pct_gap"] == 0.0

    def test_gate_adherence_math_exact(self):
        # 45 adherent + 5 non-adherent = 90% exactly
        db = _db()
        for _ in range(45):
            db.add(models.BacktestTrade(date=date.today(), pair="EURUSD", direction="long",
                                         r_multiple=1.0, rule_adherence=True, outcome="win"))
        for _ in range(5):
            db.add(models.BacktestTrade(date=date.today(), pair="EURUSD", direction="short",
                                         r_multiple=-1.0, rule_adherence=False, outcome="loss"))
        db.commit()
        db.close()
        data = client.get("/trading/gate").json()
        assert data["adherence_pct"] == 90.0
        assert data["status"] == "CLEARED"


# ════════════════════════════════════════════════════════════════
# 4. LiveTrade CRUD with gate enforcement
# ════════════════════════════════════════════════════════════════

class TestLiveTradeCRUD:

    def _lt_payload(self, **kwargs):
        defaults = {
            "date": str(date.today()),
            "pair": "EURUSD",
            "direction": "long",
            "r_multiple": 2.0,
            "rule_adherence": True,
            "outcome": "win",
            "risk_pct": 0.5,
            "net_pl_usd": 50.0,
        }
        defaults.update(kwargs)
        return defaults

    def test_post_live_returns_423_when_gate_locked(self):
        # No backtests → gate is LOCKED
        resp = client.post("/trading/live", json=self._lt_payload())
        assert resp.status_code == 423

    def test_423_response_includes_full_gate_payload(self):
        resp = client.post("/trading/live", json=self._lt_payload())
        assert resp.status_code == 423
        detail = resp.json()["detail"]
        assert "gate" in detail
        gate = detail["gate"]
        assert "total_backtests" in gate
        assert "adherence_pct" in gate
        assert "missing" in gate
        assert "trades" in gate["missing"]
        assert "adherence_pct_gap" in gate["missing"]

    def test_post_live_succeeds_when_gate_cleared(self):
        _seed_backtests(50, adherence_all=True)
        resp = client.post("/trading/live", json=self._lt_payload())
        assert resp.status_code == 201
        data = resp.json()
        assert data["pair"] == "EURUSD"
        assert data["net_pl_usd"] == 50.0

    def test_rule_broken_requires_description(self):
        _seed_backtests(50, adherence_all=True)
        resp = client.post("/trading/live", json=self._lt_payload(rule_broken=True))
        assert resp.status_code == 400

    def test_rule_broken_with_description_accepted(self):
        _seed_backtests(50, adherence_all=True)
        resp = client.post("/trading/live", json=self._lt_payload(
            rule_broken=True,
            rule_broken_description="Entered without confirmation candle"
        ))
        assert resp.status_code == 201
        assert resp.json()["rule_broken"] is True

    def test_list_live_trades(self):
        _seed_backtests(50, adherence_all=True)
        client.post("/trading/live", json=self._lt_payload())
        client.post("/trading/live", json=self._lt_payload(pair="GBPUSD"))
        resp = client.get("/trading/live")
        assert len(resp.json()) == 2

    def test_recent_live_trades_limit(self):
        _seed_backtests(50, adherence_all=True)
        for _ in range(5):
            client.post("/trading/live", json=self._lt_payload())
        assert len(client.get("/trading/live/recent?n=3").json()) == 3

    def test_patch_live_trade(self):
        _seed_backtests(50, adherence_all=True)
        r = client.post("/trading/live", json=self._lt_payload())
        tid = r.json()["id"]
        resp = client.patch(f"/trading/live/{tid}", json={"net_pl_usd": 75.0})
        assert resp.status_code == 200
        assert resp.json()["net_pl_usd"] == 75.0

    def test_delete_live_trade(self):
        _seed_backtests(50, adherence_all=True)
        r = client.post("/trading/live", json=self._lt_payload())
        tid = r.json()["id"]
        resp = client.delete(f"/trading/live/{tid}")
        assert resp.json()["deleted"] == tid

    def test_delete_live_trade_404(self):
        assert client.delete("/trading/live/9999").status_code == 404


# ════════════════════════════════════════════════════════════════
# 5. Stats endpoints
# ════════════════════════════════════════════════════════════════

class TestTradingStats:

    def test_backtest_stats_empty(self):
        data = client.get("/trading/stats/backtest").json()
        assert data["total"] == 0
        assert data["win_rate"] == 0.0
        assert data["total_r"] == 0.0

    def test_backtest_stats_correct(self):
        db = _db()
        for _ in range(3):
            db.add(models.BacktestTrade(date=date.today(), pair="EURUSD", direction="long",
                                         r_multiple=2.0, rule_adherence=True, outcome="win"))
        for _ in range(2):
            db.add(models.BacktestTrade(date=date.today(), pair="GBPUSD", direction="short",
                                         r_multiple=-1.0, rule_adherence=False, outcome="loss"))
        db.commit(); db.close()

        data = client.get("/trading/stats/backtest").json()
        assert data["total"] == 5
        assert data["win_rate"] == 60.0
        assert data["total_r"] == round(3 * 2.0 + 2 * -1.0, 2)  # 4.0
        assert data["by_pair"]["EURUSD"] == 3
        assert data["by_pair"]["GBPUSD"] == 2
        assert data["adherence_pct"] == 60.0

    def test_live_stats_includes_pl(self):
        _seed_backtests(50)
        db = _db()
        for pl in [100.0, -50.0, 75.0]:
            db.add(models.LiveTrade(date=date.today(), pair="EURUSD", direction="long",
                                     r_multiple=1.0, rule_adherence=True, outcome="win",
                                     net_pl_usd=pl))
        db.commit(); db.close()
        data = client.get("/trading/stats/live").json()
        assert data["total_pl_usd"] == 125.0


# ════════════════════════════════════════════════════════════════
# 6. Monthly scoreboard
# ════════════════════════════════════════════════════════════════

class TestMonthlyScoreboard:

    def test_monthly_scoreboard_filters_by_month(self):
        db = _db()
        db.add(models.LiveTrade(date=date(2026, 6, 15), pair="EURUSD", direction="long",
                                 r_multiple=1.0, rule_adherence=True, outcome="win",
                                 net_pl_usd=200.0))
        db.add(models.LiveTrade(date=date(2026, 7, 5), pair="EURUSD", direction="long",
                                 r_multiple=1.0, rule_adherence=True, outcome="win",
                                 net_pl_usd=150.0))
        db.commit(); db.close()

        june = client.get("/trading/scoreboard/monthly?month=2026-06").json()
        july = client.get("/trading/scoreboard/monthly?month=2026-07").json()
        assert june["net_pl_usd"] == 200.0
        assert june["trade_count"] == 1
        assert july["net_pl_usd"] == 150.0

    def test_monthly_scoreboard_empty_month(self):
        data = client.get("/trading/scoreboard/monthly?month=2026-06").json()
        assert data["net_pl_usd"] == 0.0
        assert data["trade_count"] == 0

    def test_monthly_scoreboard_invalid_month_422(self):
        assert client.get("/trading/scoreboard/monthly?month=2026-6").status_code == 422


# ════════════════════════════════════════════════════════════════
# 7. Ten-K scoreboard
# ════════════════════════════════════════════════════════════════

class TestTenkScoreboard:

    def test_tenk_zero_when_no_data(self):
        data = client.get("/scoreboard/ten-k").json()
        assert data["target_usd"] == 10000
        assert data["total_progress_usd"] == 0.0
        assert data["ai_revenue_usd"] == 0.0
        assert data["trading_pl_usd"] == 0.0

    def test_tenk_includes_ai_revenue_from_start_date(self):
        db = _db()
        db.add(models.Revenue(amount=500.0, source="Aether", date=TEN_K_START_DATE))
        db.commit(); db.close()

        data = client.get("/scoreboard/ten-k").json()
        assert data["ai_revenue_usd"] == 500.0

    def test_tenk_excludes_ai_revenue_before_start_date(self):
        db = _db()
        db.add(models.Revenue(amount=999.0, source="Old", date=date(2026, 5, 31)))
        db.commit(); db.close()

        data = client.get("/scoreboard/ten-k").json()
        assert data["ai_revenue_usd"] == 0.0

    def test_tenk_includes_trading_pl(self):
        db = _db()
        db.add(models.LiveTrade(date=TEN_K_START_DATE, pair="EURUSD", direction="long",
                                 r_multiple=2.0, rule_adherence=True, outcome="win",
                                 net_pl_usd=300.0))
        db.commit(); db.close()

        data = client.get("/scoreboard/ten-k").json()
        assert data["trading_pl_usd"] == 300.0

    def test_tenk_excludes_trading_pl_before_start_date(self):
        db = _db()
        db.add(models.LiveTrade(date=date(2026, 5, 31), pair="EURUSD", direction="long",
                                 r_multiple=1.0, rule_adherence=True, outcome="win",
                                 net_pl_usd=999.0))
        db.commit(); db.close()

        data = client.get("/scoreboard/ten-k").json()
        assert data["trading_pl_usd"] == 0.0

    def test_tenk_combines_ai_and_trading(self):
        db = _db()
        db.add(models.Revenue(amount=2000.0, source="Aether", date=TEN_K_START_DATE))
        db.add(models.LiveTrade(date=TEN_K_START_DATE, pair="EURUSD", direction="long",
                                 r_multiple=2.0, rule_adherence=True, outcome="win",
                                 net_pl_usd=500.0))
        db.commit(); db.close()

        data = client.get("/scoreboard/ten-k").json()
        assert data["total_progress_usd"] == 2500.0

    def test_tenk_monthly_breakdown_present(self):
        data = client.get("/scoreboard/ten-k").json()
        assert "monthly_breakdown" in data
        assert isinstance(data["monthly_breakdown"], list)
        # Must include June 2026 entry
        months = [m["month"] for m in data["monthly_breakdown"]]
        assert "2026-06" in months

    def test_tenk_days_remaining_non_negative(self):
        data = client.get("/scoreboard/ten-k").json()
        assert data["days_remaining"] >= 0

    def test_tenk_target_date(self):
        data = client.get("/scoreboard/ten-k").json()
        assert data["target_date"] == "2026-09-30"


# ════════════════════════════════════════════════════════════════
# 8. Brain dump trading integration
# ════════════════════════════════════════════════════════════════

class TestParseTradeLogs:

    def test_brain_dump_creates_backtest(self):
        payload = _fake_parse_response({
            "trade_logs": [{
                "type": "backtest",
                "pair": "EURUSD",
                "direction": "long",
                "r_multiple": 2.0,
                "outcome": "win",
                "adherence": True,
                "entry_reason": "London sweep MSS FVG entry",
            }]
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "logged a backtest"})
        assert resp.status_code == 200
        updates = resp.json()["updates"]
        assert len(updates["trades_created"]) == 1
        assert updates["trades_created"][0]["type"] == "backtest"

        db = _db()
        assert db.query(models.BacktestTrade).count() == 1
        bt = db.query(models.BacktestTrade).first()
        assert bt.pair == "EURUSD"
        assert bt.r_multiple == 2.0
        db.close()

    def test_brain_dump_live_trade_blocked_when_gate_locked(self):
        # No backtests → gate LOCKED
        payload = _fake_parse_response({
            "trade_logs": [{
                "type": "live",
                "pair": "EURUSD",
                "direction": "long",
                "r_multiple": 1.5,
                "outcome": "win",
                "adherence": True,
            }]
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "live trade EURUSD"})
        assert resp.status_code == 200
        data = resp.json()
        # Must have gate_locked_warning, status partial
        assert data.get("status") == "partial"
        assert "gate_locked_warning" in data
        assert "GATE LOCKED" in data["gate_locked_warning"]
        # No LiveTrade row must have been created
        db = _db()
        assert db.query(models.LiveTrade).count() == 0
        db.close()

    def test_brain_dump_live_trade_blocked_warning_includes_gate_info(self):
        payload = _fake_parse_response({
            "trade_logs": [{"type": "live", "pair": "EURUSD", "direction": "long",
                            "r_multiple": 1.0, "outcome": "win", "adherence": True}]
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "live trade"})
        updates = resp.json()["updates"]
        assert "trades_blocked" in updates
        blocked = updates["trades_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["gate"]["status"] == "LOCKED"

    def test_brain_dump_live_trade_succeeds_when_gate_cleared(self):
        _seed_backtests(50, adherence_all=True)
        payload = _fake_parse_response({
            "trade_logs": [{"type": "live", "pair": "GBPUSD", "direction": "short",
                            "r_multiple": 1.5, "outcome": "win", "adherence": True,
                            "net_pl_usd": 75.0}]
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "live trade GBPUSD"})
        assert resp.json()["status"] == "ok"
        db = _db()
        assert db.query(models.LiveTrade).count() == 1
        lt = db.query(models.LiveTrade).first()
        assert lt.pair == "GBPUSD"
        db.close()

    def test_brain_dump_live_trade_matches_account_case_insensitively(self):
        _seed_backtests(50, adherence_all=True)
        # Create account with mixed-case name
        db = _db()
        db.add(models.PropFirmAccount(
            name="Blue Guardian Starter A", firm="Blue Guardian",
            account_size_usd=5000, challenge_type="Instant Funded",
            starting_balance=5000, current_balance=5000, peak_balance=5000,
            profit_target_pct=8, max_drawdown_pct=6,
            start_date=date.today(), status="active",
        ))
        db.commit(); db.close()

        payload = _fake_parse_response({
            "trade_logs": [{"type": "live", "pair": "EURUSD", "direction": "long",
                            "r_multiple": 1.0, "outcome": "win", "adherence": True,
                            "account_name": "blue guardian"}]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "live trade via Blue Guardian"})

        db = _db()
        lt = db.query(models.LiveTrade).first()
        assert lt is not None
        assert lt.account_id is not None
        db.close()

    def test_brain_dump_skips_trade_log_without_r_multiple(self):
        payload = _fake_parse_response({
            "trade_logs": [{"type": "backtest", "pair": "EURUSD", "direction": "long",
                            "outcome": "win", "adherence": True}]  # no r_multiple
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "vague trade mention"})
        db = _db()
        assert db.query(models.BacktestTrade).count() == 0
        db.close()

    def test_brain_dump_multiple_trade_logs_in_one_parse(self):
        payload = _fake_parse_response({
            "trade_logs": [
                {"type": "backtest", "pair": "EURUSD", "direction": "long",
                 "r_multiple": 2.0, "outcome": "win", "adherence": True},
                {"type": "backtest", "pair": "GBPUSD", "direction": "short",
                 "r_multiple": -1.0, "outcome": "loss", "adherence": True},
            ]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "two backtests today"})
        db = _db()
        assert db.query(models.BacktestTrade).count() == 2
        db.close()
