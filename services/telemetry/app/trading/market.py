"""Pure functions over eToro market-data payloads. No I/O here.

Shapes follow the live API as observed on the account:
* rates:   ``{"results": [{"instrumentId", "bid", "ask", "date", "quoteType"}]}``
           ``date`` is UTC but may come without a zone suffix.
* candles: ``{"candles": [{"instrumentId", "candles": [{"fromDate", "open", ...,
           "volume": null}]}]}``. eToro returns ``volume: null`` for crypto, so
           volume is reported as unavailable, never invented.
* search:  instrument rows with ``internalAssetClassId`` (10 = crypto) and
           tradability flags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CRYPTO_ASSET_CLASS = 10


class DataError(ValueError):
    """A payload is missing fields or contradicts itself (e.g. bid above ask)."""


def parse_ts_ms(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    elif "+" in text[10:]:
        text = text[: 10 + text[10:].index("+")]
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class Quote:
    instrument_id: int
    bid: float
    ask: float
    ts_ms: int
    realtime: bool

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100

    def age_s(self, now_ms: int) -> float:
        return (now_ms - self.ts_ms) / 1000


def parse_rates(payload: dict[str, Any]) -> dict[int, Quote]:
    """Quotes by instrument. Rows that are incomplete or inconsistent are dropped."""
    out: dict[int, Quote] = {}
    for row in (payload or {}).get("results") or []:
        try:
            iid = int(row.get("instrumentId") or 0)
            bid, ask = row.get("bid"), row.get("ask")
            ts = parse_ts_ms(row.get("date"))
            if not iid or bid is None or ask is None or ts is None:
                continue
            bid, ask = float(bid), float(ask)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or bid > ask or not math.isfinite(bid + ask):
            continue
        out[iid] = Quote(iid, bid, ask, ts, realtime=row.get("quoteType") == "realtime")
    return out


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None


def parse_candles(payload: dict[str, Any], instrument_id: int) -> list[Candle]:
    """Candles oldest first. Raises DataError on a malformed payload."""
    groups = (payload or {}).get("candles")
    if not isinstance(groups, list):
        raise DataError("candles payload has no 'candles' list")
    rows: list[dict[str, Any]] = []
    for group in groups:
        if int(group.get("instrumentId") or 0) == instrument_id:
            rows = group.get("candles") or []
    out: list[Candle] = []
    for row in rows:
        ts = parse_ts_ms(row.get("fromDate"))
        try:
            values = [float(row[k]) for k in ("open", "high", "low", "close")]
        except (KeyError, TypeError, ValueError):
            raise DataError("candle is missing a price") from None
        if ts is None or min(values) <= 0:
            raise DataError("candle has no time or a non-positive price")
        vol = row.get("volume")
        out.append(Candle(ts, *values, volume=float(vol) if vol is not None else None))
    out.sort(key=lambda c: c.ts_ms)
    return out


def candles_fresh(candles: list[Candle], now_ms: int, max_age_s: float) -> bool:
    return bool(candles) and (now_ms - candles[-1].ts_ms) / 1000 <= max_age_s


def pct(a: float, b: float) -> float:
    return (b - a) / a * 100 if a else 0.0


def move(candles: list[Candle], steps: int) -> float | None:
    """Close-to-close change over the last ``steps`` candles, in percent."""
    if len(candles) <= steps:
        return None
    return pct(candles[-1 - steps].close, candles[-1].close)


def volatility(candles: list[Candle]) -> float | None:
    """Standard deviation of close-to-close returns, in percent."""
    if len(candles) < 4:
        return None
    returns = [pct(a.close, b.close) for a, b in zip(candles, candles[1:])]
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))


def total_volume(candles: list[Candle]) -> float | None:
    values = [c.volume for c in candles if c.volume is not None]
    return sum(values) if values else None


# --- instrument universe ------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    instrument_id: int
    symbol: str
    name: str


def parse_universe(rows: list[dict[str, Any]]) -> dict[int, Instrument]:
    """Crypto instruments eToro says are listed, visible, active and buyable now."""
    out: dict[int, Instrument] = {}
    for row in rows:
        iid = int(row.get("instrumentId") or 0)
        if not iid:
            continue
        if row.get("internalAssetClassId") != CRYPTO_ASSET_CLASS:
            continue
        if row.get("isDelisted") is not False or row.get("isHiddenFromClient") is not False:
            continue
        if row.get("isCurrentlyTradable") is not True or row.get("isBuyEnabled") is not True:
            continue
        if row.get("isActiveInPlatform") is False:
            continue
        symbol = str(row.get("internalSymbolFull") or "").strip()
        if not symbol:
            continue
        out[iid] = Instrument(iid, symbol.upper(), str(row.get("displayname") or symbol))
    return out


@dataclass(frozen=True)
class SideRules:
    leverages: tuple[int, ...]
    min_amount: float
    min_sl_pct: float
    allow_sl_tp: bool


@dataclass(frozen=True)
class Eligibility:
    instrument_id: int
    symbol: str
    allow_open: bool
    allow_close: bool
    min_exposure: float
    long: SideRules | None = None
    short: SideRules | None = None
    raw_ok: bool = field(default=True, compare=False)

    def rules(self, side: str) -> SideRules | None:
        return self.long if side == "long" else self.short


def parse_eligibility(payload: dict[str, Any]) -> dict[int, Eligibility]:
    out: dict[int, Eligibility] = {}
    for row in (payload or {}).get("eligibilities") or []:
        iid = int(row.get("instrumentId") or 0)
        if not iid:
            continue
        sides: dict[str, SideRules] = {}
        for cfg in row.get("leverageConfigs") or []:
            direction = str(cfg.get("direction") or "").lower()
            if direction not in {"long", "short"} or cfg.get("isPotential"):
                continue
            rules = SideRules(
                leverages=tuple(int(x) for x in cfg.get("leverageValues") or []),
                min_amount=float(cfg.get("minPositionAmount") or 0.0),
                min_sl_pct=float(cfg.get("minStopLossPercentage") or 0.0),
                allow_sl_tp=bool(cfg.get("allowStopLossTakeProfit")) and bool(cfg.get("allowEditStopLoss")),
            )
            if direction in sides:  # several configs: keep the union of leverages, strictest limits
                prev = sides[direction]
                rules = SideRules(
                    leverages=tuple(sorted(set(prev.leverages) | set(rules.leverages))),
                    min_amount=max(prev.min_amount, rules.min_amount),
                    min_sl_pct=max(prev.min_sl_pct, rules.min_sl_pct),
                    allow_sl_tp=prev.allow_sl_tp and rules.allow_sl_tp,
                )
            sides[direction] = rules
        out[iid] = Eligibility(
            instrument_id=iid,
            symbol=str(row.get("symbol") or ""),
            allow_open=row.get("allowOpenPosition") is True,
            allow_close=row.get("allowClosePosition") is True,
            min_exposure=float(row.get("minPositionExposure") or 0.0),
            long=sides.get("long"),
            short=sides.get("short"),
        )
    return out


# --- account --------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerPosition:
    position_id: int
    instrument_id: int
    is_buy: bool
    amount: float
    units: float
    open_rate: float
    stop_loss: float | None
    take_profit: float | None
    open_ms: int | None
    pnl: float


@dataclass(frozen=True)
class Account:
    cash: float
    equity: float
    invested: float
    positions: dict[int, BrokerPosition]
    pending_orders: int
    fetched_ms: int


def parse_account(payload: dict[str, Any], fetched_ms: int) -> Account:
    client = (payload or {}).get("clientPortfolio")
    if not isinstance(client, dict) or "credit" not in client:
        raise DataError("portfolio payload has no clientPortfolio.credit")
    positions: dict[int, BrokerPosition] = {}
    for row in client.get("positions") or []:
        pid = int(row.get("positionID") or 0)
        if not pid:
            raise DataError("portfolio position without positionID")
        upnl = row.get("unrealizedPnL") or {}
        sl = row.get("stopLossRate")
        tp = row.get("takeProfitRate")
        positions[pid] = BrokerPosition(
            position_id=pid,
            instrument_id=int(row.get("instrumentID") or 0),
            is_buy=bool(row.get("isBuy", True)),
            amount=float(row.get("amount") or 0.0),
            units=float(row.get("units") or 0.0),
            open_rate=float(row.get("openRate") or 0.0),
            stop_loss=None if row.get("isNoStopLoss") or not sl else float(sl),
            take_profit=None if row.get("isNoTakeProfit") or not tp else float(tp),
            open_ms=parse_ts_ms(row.get("openDateTime")),
            pnl=float(upnl.get("pnL") or 0.0),
        )
    cash = float(client.get("credit") or 0.0)
    invested = sum(p.amount for p in positions.values())
    unrealized = float(client.get("unrealizedPnL") or sum(p.pnl for p in positions.values()))
    pending = sum(len(client.get(k) or []) for k in ("orders", "ordersForOpen", "entryOrders"))
    return Account(cash=cash, equity=cash + invested + unrealized, invested=invested,
                   positions=positions, pending_orders=pending, fetched_ms=fetched_ms)
