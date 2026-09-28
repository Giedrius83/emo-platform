"""MANAGER: runs the pipeline and keeps the books straight.

SCOUT -> QUANT -> GUARD -> TRADER are direct calls, one after another, the
moment the previous stage answers. There is no consensus vote, no waiting
between stages. A REJECTED candidate hands straight on to the next ranked
one. Nothing outside these five (dashboard, reporting, the Grok bots) is on
the path, so nothing outside can delay or block a valid trade.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from . import guard as guard_mod
from . import quant as quant_mod
from .broker import WRITE_SCOPES, AuthError, Broker, BrokerError, BrokerUncertain
from .config import TradingConfig, banner
from .db import Store
from .market import (
    Account,
    DataError,
    Eligibility,
    Instrument,
    parse_account,
    parse_candles,
    parse_eligibility,
    parse_rates,
    parse_universe,
)
from .messages import Message, ScoutPayload, new_signal_id
from .runtime import Runtime, rid
from .scout import RankedAsset, Scout, side_of
from .trader import Trader, broker_position_matches

log = logging.getLogger("trading.manager")

SEARCH_PAGE = 100
ELIGIBILITY_BATCH = 100
RATES_BATCH = 500
MAX_ATTEMPTS_PER_SCAN = 3
ACCOUNT_EVERY_TICKS = 5


class Manager:
    def __init__(self, cfg: TradingConfig, store: Store, broker: Broker, **runtime: Any) -> None:
        if broker.env != cfg.trading_env:
            raise ValueError("broker environment does not match TRADING_ENV")
        self.cfg = cfg
        self.store = store
        self.broker = broker
        self.rt = Runtime(cfg, store, broker, **runtime)
        self.scout = Scout(cfg)
        self.trader = Trader(self.rt)
        self.universe: dict[int, Instrument] = {}
        self.eligibility: dict[int, Eligibility] = {}
        self.universe_at = 0
        self.account: Account | None = None
        self.verified = False
        self.flagged_foreign: set[int] = set()
        self._ticks = 0
        self._last_scan: dict[str, Any] = {}

    # -- startup -----------------------------------------------------------------

    async def start(self) -> bool:
        """Print the environment, prove the keys belong to it, rebuild state. False = do not trade."""
        text = banner(self.cfg)
        log.warning(text)
        print(text, flush=True)
        self.store.set_state("env", self.cfg.trading_env)
        self.rt.event("STARTED", "MANAGER", status="INFO", detail=text)
        self.verified = await self.verify_environment()
        if not self.verified:
            self.publish()
            return False
        await self.refresh_account()
        await self.reconcile()
        await self.refresh_universe()
        self.publish()
        return True

    async def verify_environment(self) -> bool:
        env = self.cfg.trading_env
        try:
            me = await self.rt.call("me", self.broker.me(rid()))
            scopes = set(me.get("scopes") or [])
            if scopes and not scopes & WRITE_SCOPES[env]:
                self.rt.emergency("ENV_MISMATCH", f"trading key has no {env} write scope")
                return False
            if not scopes:
                log.warning("eToro did not list key scopes; %s write access is proven by the first order", env)
            if env == "demo" and scopes & WRITE_SCOPES["real"]:
                log.warning("this key can also trade REAL; the DEMO trader only holds DEMO routes")
            account = parse_account(await self.rt.call("portfolio", self.broker.portfolio(rid())), self.rt.clock())
        except (AuthError, BrokerError, BrokerUncertain, DataError) as exc:
            self.rt.emergency("ENV_VERIFY_FAILED", f"{env}: {exc}")
            return False
        self.account = account
        log.warning("%s account verified: cash %.2f equity %.2f positions %d",
                    env.upper(), account.cash, account.equity, len(account.positions))
        return True

    # -- data --------------------------------------------------------------------

    async def refresh_account(self) -> Account | None:
        try:
            self.account = await self.trader.account()
        except (BrokerUncertain, BrokerError, DataError) as exc:
            log.warning("account refresh failed: %s", exc)
            return None
        return self.account

    async def refresh_universe(self) -> None:
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            payload = await self.rt.call("search", self.broker.search_crypto(rid(), page, SEARCH_PAGE))
            items = (payload or {}).get("items") or []
            rows.extend(items)
            total = int((payload or {}).get("totalItems") or 0)
            if not items or len(rows) >= total:
                break
            page += 1
        found = parse_universe(rows)
        eligibility: dict[int, Eligibility] = {}
        ids = sorted(found)
        for i in range(0, len(ids), ELIGIBILITY_BATCH):
            payload = await self.rt.call("eligibility", self.broker.eligibility(rid(), ids[i:i + ELIGIBILITY_BATCH]))
            eligibility.update(parse_eligibility(payload))

        def usable(e: Eligibility | None) -> bool:
            rules = e.rules("long") if e else None
            return bool(e and e.allow_open and e.allow_close and rules and self.cfg.leverage in rules.leverages)

        universe = {iid: inst for iid, inst in found.items() if usable(eligibility.get(iid))}
        if not universe:
            self.store.risk_event("UNIVERSE_EMPTY", "ERROR", f"{len(rows)} rows, {len(found)} tradable, 0 eligible")
            return
        self.universe, self.eligibility = universe, eligibility
        self.universe_at = self.rt.clock()
        log.info("crypto universe: %d catalogue rows, %d tradable, %d eligible", len(rows), len(found), len(universe))

    async def quotes(self, ids: list[int]) -> dict[int, Any]:
        out: dict[int, Any] = {}
        for i in range(0, len(ids), RATES_BATCH):
            out.update(parse_rates(await self.rt.call("rates", self.broker.rates(rid(), ids[i:i + RATES_BATCH]))))
        return out

    async def candles(self, instrument_id: int, interval: str, count: int) -> list:
        payload = await self.rt.call("candles", self.broker.candles(rid(), instrument_id, interval, count))
        return parse_candles(payload, instrument_id)

    # -- reconciliation ------------------------------------------------------------

    async def reconcile(self) -> None:
        """Make the database agree with eToro. Anything unexplained halts entries."""
        account = self.account
        if account is None:
            return
        for order in self.store.unresolved_orders():
            if order["execution_id"] in self.rt.inflight:
                continue
            await self._resolve_order(order, account)

        ours = {row["position_id"] for row in self.store.all("SELECT position_id FROM positions WHERE position_id IS NOT NULL")}
        # While an order is in flight eToro can show its position before we have
        # stored the id; judge ownership on the next pass instead.
        foreign_check = not self.rt.inflight
        for pid, bp in account.positions.items():
            if not foreign_check:
                break
            if pid not in ours and pid not in self.flagged_foreign:
                self.flagged_foreign.add(pid)
                self.rt.emergency("POSITION_MISMATCH",
                                  f"eToro position {pid} (instrument {bp.instrument_id}) is not in the trader's records")
        for pos in self.store.live_positions():
            bp = account.positions.get(pos["position_id"]) if pos["position_id"] else None
            if bp is not None and not broker_position_matches(pos, bp):
                self.rt.emergency("POSITION_MISMATCH", f"position {pos['position_id']} differs from the record")
        await self.trader.monitor(account, {})
        await self.trader.refine_pnl()

    async def _resolve_order(self, order: Any, account: Account) -> None:
        pos = self.store.position(order["position_ref"])
        try:
            info = await self.rt.call("lookup", self.broker.lookup_by_reference(rid(), order["request_id"]))
        except (BrokerUncertain, BrokerError):
            return
        now = self.rt.clock()
        if info is None:
            if (now - order["created_at"]) / 1000 < self.cfg.order_confirm_timeout_seconds:
                return
            self.store.update_order(order["execution_id"], state="REJECTED", error="not found at eToro", resolved_at=now)
            if order["action"] == "open" and pos is not None and pos["state"] in ("CREATED", "SUBMITTED"):
                self.store.move(pos["position_ref"], "CLOSED", close_reason="SYSTEM_FAILURE", closed_at=now)
            return
        status = (info.get("status") or {}).get("id")
        executions = [pe for pe in info.get("positionExecutions") or [] if pe.get("positionId")]
        if order["action"] == "open" and pos is not None and pos["state"] in ("CREATED", "SUBMITTED"):
            if executions:
                pe = executions[0]
                opening = pe.get("openingData") or {}
                entry = float(opening.get("avgPrice") or pos["tp_price"] / (1 + pos["tp_pct"] / 100))
                opened = self.rt.clock()
                self.store.update_order(order["execution_id"], state="FILLED", order_id=info.get("orderId"), resolved_at=now)
                if pos["state"] == "CREATED":
                    self.store.move(pos["position_ref"], "SUBMITTED")
                self.store.move(pos["position_ref"], "OPEN", position_id=int(pe["positionId"]), entry_price=entry,
                                units=float(opening.get("units") or pe.get("remainingUnits") or 0.0),
                                sl_price=entry * (1 - (1 if pos["side"] == "long" else -1) * pos["sl_pct"] / 100),
                                opened_at=opened, deadline_at=opened + self.cfg.max_hold_seconds * 1000)
                self.rt.event("POSITION_OPENED", "MANAGER", status="RECONCILED", signal_id=pos["signal_id"],
                              detail=f"{pos['symbol']} position {pe['positionId']} recovered from eToro")
            elif status in {4, 7, 8, 10}:
                self.store.update_order(order["execution_id"], state="REJECTED", order_id=info.get("orderId"), resolved_at=now)
                self.store.move(pos["position_ref"], "CLOSED", close_reason="BROKER_REJECT", closed_at=now)
        elif order["action"] == "close" and pos is not None:
            if pos["position_id"] not in account.positions:
                self.store.update_order(order["execution_id"], state="FILLED", resolved_at=now)
            elif status in {4, 7, 8, 10}:
                self.store.update_order(order["execution_id"], state="REJECTED", resolved_at=now)

    # -- the pipeline --------------------------------------------------------------

    def _held(self) -> set[int]:
        held = {p["instrument_id"] for p in self.store.live_positions()}
        if self.account:
            held |= {bp.instrument_id for bp in self.account.positions.values()}
        return held

    async def scan_once(self) -> str:
        self.rt.sync_halt()
        started = self.rt.clock()
        self.rt.heartbeat("MANAGER")
        if not self.verified:
            return "NOT_VERIFIED"
        if not self.universe or started - self.universe_at > self.cfg.universe_refresh_seconds * 1000:
            await self.refresh_universe()
        if not self.universe:
            return "NO_UNIVERSE"
        await self.refresh_account()
        await self.reconcile()
        stats = self.store.day_stats(started)
        self.rt.set_entry_halt(guard_mod.entry_halt_reason(stats, self.cfg))

        quotes = await self.quotes(sorted(self.universe))
        self.rt.heartbeat("SCOUT")
        now = self.rt.clock()
        if not quotes:
            self.rt.invalid("NO_QUOTES", "rates returned nothing for the universe")
            return "INVALID"
        freshest = max(q.ts_ms for q in quotes.values())
        if (now - freshest) / 1000 > self.cfg.max_market_data_age_seconds:
            self.rt.emergency("MARKET_DATA_STALE", f"newest quote is {(now - freshest) / 1000:.0f}s old")
            return "INVALID"
        if (freshest - now) / 1000 > 5:
            self.rt.emergency("CLOCK_SKEW", f"quotes are {(freshest - now) / 1000:.0f}s in the future")
            return "INVALID"

        candidates, rows = self.scout.prefilter(self.universe, quotes, now, self._held())
        open_count = len(self._held())
        blocked = self.rt.halt.emergency or self.rt.halt.entry_halted or open_count >= self.cfg.max_open_positions
        if blocked:
            self._publish_assets(rows, started, "ENTRIES_BLOCKED")
            return "BLOCKED"

        scored: list[RankedAsset] = []
        for row in candidates:
            try:
                scored.append(self.scout.score(row, await self.candles(row.instrument_id, "OneMinute", 30)))
            except DataError as exc:
                row.status = "NO_DATA"
                self.rt.invalid("CANDLES_CORRUPT", f"{row.symbol}: {exc}")
            except (BrokerUncertain, BrokerError):
                row.status = "NO_DATA"
        signals = self.scout.signals(scored)
        self.store.metric(None, "SCOUT", started, self.rt.clock())
        self._publish_assets(rows, started, "SCANNED")
        if not signals:
            self.rt.event("NO_SIGNAL", "SCOUT", target="MANAGER", status="NO_SIGNAL",
                          detail=f"{len(quotes)} quotes, {len(candidates)} checked, none qualified")
            return "NO_SIGNAL"

        outcome = "REJECTED"
        for row in signals[:MAX_ATTEMPTS_PER_SCAN]:
            outcome = await self.run_signal(row)
            if outcome != "REJECTED":
                break  # EXECUTED, or a block/failure that stops this scan
        return outcome

    async def run_signal(self, row: RankedAsset) -> str:
        sid = new_signal_id()
        side = side_of(row)
        signal = ScoutPayload(
            instrument_id=row.instrument_id, symbol=row.symbol, side=side, rank=1, score=row.score or 0.0,
            bid=row.bid, ask=row.ask, spread_pct=row.spread_pct, quote_ts=row.quote_ts,
            move_1m=row.move_1m or 0.0, move_5m=row.move_5m or 0.0, volatility_1m=row.volatility_1m or 0.0,
            volume=row.volume,
        )
        self.store.insert_signal(signal_id=sid, created_at=self.rt.clock(), instrument_id=row.instrument_id,
                                 symbol=row.symbol, side=side, score=signal.score, payload=signal.model_dump())
        msg = Message(signal_id=sid, source_bot="SCOUT", target_bot="QUANT", status="SIGNAL",
                      reason=f"{side.upper()} {row.symbol} score {signal.score:+.3f} 5m {signal.move_5m:+.2f}%",
                      payload=signal.model_dump())
        self.rt.record(msg, "SIGNAL")

        # QUANT
        t = self.rt.clock()
        self.rt.heartbeat("QUANT")
        try:
            quote = (await self.quotes([row.instrument_id])).get(row.instrument_id)
            c1 = await self.candles(row.instrument_id, "OneMinute", 30)
            c5 = await self.candles(row.instrument_id, "FiveMinutes", 4)
            status, qp, reason = quant_mod.validate(signal, quote, c1, c5, self.cfg, self.rt.clock())
        except DataError as exc:
            status, qp, reason = "INVALID", None, f"DATA_CORRUPT {exc}"
        except (BrokerUncertain, BrokerError) as exc:
            status, qp, reason = "REJECTED", None, f"DATA_UNAVAILABLE {exc}"
        self.store.metric(sid, "QUANT", t, self.rt.clock())
        qmsg = Message(signal_id=sid, source_bot="QUANT", target_bot="GUARD" if status == "APPROVED" else "MANAGER",
                       status=status, reason=reason, payload=qp.model_dump() if qp else {})
        self.rt.record(qmsg, status)
        if status != "APPROVED":
            self.store.set_signal_status(sid, status, reason)
            if status == "INVALID":
                self.rt.invalid("QUANT_INVALID", reason)
            return "REJECTED" if status == "REJECTED" else status
        self.rt.invalid_streak = 0

        # GUARD
        t = self.rt.clock()
        self.rt.heartbeat("GUARD")
        await self.refresh_account()
        pending = sum(p["amount"] for p in self.store.live_positions() if p["position_id"] is None)
        status, gp, reason = guard_mod.evaluate(
            signal=signal, quant=qp, account=self.account, eligibility=self.eligibility.get(row.instrument_id),
            live_instruments={p["instrument_id"] for p in self.store.live_positions()}, pending_exposure=pending,
            stats=self.store.day_stats(t), halt=self.rt.halt, cfg=self.cfg, now_ms=self.rt.clock(),
        )
        self.store.metric(sid, "GUARD", t, self.rt.clock())
        gmsg = Message(signal_id=sid, source_bot="GUARD", target_bot="TRADER" if status == "APPROVED" else "MANAGER",
                       status=status, reason=reason, payload=gp.model_dump() if gp else {})
        self.rt.record(gmsg, status)
        if status != "APPROVED":
            self.store.set_signal_status(sid, status, reason)
            if status == "INVALID":
                self.rt.emergency("GUARD_INVALID", reason)
            return status
        self.store.set_signal_status(sid, "APPROVED")

        # TRADER
        self.rt.heartbeat("TRADER")
        result = await self.trader.execute(gmsg, signal, gp, self.eligibility.get(row.instrument_id))
        return result.status

    # -- monitoring ------------------------------------------------------------------

    async def monitor_once(self) -> None:
        self.rt.sync_halt()
        self.rt.heartbeat("TRADER")
        self._ticks += 1
        live = self.store.live_positions()
        if not live and not self.store.unresolved_orders():
            if self._ticks % ACCOUNT_EVERY_TICKS == 0:
                self.publish()
            return
        account = self.account
        if self._ticks % ACCOUNT_EVERY_TICKS == 0 or account is None:
            account = await self.refresh_account()
            if account is not None:
                await self.reconcile()
                live = self.store.live_positions()
        ids = sorted({p["instrument_id"] for p in live})
        quotes = await self.quotes(ids) if ids else {}
        # Account-based checks (positions eToro already closed) ran in reconcile above.
        await self.trader.monitor(None, quotes)
        self.publish()

    # -- dashboard state (read-only consumers) --------------------------------------

    def _publish_assets(self, rows: list[RankedAsset], started: int, outcome: str) -> None:
        self._last_scan = {
            "ts": started, "outcome": outcome, "universe": len(self.universe),
            "ranked": [r.public() for r in rows[:25]],
        }
        self.store.set_state("scan", self._last_scan)

    def publish(self) -> None:
        stats = self.store.day_stats(self.rt.clock())
        halt = self.rt.halt
        mode = "EMERGENCY_HALT" if halt.emergency else "ENTRY_HALTED" if halt.entry_halted else "RUNNING"
        if not self.verified:
            mode = "EMERGENCY_HALT" if halt.emergency else "NOT_VERIFIED"
        account = self.account
        self.store.set_state("runtime", {
            "env": self.cfg.trading_env,
            "mode": mode,
            "heartbeat": self.rt.clock(),
            "emergency_reason": halt.emergency_reason,
            "entry_reason": halt.entry_reason,
            "api_failures": self.rt.api_failures,
            "universe": len(self.universe),
            "account": None if account is None else {
                "cash": round(account.cash, 2), "equity": round(account.equity, 2),
                "invested": round(account.invested, 2), "positions": len(account.positions),
                "fetched": account.fetched_ms,
            },
            "day": {"realized_pnl": round(stats.realized_pnl, 2), "trades": stats.trades,
                    "consecutive_losses": stats.consecutive_losses},
            "limits": self.cfg.public(),
        })

    # -- loops -------------------------------------------------------------------------

    async def run(self) -> None:
        await self._guarded(self.start)
        await asyncio.gather(self._loop(self.scan_once, self.cfg.scan_interval_seconds),
                             self._loop(self.monitor_once, self.cfg.monitor_interval_seconds))

    async def _loop(self, step, interval: float) -> None:
        while True:
            await self._guarded(step)
            await self.rt.sleep(interval)

    async def _guarded(self, step) -> None:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except sqlite3.Error as exc:
            self.rt.emergency_reason = self.rt.emergency_reason or "DB_FAILURE"
            log.critical("database failure: %s", exc)
        except (BrokerUncertain, BrokerError, DataError) as exc:
            log.warning("%s failed: %s", getattr(step, "__name__", "step"), exc)
        except Exception:
            log.exception("%s crashed", getattr(step, "__name__", "step"))
            self.rt.invalid("UNEXPECTED_ERROR", getattr(step, "__name__", "step"))
