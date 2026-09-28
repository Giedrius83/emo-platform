"""GUARD: deterministic risk checks, position size, and exit levels.

Pure function over the account as eToro reports it right now, the trader's
own records, and the configured limits. The same inputs always give the same
answer. Nothing here talks to the network.

Exit levels
-----------
eToro enforces a minimum stop-loss distance on crypto (10% of margin on the
live account), so a 0.3-0.5% stop cannot be placed at the broker. Guard
therefore sets two stops:

* ``sl_pct``         the configured 0.3-0.5% stop, enforced by the Trader's
                     monitor, which closes the position at market;
* ``broker_sl_pct``  the tightest stop eToro accepts plus a buffer, placed on
                     the order as a crash backstop in case the trader is down.

Take-profit has no broker minimum, so ``tp_pct`` is placed at eToro directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import TradingConfig
from .db import DayStats
from .market import Account, Eligibility
from .messages import GuardPayload, QuantPayload, ScoutPayload

ACCOUNT_MAX_AGE_S = 30


@dataclass(frozen=True)
class Halt:
    emergency: bool = False
    emergency_reason: str = ""
    entry_halted: bool = False
    entry_reason: str = ""


def entry_halt_reason(stats: DayStats, cfg: TradingConfig) -> str:
    """Why new entries are paused for the rest of the UTC day, or '' if they are not."""
    if stats.realized_pnl <= -cfg.max_daily_loss_usd:
        return f"DAILY_LOSS_LIMIT {stats.realized_pnl:.2f} <= -{cfg.max_daily_loss_usd}"
    if stats.consecutive_losses >= cfg.max_consecutive_losses:
        return f"CONSECUTIVE_LOSSES {stats.consecutive_losses} >= {cfg.max_consecutive_losses}"
    if stats.trades >= cfg.max_trade_count_per_day:
        return f"MAX_TRADES_PER_DAY {stats.trades} >= {cfg.max_trade_count_per_day}"
    return ""


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def evaluate(
    *,
    signal: ScoutPayload,
    quant: QuantPayload,
    account: Account | None,
    eligibility: Eligibility | None,
    live_instruments: set[int],
    pending_exposure: float,
    stats: DayStats,
    halt: Halt,
    cfg: TradingConfig,
    now_ms: int,
) -> tuple[str, GuardPayload | None, str]:
    if halt.emergency:
        return "RISK_BLOCK", None, f"EMERGENCY_HALT {halt.emergency_reason}"
    reason = halt.entry_reason if halt.entry_halted else entry_halt_reason(stats, cfg)
    if reason:
        return "RISK_BLOCK", None, reason
    if account is None or (now_ms - account.fetched_ms) / 1000 > ACCOUNT_MAX_AGE_S:
        return "REJECTED", None, "ACCOUNT_DATA_STALE"
    if signal.side == "short" and not cfg.allow_short:
        return "REJECTED", None, "SHORT_DISABLED"

    if eligibility is None:
        return "REJECTED", None, "NO_ELIGIBILITY_DATA"
    if not (eligibility.allow_open and eligibility.allow_close):
        return "REJECTED", None, "INSTRUMENT_NOT_TRADABLE"
    rules = eligibility.rules(signal.side)
    if rules is None or cfg.leverage not in rules.leverages:
        return "REJECTED", None, f"LEVERAGE_{cfg.leverage}_NOT_ALLOWED"
    if not rules.allow_sl_tp:
        return "REJECTED", None, "SL_TP_NOT_ALLOWED"

    held = {p.instrument_id for p in account.positions.values()} | live_instruments
    if signal.instrument_id in held:
        return "REJECTED", None, "DUPLICATE_INSTRUMENT"
    open_count = len(account.positions) + len(live_instruments - {p.instrument_id for p in account.positions.values()})
    if open_count >= cfg.max_open_positions:
        return "RISK_BLOCK", None, f"MAX_OPEN_POSITIONS {open_count} >= {cfg.max_open_positions}"
    if account.pending_orders:
        return "REJECTED", None, "BROKER_HAS_PENDING_ORDERS"

    equity = account.equity
    if not math.isfinite(equity) or equity <= 0:
        return "INVALID", None, "EQUITY_NOT_POSITIVE"
    exposure = account.invested + pending_exposure
    room = equity * cfg.max_total_exposure_percent / 100 - exposure
    caps = [equity * cfg.max_position_percent / 100, account.cash, room]
    if cfg.max_position_usd > 0:
        caps.append(cfg.max_position_usd)
    amount = math.floor(min(caps) * 100) / 100
    minimum = max(eligibility.min_exposure, rules.min_amount)
    if amount < minimum:
        if account.cash < minimum:
            return "REJECTED", None, f"INSUFFICIENT_CASH {account.cash:.2f} < {minimum:.2f}"
        if room < minimum:
            return "RISK_BLOCK", None, f"MAX_TOTAL_EXPOSURE room {room:.2f} < {minimum:.2f}"
        return "REJECTED", None, f"BELOW_MINIMUM {amount:.2f} < {minimum:.2f}"

    vol = quant.volatility_1m
    sl_pct = round(clamp(1.5 * vol, cfg.sl_percent_min, cfg.sl_percent_max), 4)
    tp_pct = round(clamp(max(2.0 * vol, sl_pct), cfg.tp_percent_min, cfg.tp_percent_max), 4)
    if tp_pct < sl_pct:
        return "REJECTED", None, f"RISK_REWARD tp {tp_pct}% < sl {sl_pct}%"
    if quant.spread_pct * 2 > tp_pct:
        return "REJECTED", None, f"COST_TOO_HIGH spread {quant.spread_pct}% vs tp {tp_pct}%"
    broker_sl_pct = round(rules.min_sl_pct / cfg.leverage + cfg.broker_sl_buffer_percent, 4)
    if broker_sl_pct <= sl_pct:
        broker_sl_pct = round(sl_pct + cfg.broker_sl_buffer_percent, 4)

    reference = signal.ask if signal.side == "long" else signal.bid
    return "APPROVED", GuardPayload(
        amount=amount,
        leverage=cfg.leverage,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        broker_sl_pct=broker_sl_pct,
        reference_price=reference,
        equity=round(equity, 2),
        cash=round(account.cash, 2),
        exposure_after_pct=round((exposure + amount) / equity * 100, 3),
    ), "within limits"


def exit_prices(side: str, entry: float, tp_pct: float, sl_pct: float, broker_sl_pct: float) -> tuple[float, float, float]:
    """(take-profit, software stop, broker backstop stop) around an entry price."""
    sign = 1 if side == "long" else -1
    tp = entry * (1 + sign * tp_pct / 100)
    sl = entry * (1 - sign * sl_pct / 100)
    broker_sl = entry * (1 - sign * broker_sl_pct / 100)
    return tp, sl, broker_sl
