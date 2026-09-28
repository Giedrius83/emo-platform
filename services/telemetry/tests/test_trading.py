"""The trading pipeline, end to end against a stateful fake eToro.

Every test drives the real Manager/Trader/Broker code; only the HTTP layer and
the clock are fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.trading import guard as guard_mod
from app.trading import quant as quant_mod
from app.trading.broker import ROUTES, Broker
from app.trading.config import REAL_CONFIRMATION, ConfigError, Secret, load_config
from app.trading.db import DayStats, LifecycleError, Store
from app.trading.manager import Manager
from app.trading.market import (
    Quote,
    parse_candles,
    parse_eligibility,
    parse_rates,
    parse_universe,
)
from app.trading.messages import Message, ScoutPayload
from app.trading.__main__ import main as cli
from tests.fake_etoro import BASE, FakeClock, FakeEtoro

FIX = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[3]
API_KEY = "test-api-key-7f3a9c"
USER_KEY = "test-user-key-b81e44"


def run(coro):
    return asyncio.run(coro)


class Rig:
    """A manager wired to a fake eToro and a fake clock."""

    def __init__(self, tmp_path: Path, env: str = "demo", **overrides: str) -> None:
        self.clock = FakeClock()
        self.fake = FakeEtoro(self.clock, env=env)
        settings = {
            "TRADING_ENV": env, "TRADING_PIPELINE_ENABLED": "true", "ETORO_TRADING_API_KEY": API_KEY,
            "ETORO_TRADING_USER_KEY": USER_KEY, "ETORO_API_BASE": BASE, "TRADING_DB_PATH": str(tmp_path / "t.db"),
            "ORDER_CONFIRM_TIMEOUT_SECONDS": "5",
        }
        if env == "real":
            settings["TRADING_REAL_CONFIRM"] = REAL_CONFIRMATION
        settings.update(overrides)
        self.cfg = load_config(settings)
        self.store = Store(self.cfg.db_path)
        self.manager = self._manager()

    def _manager(self) -> Manager:
        broker = Broker(self.cfg.trading_env, self.cfg.api_key, self.cfg.user_key, base_url=BASE,
                        transport=self.fake.transport())
        return Manager(self.cfg, self.store, broker, clock=self.clock.now, sleep=self.clock.sleep)

    def restart(self) -> Manager:
        self.manager = self._manager()
        return self.manager

    def coins(self) -> None:
        self.fake.add_coin(100000, "BTC", 83320.0, 83330.0)
        self.fake.add_coin(100067, "1INCH", 0.09860, 0.09864, trend=0.004)  # altcoin with the strongest trend

    def events(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.store.all("SELECT * FROM pipeline_events WHERE kind = ? ORDER BY id", (kind,))
        return self.store.all("SELECT * FROM pipeline_events ORDER BY id")

    def live(self) -> list[sqlite3.Row]:
        return self.store.live_positions()

    async def start_and_scan(self) -> str:
        assert await self.manager.start()
        return await self.manager.scan_once()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.coins()
    return r


def signal_payload(**kw) -> ScoutPayload:
    base = dict(instrument_id=1, symbol="X", side="long", rank=1, score=1.0, bid=99.95, ask=100.0,
                spread_pct=0.05, quote_ts=0, move_1m=0.1, move_5m=0.5, volatility_1m=0.1, volume=None)
    base.update(kw)
    return ScoutPayload(**base)


# --- 1. the full handoff chain -----------------------------------------------------


def test_signal_flows_scout_quant_guard_trader_to_a_monitored_position(rig: Rig) -> None:
    outcome = run(rig.start_and_scan())
    assert outcome == "EXECUTED"
    chain = [(e["source_bot"], e["kind"]) for e in rig.events() if e["signal_id"]]
    assert chain[:4] == [("SCOUT", "SIGNAL"), ("QUANT", "APPROVED"), ("GUARD", "APPROVED"), ("TRADER", "EXECUTION_SUBMITTED")]
    sids = {e["signal_id"] for e in rig.events() if e["signal_id"]}
    assert len(sids) == 1  # every message carries the same signal_id
    (pos,) = rig.live()
    assert pos["state"] == "MONITORING" and pos["position_id"] in rig.fake.positions
    assert pos["symbol"] == "1INCH"  # an altcoin won on its own ranking, not a BTC/ETH whitelist
    stages = {m["stage"] for m in rig.store.all("SELECT stage FROM execution_metrics")}
    assert {"SCOUT", "QUANT", "GUARD", "TRADER_SUBMIT", "SIGNAL_TO_FILL"} <= stages


def test_every_message_is_strict_json() -> None:
    msg = Message(signal_id="sig_1", source_bot="SCOUT", target_bot="QUANT", status="SIGNAL", payload={"a": 1})
    assert set(json.loads(msg.model_dump_json())) >= {"message_id", "signal_id", "source_bot", "target_bot",
                                                      "timestamp", "status", "payload"}
    with pytest.raises(Exception):
        Message(signal_id="s", source_bot="SCOUT", target_bot="QUANT", status="SIGNAL", extra_field=1)
    with pytest.raises(Exception):
        Message(signal_id="s", source_bot="ORKA", target_bot="QUANT", status="SIGNAL")  # not a pipeline bot


# --- 2. rejections keep scanning; approvals are deterministic -------------------------


def test_no_signal_when_nothing_trends(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.fake.add_coin(100000, "BTC", 83320.0, 83330.0, trend=-0.001)
    assert run(r.start_and_scan()) == "NO_SIGNAL"
    assert r.fake.posts() == []


def test_rejected_candidate_hands_on_to_the_next_ranked_one(rig: Rig) -> None:
    # The best-ranked coin fails Quant (its 5m trend turned down); the next one trades.
    rig.fake.closes_5m[100067] = [1.0, 1.0, 0.2, 0.1]
    outcome = run(rig.start_and_scan())
    assert outcome == "EXECUTED"
    rejected = rig.events("REJECTED")
    assert rejected and "TIMEFRAMES_DISAGREE" in rejected[0]["detail"]
    assert rig.live()[0]["symbol"] == "BTC"


def test_quant_rejects_wide_spread() -> None:
    cfg = load_config({})
    quote = Quote(1, 100.0, 100.5, 0, True)  # 0.5% spread
    status, _, reason = quant_mod.validate(signal_payload(), quote, [], [], cfg, 0)
    assert status == "REJECTED" and reason.startswith("SPREAD")


def test_guard_approval_is_deterministic_and_within_limits() -> None:
    cfg = load_config({})
    from app.trading.market import Account, Eligibility, SideRules
    acct = Account(cash=1000.0, equity=1000.0, invested=0.0, positions={}, pending_orders=0, fetched_ms=0)
    elig = Eligibility(1, "X", True, True, 10.0, long=SideRules((1,), 10.0, 10.0, True))
    qp = quant_mod.QuantPayload(spread_pct=0.05, quote_age_s=0.5, move_1m=0.1, trend_5m=0.2, candle_1m_ts=0,
                                candle_5m_ts=0, volatility_1m=0.25)
    args = dict(signal=signal_payload(), quant=qp, account=acct, eligibility=elig, live_instruments=set(),
                pending_exposure=0.0, stats=DayStats(0.0, 0, 0), halt=guard_mod.Halt(), cfg=cfg, now_ms=0)
    first, second = guard_mod.evaluate(**args), guard_mod.evaluate(**args)
    assert first == second and first[0] == "APPROVED"
    gp = first[1]
    assert gp.amount == 250.0  # 25% of equity
    assert cfg.tp_percent_min <= gp.tp_pct <= cfg.tp_percent_max
    assert cfg.sl_percent_min <= gp.sl_pct <= cfg.sl_percent_max
    assert gp.tp_pct >= gp.sl_pct
    assert gp.broker_sl_pct >= 10.0  # eToro's minimum crypto stop distance is respected


# --- 3. sizing and account limits ------------------------------------------------------


def test_position_size_is_capped_by_cash(rig: Rig) -> None:
    rig.fake.cash = 60.0  # equity 60: 25% is 15, above eToro's $10 minimum
    run(rig.start_and_scan())
    (pos,) = rig.live()
    assert pos["amount"] == 15.0


def test_insufficient_cash_is_rejected_without_an_order(rig: Rig) -> None:
    rig.fake.cash = 5.0  # below eToro's $10 minimum
    run(rig.start_and_scan())
    assert rig.fake.posts() == []
    assert any("INSUFFICIENT_CASH" in e["detail"] for e in rig.events("REJECTED"))


def test_max_position_percent_limits_size(rig: Rig) -> None:
    run(rig.start_and_scan())
    (pos,) = rig.live()
    assert pos["amount"] <= 10_000.0 * 0.25


def test_max_open_positions_blocks_new_entries(tmp_path: Path) -> None:
    r = Rig(tmp_path, MAX_OPEN_POSITIONS="1")
    r.coins()
    assert run(r.start_and_scan()) == "EXECUTED"
    assert run(r.manager.scan_once()) == "BLOCKED"
    assert len(r.fake.posts()) == 1


# --- 4. duplicates ---------------------------------------------------------------------


def test_same_signal_cannot_execute_twice(rig: Rig) -> None:
    run(rig.start_and_scan())
    sid = rig.live()[0]["signal_id"]
    rig.store.set_signal_status(sid, "APPROVED")  # even if someone re-approves it
    gp = guard_mod.GuardPayload(amount=20.0, leverage=1, tp_pct=0.4, sl_pct=0.3, broker_sl_pct=11.0,
                                reference_price=0.09864, equity=1.0, cash=1.0, exposure_after_pct=1.0)
    approval = Message(signal_id=sid, source_bot="GUARD", target_bot="TRADER", status="APPROVED")
    result = run(rig.manager.trader.execute(approval, signal_payload(instrument_id=100067, symbol="1INCH",
                                                                     ask=0.09864, bid=0.0986), gp,
                                            rig.manager.eligibility[100067]))
    assert result.status == "REJECTED" and "DUPLICATE" in result.reason
    assert len(rig.fake.posts()) == 1


def test_database_refuses_a_second_live_position_on_one_instrument(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "d.db"))
    common = dict(instrument_id=7, symbol="X", side="long", amount=10.0, tp_price=1.0, sl_price=1.0,
                  broker_sl_price=1.0, tp_pct=0.4, sl_pct=0.3)
    store.create_open(position_ref="p1", execution_id="e1", request_id="r1", signal_id="s1", **common)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_open(position_ref="p2", execution_id="e2", request_id="r2", signal_id="s2", **common)
    with pytest.raises(LifecycleError):
        store.move("p1", "MONITORING")  # CREATED cannot skip SUBMITTED/OPEN


# --- 5. stale data, freshness, slippage, tradability -----------------------------------


def test_stale_quotes_halt_the_market_scan(rig: Rig) -> None:
    rig.fake.quote_age_ms = 60_000
    assert run(rig.start_and_scan()) == "INVALID"
    assert rig.manager.rt.halt.emergency and "MARKET_DATA_STALE" in rig.manager.rt.emergency_reason
    assert rig.fake.posts() == []


def test_delayed_or_stale_quote_is_rejected_by_quant() -> None:
    cfg = load_config({})
    now = 1_000_000
    delayed = Quote(1, 99.99, 100.0, now, realtime=False)
    assert quant_mod.validate(signal_payload(), delayed, [], [], cfg, now)[2] == "DELAYED_QUOTE"
    old = Quote(1, 99.99, 100.0, now - 60_000, realtime=True)
    assert quant_mod.validate(signal_payload(), old, [], [], cfg, now)[2].startswith("STALE_QUOTE")


def test_stale_candles_are_rejected(rig: Rig) -> None:
    rig.fake.candle_age_ms = 20 * 60_000
    run(rig.start_and_scan())
    assert rig.fake.posts() == []
    assert any("STALE_CANDLES" in e["detail"] for e in rig.events("REJECTED"))


def test_entry_slippage_over_limit_is_rejected(rig: Rig) -> None:
    run(rig.manager.start())
    fetch = rig.manager.trader.quote

    async def moved(instrument_id: int):
        # The price runs 0.3% away between Guard's approval and the Trader's refresh.
        q = await fetch(instrument_id)
        return Quote(q.instrument_id, q.bid * 1.003, q.ask * 1.003, q.ts_ms, True)

    rig.manager.trader.quote = moved
    run(rig.manager.scan_once())
    assert rig.fake.posts() == []
    assert any("SLIPPAGE" in e["detail"] for e in rig.events("REJECTED"))


def test_untradable_instrument_never_reaches_an_order(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.fake.add_coin(100067, "1INCH", 0.09860, 0.09864, trend=0.004)
    r.fake.eligibility_overrides[100067] = {"allowOpenPosition": False}
    r.fake.add_coin(100681, "ZER0", 1.0, 1.0001, trend=0.004, isCurrentlyTradable=False)
    assert run(r.start_and_scan()) == "NO_UNIVERSE"
    assert r.fake.posts() == []


# --- 6. exits ---------------------------------------------------------------------------


def _opened(rig: Rig):
    run(rig.start_and_scan())
    (pos,) = rig.live()
    return pos


def test_software_stop_loss_closes_the_position(rig: Rig) -> None:
    pos = _opened(rig)
    bid, ask = rig.fake.quotes[pos["instrument_id"]]
    rig.fake.quotes[pos["instrument_id"]] = (pos["sl_price"] * 0.999, pos["sl_price"] * 1.0)
    run(rig.manager.monitor_once())
    closed = rig.store.position(pos["position_ref"])
    assert closed["state"] == "CLOSED" and closed["close_reason"] == "SL"
    assert rig.events("SL")


def test_take_profit_by_the_broker_is_recorded_as_tp(rig: Rig) -> None:
    pos = _opened(rig)
    rig.fake.broker_close(pos["position_id"], pos["tp_price"])
    rig.clock.ms += 10_000
    run(_refresh_and_reconcile(rig.manager))
    closed = rig.store.position(pos["position_ref"])
    assert closed["close_reason"] == "TP" and closed["pnl_final"] == 1


async def _refresh_and_reconcile(manager: Manager) -> None:
    await manager.refresh_account()
    await manager.reconcile()


def test_position_is_closed_after_max_hold_seconds(rig: Rig) -> None:
    pos = _opened(rig)
    rig.clock.ms += 899_000
    run(rig.manager.monitor_once())
    assert rig.store.position(pos["position_ref"])["state"] == "MONITORING"
    rig.clock.ms += 2_000
    run(rig.manager.monitor_once())
    closed = rig.store.position(pos["position_ref"])
    assert closed["state"] == "CLOSED" and closed["close_reason"] == "TIMEOUT"
    assert pos["position_id"] not in rig.fake.positions


def test_missing_broker_protection_closes_immediately(rig: Rig) -> None:
    rig.fake.drop_protection = True
    run(rig.start_and_scan())
    rows = rig.store.all("SELECT * FROM positions")
    assert rows[0]["state"] == "CLOSED" and rows[0]["close_reason"] == "SYSTEM_FAILURE"
    assert rig.fake.positions == {}


# --- 7. risk limits ---------------------------------------------------------------------


def _closed_loss(store: Store, n: int, at: int, pnl: float = -1.0) -> None:
    for i in range(n):
        store.conn.execute(
            "INSERT INTO positions (position_ref, instrument_id, symbol, side, amount, entry_price, state, "
            "close_reason, created_at, opened_at, closed_at, pnl) VALUES (?, ?, 'X', 'long', 10, 1, 'CLOSED', 'SL', ?, ?, ?, ?)",
            (f"old{i}", 900 + i, at, at, at + i, pnl))
    store.conn.commit()


def test_daily_loss_limit_halts_entries(rig: Rig) -> None:
    _closed_loss(rig.store, 1, rig.clock.now() - 1000, pnl=-30.0)
    assert run(rig.start_and_scan()) == "BLOCKED"
    assert "DAILY_LOSS_LIMIT" in rig.manager.rt.entry_reason
    assert rig.fake.posts() == []


def test_consecutive_losses_halt_entries(rig: Rig) -> None:
    _closed_loss(rig.store, 3, rig.clock.now() - 10_000, pnl=-0.5)
    assert run(rig.start_and_scan()) == "BLOCKED"
    assert "CONSECUTIVE_LOSSES" in rig.manager.rt.entry_reason


def test_trade_count_per_day_limit() -> None:
    cfg = load_config({"MAX_TRADE_COUNT_PER_DAY": "2"})
    assert "MAX_TRADES_PER_DAY" in guard_mod.entry_halt_reason(DayStats(0.0, 2, 0), cfg)
    assert guard_mod.entry_halt_reason(DayStats(0.0, 1, 0), cfg) == ""


# --- 8. emergency halt ------------------------------------------------------------------


def test_emergency_halt_stops_entries_but_still_manages_positions(rig: Rig) -> None:
    pos = _opened(rig)
    rig.manager.rt.emergency("TEST", "operator drill")
    rig.fake.add_coin(100002, "SOL", 150.0, 150.05, trend=0.01)
    assert run(rig.manager.scan_once()) == "BLOCKED"
    rig.clock.ms += 901_000
    run(rig.manager.monitor_once())
    assert rig.store.position(pos["position_ref"])["close_reason"] == "TIMEOUT"
    assert len(rig.fake.posts()) == 2  # the entry and its timeout close; no new entry


def test_emergency_halt_survives_restart_until_explicit_recovery(rig: Rig, monkeypatch) -> None:
    run(rig.manager.start())
    rig.manager.rt.emergency("TEST", "drill")
    manager = rig.restart()
    assert manager.rt.halt.emergency
    monkeypatch.setenv("TRADING_DB_PATH", rig.cfg.db_path)
    assert cli(["recover", "--reason", "checked the account"]) == 0
    manager.rt.sync_halt()
    assert not manager.rt.halt.emergency


def test_repeated_api_failures_trigger_emergency_halt(rig: Rig) -> None:
    run(rig.manager.start())
    rig.fake.fail_paths["/market-data/rates"] = 503
    for _ in range(rig.cfg.api_failure_halt_count):
        run(rig.manager._guarded(rig.manager.scan_once))
    assert rig.manager.rt.emergency_reason == "API_FAILURES"
    assert rig.events("API_ERROR")


def test_authentication_failure_halts_immediately(rig: Rig) -> None:
    rig.fake.fail_paths["/pnl"] = 401
    assert run(rig.manager.start()) is False
    assert rig.manager.rt.halt.emergency
    assert rig.fake.posts() == []


# --- 9. uncertain submits: look up, never resend ---------------------------------------


def test_submit_timeout_after_acceptance_is_found_by_reference_not_resent(rig: Rig) -> None:
    rig.fake.open_mode = "timeout_after"
    assert run(rig.start_and_scan()) == "EXECUTED"
    assert len(rig.fake.posts()) == 1
    lookups = [c for c in rig.fake.calls if "orders:lookup" in c[1]]
    assert lookups
    (pos,) = rig.live()
    assert pos["position_id"] in rig.fake.positions


def test_submit_timeout_with_no_order_halts_and_never_resends(rig: Rig) -> None:
    rig.fake.open_mode = "timeout_before"
    assert run(rig.start_and_scan()) == "FAILED"
    assert len(rig.fake.posts()) == 1
    assert rig.manager.rt.emergency_reason == "UNCERTAIN_SUBMIT"
    assert rig.store.all("SELECT * FROM positions")[0]["close_reason"] == "SYSTEM_FAILURE"


def test_broker_reject_closes_the_record_as_broker_reject(rig: Rig) -> None:
    rig.fake.open_mode = "reject"
    assert run(rig.start_and_scan()) == "FAILED"
    assert rig.store.all("SELECT * FROM positions")[0]["close_reason"] == "BROKER_REJECT"
    assert not rig.manager.rt.halt.emergency  # a clean refusal is not an outage


# --- 10. restart recovery and reconciliation ---------------------------------------------


def test_restart_resumes_monitoring_an_open_position(rig: Rig) -> None:
    pos = _opened(rig)
    manager = rig.restart()
    assert run(manager.start())
    assert rig.store.position(pos["position_ref"])["state"] == "MONITORING"
    rig.clock.ms += 901_000
    run(manager.monitor_once())
    assert rig.store.position(pos["position_ref"])["close_reason"] == "TIMEOUT"
    assert len(rig.fake.posts()) == 2


def test_restart_recovers_an_order_left_submitted_by_a_crash(rig: Rig) -> None:
    run(rig.manager.start())
    common = dict(instrument_id=100067, symbol="1INCH", side="long", amount=20.0, tp_price=0.099, sl_price=0.098,
                  broker_sl_price=0.088, tp_pct=0.4, sl_pct=0.3)
    rig.store.create_open(position_ref="p1", execution_id="e1", request_id="req-crash", signal_id="s1", **common)
    rig.store.move("p1", "SUBMITTED")
    rig.store.update_order("e1", state="SUBMITTED")
    # eToro had accepted it before the crash:
    rig.fake.open_mode = "fill"
    import httpx
    rig.fake._open(httpx.Request("POST", BASE + "/x", headers={"x-request-id": "req-crash"},
                                 content=json.dumps({"InstrumentID": 100067, "Amount": 20.0, "StopLossRate": 0.088,
                                                     "TakeProfitRate": 0.099})))
    manager = rig.restart()
    assert run(manager.start())
    pos = rig.store.position("p1")
    assert pos["state"] == "MONITORING" and pos["position_id"] in rig.fake.positions


def test_foreign_position_at_the_broker_triggers_emergency_halt(rig: Rig) -> None:
    rig.fake.add_foreign_position(100000)  # e.g. a Grok bot trading the same account
    assert run(rig.manager.start())
    assert rig.manager.rt.emergency_reason == "POSITION_MISMATCH"
    assert run(rig.manager.scan_once()) == "BLOCKED"
    assert rig.fake.posts() == []


# --- 11. DEMO / REAL separation -------------------------------------------------------------


def test_demo_trader_only_ever_calls_demo_order_routes(rig: Rig) -> None:
    pos = _opened(rig)
    rig.clock.ms += 901_000
    run(rig.manager.monitor_once())
    writes = [p for m, p, _ in rig.fake.calls if "/trading/" in p and ("/execution/" in p or "/info/" in p)]
    assert writes and all("/demo" in p for p in writes)
    assert pos["position_id"] not in rig.fake.positions


def test_real_requires_explicit_confirmation_and_real_routes_are_separate() -> None:
    with pytest.raises(ConfigError):
        load_config({"TRADING_ENV": "real"})
    assert load_config({}).trading_env == "demo"  # the default
    assert all("/demo" not in path for path in ROUTES["real"].values())
    assert all("/demo" in path for path in ROUTES["demo"].values())
    broker = Broker("demo", Secret("a"), Secret("b"), base_url=BASE)
    with pytest.raises(TypeError):
        broker._routes["open"] = ROUTES["real"]["open"]  # routes are read-only after construction


def test_key_without_demo_write_scope_refuses_to_start(rig: Rig) -> None:
    rig.fake.scopes = ["etoro-public:real:read", "etoro-public:real:write"]
    assert run(rig.manager.start()) is False
    assert rig.manager.rt.emergency_reason == "ENV_MISMATCH"


# --- 12. secrets ---------------------------------------------------------------------------


def test_keys_never_reach_logs_database_or_dashboard_state(rig: Rig, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    pos = _opened(rig)
    rig.clock.ms += 901_000
    run(rig.manager.monitor_once())
    dump = "\n".join(rig.store.conn.iterdump())
    for secret in (API_KEY, USER_KEY):
        assert secret not in caplog.text
        assert secret not in dump
        assert secret not in repr(rig.cfg) and secret not in json.dumps(rig.cfg.public(), default=str)
    assert "***" in repr(rig.manager.broker)
    assert pos is not None


def test_no_credentials_in_frontend_bundle_or_tracked_files() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    pattern = re.compile(r"(x-user-key|x-api-key)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}")
    for rel in tracked:
        path = REPO / rel
        if path.suffix in {".png", ".jpg", ".ico", ".woff", ".woff2"} or not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        assert not pattern.search(text), rel
    bundle = "".join(p.read_text(errors="ignore") for p in (REPO / "deploy" / "web").rglob("*.js"))
    for word in ("ETORO_API_KEY", "ETORO_USER_KEY", "ETORO_TRADING", "x-user-key", "x-api-key"):
        assert word not in bundle


# --- 13. discovery, parsing of live payloads ------------------------------------------------


def test_dynamic_discovery_from_the_live_catalogue_shape() -> None:
    rows = json.loads((FIX / "etoro_search_crypto.json").read_text())["items"]
    rows.append({"instrumentId": 1001, "internalAssetClassId": 5, "internalSymbolFull": "AAPL",
                 "isCurrentlyTradable": True, "isBuyEnabled": True, "isDelisted": False, "isHiddenFromClient": False})
    universe = parse_universe(rows)
    symbols = {i.symbol for i in universe.values()}
    assert {"0G", "1INCH", "2Z"} <= symbols  # coins beyond BTC/ETH
    assert "00" not in symbols  # not currently tradable
    assert "AGBP" not in symbols  # buying disabled
    assert "AAPL" not in symbols  # not crypto


def test_live_rates_candles_and_eligibility_parse() -> None:
    quotes = parse_rates(json.loads((FIX / "etoro_rates.json").read_text()))
    btc = quotes[100000]
    assert btc.realtime and 0.01 < btc.spread_pct < 0.02
    candles = parse_candles(json.loads((FIX / "etoro_candles.json").read_text()), 100000)
    assert [c.ts_ms for c in candles] == sorted(c.ts_ms for c in candles)
    assert all(c.volume is None for c in candles)  # eToro gives no crypto volume: reported, not invented
    elig = parse_eligibility(json.loads((FIX / "etoro_eligibility.json").read_text()))[100000]
    assert elig.allow_open and elig.long.leverages == (1,) and elig.long.min_sl_pct == 10.0


# --- 14. static checks: one execution path, read-only dashboard -----------------------------


def test_only_broker_module_holds_order_routes() -> None:
    app_dir = REPO / "services" / "telemetry" / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        if re.search(r"/trading/execution|market-open-orders|market-close-orders", text) and path.name != "broker.py":
            offenders.append(path.name)
    assert offenders == []
    trading = [p for p in (app_dir / "trading").glob("*.py")]
    callers = [p.name for p in trading if re.search(r"\.open_by_amount\(|\.close_position\(", p.read_text())]
    assert callers == ["trader.py"]


def test_dashboard_has_no_trading_controls_or_write_routes() -> None:
    src = REPO / "apps" / "trading-terminal" / "src"
    text = " ".join(p.read_text() for p in src.rglob("*.js*"))
    labels = " ".join(re.findall(r"<button[^>]*>(.*?)</button>", text, re.I | re.S))
    assert not re.search(r"\b(buy|sell|close|leverage|api key|enable)\b", labels, re.I)
    assert not re.search(r"method:\s*['\"](POST|PUT|PATCH|DELETE)", text)  # the browser only reads
    main_py = (REPO / "services" / "telemetry" / "app" / "main.py").read_text()
    writes = re.findall(r"@app\.(?:post|put|patch|delete)\(\"([^\"]+)", main_py)
    assert writes == ["/api/ingest"]  # bot reports only; nothing reaches the trader
    assert "open_by_amount" not in main_py and "close_position" not in main_py


# --- 15. the dashboard's read-only view ------------------------------------------------------


def test_dashboard_view_shows_the_trade_and_cannot_write(rig: Rig) -> None:
    from app.hub import TelemetryHub
    from app.trading_view import TradingView

    run(rig.start_and_scan())
    rig.manager.publish()
    view = TradingView(rig.cfg.db_path)
    state = view.read(now_ms=rig.clock.now())
    assert state["env"] == "demo" and state["mode"] == "RUNNING"
    assert state["positions"][0]["symbol"] == "1INCH" and state["positions"][0]["tp_price"]
    assert state["scan"]["ranked"] and state["scan"]["ranked"][0]["volume_status"] == "DATA_UNAVAILABLE"
    kinds = [e.kind for e in view.new_activity()]
    assert "SIGNAL" in kinds and "EXECUTION_FILLED" in kinds and "NO_SIGNAL" not in kinds

    conn = sqlite3.connect(f"file:{rig.cfg.db_path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM positions")
    conn.close()

    hub = TelemetryHub(seed=1)
    hub.attach_trading(view)
    hub.poll_trading()
    body = hub.snapshot_frame().model_dump_json()
    assert '"trading"' in body and API_KEY not in body and USER_KEY not in body


def test_dashboard_marks_a_silent_trader_offline(rig: Rig) -> None:
    from app.trading_view import TradingView

    run(rig.manager.start())
    view = TradingView(rig.cfg.db_path)
    assert view.read(now_ms=rig.clock.now() + 5 * 60_000)["mode"] == "SYSTEM_OFFLINE"
    assert TradingView(str(Path(rig.cfg.db_path).with_name("missing.db"))).read()["mode"] == "NOT_CONFIGURED"


def test_position_of_an_in_flight_order_is_not_mistaken_for_a_foreign_one(rig: Rig) -> None:
    run(rig.manager.start())
    rig.fake.add_foreign_position(100000)  # appears at eToro before the trader stored its id
    rig.manager.rt.inflight.add("exe_in_flight")
    run(_refresh_and_reconcile(rig.manager))
    assert not rig.manager.rt.halt.emergency
    rig.manager.rt.inflight.clear()
    run(_refresh_and_reconcile(rig.manager))  # still unexplained once nothing is in flight: halt
    assert rig.manager.rt.emergency_reason == "POSITION_MISMATCH"
