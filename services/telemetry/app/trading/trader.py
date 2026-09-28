"""TRADER: execution only. It never decides whether to trade.

Entry, in order:
 1. the approval came from GUARD, for a signal the database has as APPROVED,
    still inside its time-to-live, with no emergency halt;
 2. a fresh quote: realtime, not stale, spread and slippage within limits;
 3. position and order reserved in the database, which refuses a duplicate;
 4. marked SUBMITTED, then one POST with the stored x-request-id;
 5. an uncertain answer is resolved by looking the reference up. Never resent;
 6. the fill is confirmed, then eToro's copy of the position must carry both
    TP and the backstop SL, or it is closed at once as SYSTEM_FAILURE.

While MONITORING, the position closes on TP, the software SL, MAX_HOLD_SECONDS,
or (if configured) an emergency halt.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .broker import STATUS_FILLED, STATUS_PARTIAL, STATUS_REJECTED, AuthError, BrokerError, BrokerUncertain
from .guard import exit_prices
from .market import Account, BrokerPosition, DataError, Eligibility, Quote, parse_account, parse_rates, parse_ts_ms
from .messages import GuardPayload, Message, ScoutPayload
from .runtime import Runtime, rid

log = logging.getLogger("trading.trader")

POLL_S = 0.5
CLOSE_RETRY_MS = 5_000
EXTERNAL_CLOSE_GRACE_MS = 300_000


def round_rate(price: float) -> float:
    """Six significant digits: 83333.3 -> 83333.3, 0.0986312 -> 0.0986312."""
    if price <= 0 or not math.isfinite(price):
        raise ValueError(f"bad price {price}")
    decimals = max(2, 6 - int(math.floor(math.log10(price))) - 1)
    return round(price, decimals)


class Trader:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.cfg = rt.cfg
        self.store = rt.store
        self.broker = rt.broker
        self._last_close_try: dict[str, int] = {}
        self._missing_since: dict[str, int] = {}  # position_ref -> when eToro stopped listing it

    # -- helpers -----------------------------------------------------------------

    def _reply(self, signal_id: str, status: str, reason: str, payload: dict[str, Any] | None = None) -> Message:
        msg = Message(signal_id=signal_id, source_bot="TRADER", target_bot="MANAGER", status=status,
                      reason=reason, payload=payload or {})
        kind = {"EXECUTED": "POSITION_OPENED", "FAILED": "REJECTED"}.get(status, status)
        self.rt.record(msg, kind)
        return msg

    async def quote(self, instrument_id: int) -> Quote | None:
        payload = await self.rt.call("rates", self.broker.rates(rid(), [instrument_id]))
        return parse_rates(payload).get(instrument_id)

    async def account(self) -> Account:
        payload = await self.rt.call("portfolio", self.broker.portfolio(rid()))
        return parse_account(payload, self.rt.clock())

    def _match_new_position(self, account: Account, instrument_id: int, is_buy: bool, amount: float,
                            since_ms: int) -> BrokerPosition | None:
        """A position at eToro that fits an order we sent but did not hear back about."""
        known = {r["position_id"] for r in self.store.all("SELECT position_id FROM positions WHERE position_id IS NOT NULL")}
        for bp in account.positions.values():
            if (bp.instrument_id == instrument_id and bp.is_buy == is_buy and bp.position_id not in known
                    and (bp.open_ms or 0) >= since_ms - 5_000 and 0.9 * amount <= bp.amount <= 1.01 * amount):
                return bp
        return None

    async def locate_open(self, reference: str, instrument_id: int, is_buy: bool, amount: float, since_ms: int,
                          *, wait: bool = True) -> dict[str, Any] | None:
        """After an uncertain submit, find out whether eToro has the order. Never resends.

        eToro's lookup by reference does not find v1 market orders (verified on
        DEMO), so the portfolio is checked for a new position that matches the
        order: same instrument and side, an amount within fees of the request,
        opened after the submit, and not known to the trader. Its order id is
        then confirmed through the order lookup.
        """
        deadline = self.rt.clock() + self.cfg.order_confirm_timeout_seconds * 1000
        while True:
            try:
                info = await self.rt.call("lookup", self.broker.lookup_by_reference(rid(), reference))
                if info:
                    return info
                match = self._match_new_position(await self.account(), instrument_id, is_buy, amount, since_ms)
                if match is not None and match.order_id:
                    info = await self.rt.call("order", self.broker.order_info(rid(), match.order_id))
                    if info:
                        return info
            except (BrokerUncertain, BrokerError, DataError):
                pass
            if not wait or self.rt.clock() >= deadline:
                return None
            await self.rt.sleep(POLL_S)

    async def _await_open(self, order_id: int) -> dict[str, Any] | None:
        """Poll the order until eToro reports a final state. None on timeout."""
        deadline = self.rt.clock() + self.cfg.order_confirm_timeout_seconds * 1000
        while True:
            try:
                info = await self.rt.call("order", self.broker.order_info(rid(), order_id))
            except (BrokerUncertain, BrokerError):
                info = None
            status = ((info or {}).get("status") or {}).get("id")
            if status == STATUS_FILLED or status in STATUS_REJECTED:
                return info
            if status in STATUS_PARTIAL and _executions(info):
                return info
            if self.rt.clock() >= deadline:
                return None
            await self.rt.sleep(POLL_S)

    # -- entry -------------------------------------------------------------------

    async def execute(self, approval: Message, signal: ScoutPayload, guard: GuardPayload,
                      eligibility: Eligibility | None) -> Message:
        sid = approval.signal_id
        started = self.rt.clock()
        if approval.source_bot != "GUARD" or approval.status != "APPROVED":
            return self._reply(sid, "INVALID", "APPROVAL_NOT_FROM_GUARD")
        row = self.store.signal(sid)
        if row is None or row["status"] != "APPROVED":
            return self._reply(sid, "INVALID", f"SIGNAL_NOT_APPROVED ({row['status'] if row else 'missing'})")
        if row["instrument_id"] != signal.instrument_id:
            return self._reply(sid, "INVALID", "SIGNAL_MISMATCH")
        if (started - row["created_at"]) / 1000 > self.cfg.signal_ttl_seconds:
            self.store.set_signal_status(sid, "EXPIRED")
            return self._reply(sid, "REJECTED", "SIGNAL_EXPIRED")
        if self.rt.halt.emergency:
            return self._reply(sid, "RISK_BLOCK", f"EMERGENCY_HALT {self.rt.emergency_reason}")
        if eligibility is None or not (eligibility.allow_open and eligibility.allow_close):
            return self._reply(sid, "REJECTED", "INSTRUMENT_NOT_TRADABLE")

        quote = await self.quote(signal.instrument_id)
        now = self.rt.clock()
        if quote is None or not quote.realtime:
            return self._reply(sid, "REJECTED", "NO_REALTIME_QUOTE")
        if quote.age_s(now) > self.cfg.max_market_data_age_seconds:
            return self._reply(sid, "REJECTED", f"STALE_QUOTE age={quote.age_s(now):.1f}s")
        if quote.spread_pct > self.cfg.max_spread_percent:
            return self._reply(sid, "REJECTED", f"SPREAD {quote.spread_pct:.3f}%")
        long = signal.side == "long"
        entry_ref = quote.ask if long else quote.bid
        slippage = (entry_ref - guard.reference_price) / guard.reference_price * 100 * (1 if long else -1)
        if slippage > self.cfg.max_entry_slippage_percent:
            return self._reply(sid, "REJECTED", f"SLIPPAGE {slippage:.3f}% > {self.cfg.max_entry_slippage_percent}%")

        tp, sl, broker_sl = exit_prices(signal.side, entry_ref, guard.tp_pct, guard.sl_pct, guard.broker_sl_pct)
        tp, broker_sl = round_rate(tp), round_rate(broker_sl)
        position_ref, execution_id, request_id = f"pos_{uuid.uuid4().hex}", f"exe_{uuid.uuid4().hex}", str(uuid.uuid4())
        try:
            self.store.create_open(
                position_ref=position_ref, execution_id=execution_id, request_id=request_id, signal_id=sid,
                instrument_id=signal.instrument_id, symbol=signal.symbol, side=signal.side, amount=guard.amount,
                tp_price=tp, sl_price=sl, broker_sl_price=broker_sl, tp_pct=guard.tp_pct, sl_pct=guard.sl_pct,
            )
        except sqlite3.IntegrityError as exc:
            self.store.risk_event("DUPLICATE_BLOCKED", "WARN", f"{sid} {signal.symbol}: {exc}")
            return self._reply(sid, "REJECTED", "DUPLICATE_BLOCKED_BY_DATABASE")

        self.rt.inflight.add(execution_id)
        try:
            return await self._submit(sid, signal, guard, position_ref, execution_id, request_id, tp, sl, broker_sl, started)
        finally:
            self.rt.inflight.discard(execution_id)

    async def _submit(self, sid: str, signal: ScoutPayload, guard: GuardPayload, position_ref: str,
                      execution_id: str, request_id: str, tp: float, sl: float, broker_sl: float,
                      started: int) -> Message:
        self.store.move(position_ref, "SUBMITTED")
        self.store.update_order(execution_id, state="SUBMITTED", submitted_at=self.rt.clock())
        self.store.set_signal_status(sid, "EXECUTING")
        self.rt.event("EXECUTION_SUBMITTED", "TRADER", target="ETORO", status="SUBMITTED", signal_id=sid,
                      detail=f"{signal.side.upper()} {signal.symbol} ${guard.amount:.2f} TP {tp:g} backstop SL {broker_sl:g}",
                      payload={"execution_id": execution_id, "request_id": request_id, "env": self.broker.env})
        t_submit = self.rt.clock()
        order_id: int | None = None
        try:
            resp = await self.rt.call("open", self.broker.open_by_amount(
                request_id, instrument_id=signal.instrument_id, is_buy=signal.side == "long", amount=guard.amount,
                leverage=guard.leverage, stop_loss_rate=broker_sl, take_profit_rate=tp,
            ), write=True)
            order_id = int(((resp or {}).get("orderForOpen") or {}).get("orderID") or 0) or None
        except (AuthError, BrokerError) as exc:
            return self._rejected(sid, execution_id, position_ref, str(exc))
        except BrokerUncertain as exc:
            self.store.update_order(execution_id, state="UNKNOWN", error=str(exc))
            info = await self.locate_open(request_id, signal.instrument_id, signal.side == "long", guard.amount, t_submit)
            if info is None:
                self.store.update_order(execution_id, state="REJECTED", error="uncertain submit; no matching order at eToro",
                                        resolved_at=self.rt.clock())
                self.store.move(position_ref, "CLOSED", close_reason="SYSTEM_FAILURE", closed_at=self.rt.clock())
                self.store.set_signal_status(sid, "FAILED", "UNCERTAIN_SUBMIT")
                self.rt.emergency("UNCERTAIN_SUBMIT", f"{signal.symbol} request {request_id}")
                return self._reply(sid, "FAILED", "UNCERTAIN_SUBMIT_NOT_FOUND")
            order_id = int(info.get("orderId") or 0) or None
        self.store.metric(sid, "TRADER_SUBMIT", t_submit, self.rt.clock())
        if not order_id:
            self.store.update_order(execution_id, state="UNKNOWN", error="no order id in response")
            self.rt.emergency("ORDER_ID_MISSING", f"{signal.symbol} request {request_id}")
            return self._reply(sid, "FAILED", "ORDER_ID_MISSING")
        self.store.update_order(execution_id, order_id=order_id)

        info = await self._await_open(order_id)
        if info is None:
            self.store.update_order(execution_id, state="UNKNOWN", error="fill not confirmed in time")
            self.rt.emergency("FILL_UNCONFIRMED", f"{signal.symbol} order {order_id}")
            return self._reply(sid, "FAILED", "FILL_UNCONFIRMED")
        status = (info.get("status") or {})
        if status.get("id") in STATUS_REJECTED or not _executions(info):
            return self._rejected(sid, execution_id, position_ref,
                                  f"order {order_id} {status.get('name')}: {status.get('errorMessage') or ''}".strip())
        return await self._opened(sid, signal, guard, position_ref, execution_id, info, started)

    def _rejected(self, sid: str, execution_id: str, position_ref: str, error: str) -> Message:
        now = self.rt.clock()
        self.store.update_order(execution_id, state="REJECTED", error=error[:300], resolved_at=now)
        self.store.move(position_ref, "CLOSED", close_reason="BROKER_REJECT", closed_at=now)
        self.store.set_signal_status(sid, "FAILED", "BROKER_REJECT")
        return self._reply(sid, "FAILED", f"BROKER_REJECT {error[:160]}")

    async def _opened(self, sid: str, signal: ScoutPayload, guard: GuardPayload, position_ref: str,
                      execution_id: str, info: dict[str, Any], started: int) -> Message:
        pe = _executions(info)[0]
        opening = pe.get("openingData") or {}
        position_id = int(pe["positionId"])
        entry = float(opening.get("avgPrice") or 0.0)
        units = float(opening.get("units") or pe.get("remainingUnits") or 0.0)
        opened_at = parse_ts_ms(opening.get("executionTime") or opening.get("openTime")) or self.rt.clock()
        now = self.rt.clock()
        self.store.update_order(execution_id, state="FILLED", resolved_at=now,
                                response={"orderId": info.get("orderId"), "positionId": position_id})
        if entry <= 0 or units <= 0:
            entry = entry if entry > 0 else guard.reference_price
            self.store.risk_event("FILL_DATA_INCOMPLETE", "ERROR", f"{signal.symbol} position {position_id}")
        _, sl, _ = exit_prices(signal.side, entry, guard.tp_pct, guard.sl_pct, guard.broker_sl_pct)
        self.store.move(position_ref, "OPEN", position_id=position_id, entry_price=entry, units=units,
                        sl_price=sl, opened_at=opened_at, deadline_at=opened_at + self.cfg.max_hold_seconds * 1000)
        self.store.set_signal_status(sid, "EXECUTED")
        self.store.metric(sid, "SIGNAL_TO_FILL", started, now)
        self.rt.event("EXECUTION_FILLED", "ETORO", target="TRADER", status="FILLED", signal_id=sid,
                      detail=f"{signal.symbol} position {position_id} @ {entry:g} units {units:g}",
                      payload={"position_id": position_id, "order_id": info.get("orderId")})

        # eToro's own copy of the position must carry both protections.
        protected = await self._protection_confirmed(position_id)
        if not protected:
            self.store.move(position_ref, "MONITORING")
            self.store.risk_event("UNPROTECTED_POSITION", "CRITICAL", f"{signal.symbol} position {position_id}")
            await self.close(self.store.position(position_ref), "SYSTEM_FAILURE")
            return self._reply(sid, "FAILED", "PROTECTION_MISSING_CLOSED")
        self.store.move(position_ref, "MONITORING")
        return self._reply(sid, "EXECUTED", f"{signal.symbol} position {position_id} @ {entry:g}",
                           {"position_id": position_id, "entry": entry, "units": units, "amount": guard.amount})

    async def _protection_confirmed(self, position_id: int) -> bool:
        deadline = self.rt.clock() + self.cfg.order_confirm_timeout_seconds * 1000
        while True:
            try:
                account = await self.account()
                pos = account.positions.get(position_id)
                if pos is not None:
                    return pos.take_profit is not None and pos.stop_loss is not None
            except (BrokerUncertain, BrokerError):
                pass
            if self.rt.clock() >= deadline:
                return False
            await self.rt.sleep(POLL_S)

    # -- monitoring ----------------------------------------------------------------

    async def monitor(self, account: Account | None, quotes: dict[int, Quote]) -> None:
        now = self.rt.clock()
        for pos in self.store.live_positions():
            if pos["state"] == "OPEN":
                self.store.move(pos["position_ref"], "MONITORING")
                pos = self.store.position(pos["position_ref"])
            if pos["state"] != "MONITORING" or pos["position_id"] is None:
                continue
            if account is not None and pos["position_id"] not in account.positions:
                await self.finalize_external(pos, account)
                continue
            reason = self.exit_reason(pos, quotes.get(pos["instrument_id"]), now)
            if reason:
                await self.close(pos, reason)

    def exit_reason(self, pos: Any, quote: Quote | None, now: int) -> str | None:
        if pos["deadline_at"] and now >= pos["deadline_at"]:
            return "TIMEOUT"
        if self.rt.halt.emergency and self.cfg.emergency_close_positions:
            return "EMERGENCY"
        if quote is None or quote.age_s(now) > self.cfg.max_market_data_age_seconds:
            return None  # no trustworthy price: the broker TP/backstop SL and the timeout still protect
        long = pos["side"] == "long"
        exit_px = quote.bid if long else quote.ask
        if (long and exit_px <= pos["sl_price"]) or (not long and exit_px >= pos["sl_price"]):
            return "SL"
        if (long and exit_px >= pos["tp_price"]) or (not long and exit_px <= pos["tp_price"]):
            return "TP"
        return None

    async def close(self, pos: Any, reason: str) -> None:
        ref = pos["position_ref"]
        now = self.rt.clock()
        if now - self._last_close_try.get(ref, 0) < CLOSE_RETRY_MS:
            return
        self._last_close_try[ref] = now
        execution_id, request_id = f"exe_{uuid.uuid4().hex}", str(uuid.uuid4())
        try:
            self.store.create_close(execution_id=execution_id, request_id=request_id, position_ref=ref,
                                    instrument_id=pos["instrument_id"])
        except sqlite3.IntegrityError:
            return  # a close for this position is already in flight
        self.rt.inflight.add(execution_id)
        try:
            await self._close(pos, reason, execution_id, request_id)
        finally:
            self.rt.inflight.discard(execution_id)

    async def _close(self, pos: Any, reason: str, execution_id: str, request_id: str) -> None:
        self.store.update_order(execution_id, state="SUBMITTED", submitted_at=self.rt.clock())
        self.rt.event("EXECUTION_SUBMITTED", "TRADER", target="ETORO", status="SUBMITTED", signal_id=pos["signal_id"],
                      detail=f"CLOSE {pos['symbol']} position {pos['position_id']} ({reason})",
                      payload={"execution_id": execution_id, "request_id": request_id, "reason": reason})
        order_id: int | None = None
        try:
            resp = await self.rt.call("close", self.broker.close_position(
                request_id, position_id=pos["position_id"], instrument_id=pos["instrument_id"]), write=True)
            order_id = int(((resp or {}).get("orderForClose") or {}).get("orderID") or 0) or None
        except (AuthError, BrokerError) as exc:
            self.store.update_order(execution_id, state="REJECTED", error=str(exc)[:300], resolved_at=self.rt.clock())
            self.store.risk_event("CLOSE_REJECTED", "ERROR", f"{pos['symbol']} {pos['position_id']}: {exc}")
            return  # next monitor pass re-checks the account before any new attempt
        except BrokerUncertain as exc:
            # Closing twice is harmless (eToro refuses the second), but only
            # the account tells us whether the first one went through.
            self.store.update_order(execution_id, state="UNKNOWN", error=str(exc))
            try:
                account = await self.account()
            except (BrokerUncertain, BrokerError, DataError):
                return  # stays UNKNOWN; reconciliation settles it from the account
            if pos["position_id"] not in account.positions:
                self.store.update_order(execution_id, state="FILLED", resolved_at=self.rt.clock())
                await self.finalize_external(pos, account, reason=reason)
            else:
                self.store.update_order(execution_id, state="REJECTED", error="uncertain close; position still open",
                                        resolved_at=self.rt.clock())
            return
        if order_id:
            self.store.update_order(execution_id, order_id=order_id)
            fill = await self._await_close(order_id)
            if fill is not None:
                rate, at = fill
                self.store.update_order(execution_id, state="FILLED", resolved_at=self.rt.clock())
                self.finalize(pos, reason, rate, at)
                return
        self.store.update_order(execution_id, state="UNKNOWN", error="close not confirmed in time")
        self.store.risk_event("CLOSE_UNCONFIRMED", "ERROR", f"{pos['symbol']} position {pos['position_id']}")

    async def _await_close(self, order_id: int) -> tuple[float, int] | None:
        deadline = self.rt.clock() + self.cfg.order_confirm_timeout_seconds * 1000
        while True:
            try:
                info = await self.rt.call("close-order", self.broker.close_order_info(rid(), order_id))
            except (BrokerUncertain, BrokerError):
                info = None
            for row in (info or {}).get("positions") or []:
                if row.get("rate"):
                    return float(row["rate"]), parse_ts_ms(row.get("occurred")) or self.rt.clock()
            if (info or {}).get("errorCode"):
                return None
            if self.rt.clock() >= deadline:
                return None
            await self.rt.sleep(POLL_S)

    def finalize(self, pos: Any, reason: str, exit_price: float, closed_at: int, pnl: float | None = None,
                 final: bool = False) -> None:
        if pnl is None:
            sign = 1 if pos["side"] == "long" else -1
            pnl = sign * (exit_price - (pos["entry_price"] or exit_price)) * (pos["units"] or 0.0)
        self.store.move(pos["position_ref"], "CLOSED", close_reason=reason, exit_price=exit_price,
                        closed_at=closed_at, pnl=round(pnl, 4), pnl_final=1 if final else 0)
        detail = f"{pos['symbol']} position {pos['position_id']} {pos['entry_price']:g} -> {exit_price:g} pnl {pnl:+.2f}"
        self.rt.event("POSITION_CLOSED", "TRADER", status=reason, signal_id=pos["signal_id"], detail=detail,
                      payload={"reason": reason, "pnl": pnl, "exit": exit_price})
        if reason in {"TP", "SL", "TIMEOUT", "EMERGENCY"}:
            self.rt.event(reason, "TRADER", status=reason, signal_id=pos["signal_id"], detail=detail)
        if pos["opened_at"]:
            self.store.metric(pos["signal_id"], "HOLD", pos["opened_at"], closed_at)

    async def finalize_external(self, pos: Any, account: Account, reason: str | None = None) -> None:
        """The position left the account without a confirmed close from us: eToro's
        TP/SL, a manual close, or our own close whose answer was lost (``reason``)."""
        row = await self._history_row(pos["position_id"])
        now = self.rt.clock()
        if row is None:
            missing = self._missing_since.setdefault(pos["position_ref"], now)
            if now - missing < EXTERNAL_CLOSE_GRACE_MS:
                return  # not in history yet; check again next pass
            quote = await self.quote(pos["instrument_id"])
            exit_px = (quote.bid if pos["side"] == "long" else quote.ask) if quote else pos["entry_price"]
            self.store.risk_event("CLOSE_NOT_IN_HISTORY", "ERROR", f"position {pos['position_id']}")
            self.finalize(pos, reason or "SYSTEM_FAILURE", exit_px, now)  # P&L is estimated; refine_pnl corrects it
            self._missing_since.pop(pos["position_ref"], None)
            return
        self._missing_since.pop(pos["position_ref"], None)
        exit_px = float(row.get("closeRate") or 0.0)
        long = pos["side"] == "long"
        if reason:
            pass
        elif (long and exit_px >= pos["tp_price"] * 0.999) or (not long and exit_px <= pos["tp_price"] * 1.001):
            reason = "TP"
        elif (long and exit_px <= pos["broker_sl_price"] * 1.001) or (not long and exit_px >= pos["broker_sl_price"] * 0.999):
            reason = "SL"
        else:
            reason = "MANUAL"
        self.finalize(pos, reason, exit_px, parse_ts_ms(row.get("closeTimestamp")) or now,
                      pnl=float(row.get("netProfit") or 0.0), final=True)

    async def _history_row(self, position_id: int) -> dict[str, Any] | None:
        since = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        try:
            rows = await self.rt.call("history", self.broker.history(rid(), since))
        except (BrokerUncertain, BrokerError):
            return None
        for row in rows or []:
            if int(row.get("positionId") or 0) == position_id:
                return row
        return None

    async def refine_pnl(self) -> None:
        """Replace estimated P&L with eToro's net profit once history has it."""
        pending = self.store.all("SELECT * FROM positions WHERE state = 'CLOSED' AND pnl_final = 0 "
                                 "AND position_id IS NOT NULL AND entry_price IS NOT NULL")
        if not pending:
            return
        since = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        try:
            rows = await self.rt.call("history", self.broker.history(rid(), since))
        except (BrokerUncertain, BrokerError):
            return
        by_id = {int(r.get("positionId") or 0): r for r in rows or []}
        for pos in pending:
            row = by_id.get(pos["position_id"])
            if row is not None and row.get("netProfit") is not None:
                self.store.update_position(pos["position_ref"], pnl=float(row["netProfit"]), pnl_final=1)


def _executions(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [pe for pe in (info or {}).get("positionExecutions") or [] if pe.get("positionId")]


def broker_position_matches(pos: Any, bp: BrokerPosition) -> bool:
    return bp.instrument_id == pos["instrument_id"] and bp.is_buy == (pos["side"] == "long")
