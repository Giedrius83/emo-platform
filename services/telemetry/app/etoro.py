"""Read-only eToro feed: what the bots actually hold and trade.

The dashboard learns positions, closed trades and account value straight from
the eToro Public API, so it shows real numbers without any change to the bots.
This module only ever calls GET routes. It cannot place, modify or close a
trade, whatever key it is given.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .models import ActivityEvent, ActivityLevel, ClosedTrade, DataSource, Portfolio, Position

log = logging.getLogger("telemetry.etoro")

DEFAULT_BASE = "https://public-api.etoro.com"

ROUTES = {
    "real": {"pnl": "/api/v1/trading/info/real/pnl", "history": "/api/v1/trading/info/trade/history"},
    "demo": {"pnl": "/api/v1/trading/info/demo/pnl", "history": "/api/v1/trading/info/trade/demo/history"},
}
INSTRUMENTS = "/api/v1/market-data/instruments"


class EtoroError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"eToro API {status}: {message}")
        self.status = status
        self.retry_after = retry_after


# --- parsing: pure functions over the raw API payloads -------------------------


@dataclass(frozen=True)
class Instrument:
    instrument_id: int
    symbol: str
    name: str
    logo: str | None = None


def parse_instruments(payload: dict[str, Any]) -> dict[int, Instrument]:
    out: dict[int, Instrument] = {}
    for row in payload.get("instrumentDisplayDatas") or []:
        iid = int(row.get("instrumentID") or 0)
        if not iid:
            continue
        images = [img for img in row.get("images") or [] if img.get("width") and img.get("uri")]
        # Prefer the 50px avatar; fall back to whatever raster exists.
        images.sort(key=lambda img: abs(float(img["width"]) - 50))
        out[iid] = Instrument(
            instrument_id=iid,
            symbol=str(row.get("symbolFull") or row.get("instrumentDisplayName") or f"#{iid}"),
            name=str(row.get("instrumentDisplayName") or row.get("symbolFull") or f"#{iid}"),
            logo=images[0]["uri"] if images else None,
        )
    return out


def _ms(value: str | None) -> int:
    if not value:
        return 0
    text = value.rstrip("Z")
    # eToro sends up to 7 fractional digits; Python takes at most 6.
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _protective(rate: Any, open_rate: float, *, kind: str, disabled: bool = False) -> float | None:
    """eToro returns placeholder rates when SL/TP is unset; hide those."""
    if disabled or rate in (None, 0):
        return None
    rate = float(rate)
    if open_rate <= 0 or rate <= 0:
        return None
    if kind == "sl" and rate < open_rate * 0.01:
        return None
    if kind == "tp" and rate > open_rate * 50:
        return None
    return rate


def _lookup(instruments: dict[int, Instrument], iid: int) -> Instrument:
    return instruments.get(iid) or Instrument(iid, f"#{iid}", f"Instrument {iid}")


def parse_positions(payload: dict[str, Any], instruments: dict[int, Instrument]) -> list[Position]:
    portfolio = payload.get("clientPortfolio") or {}
    out: list[Position] = []
    for row in portfolio.get("positions") or []:
        iid = int(row.get("instrumentID") or 0)
        inst = _lookup(instruments, iid)
        upnl = row.get("unrealizedPnL") or {}
        invested = float(row.get("amount") or 0.0)
        pnl = float(upnl.get("pnL") or 0.0)
        open_rate = float(row.get("openRate") or 0.0)
        out.append(
            Position(
                position_id=int(row.get("positionID") or 0),
                instrument_id=iid,
                symbol=inst.symbol,
                name=inst.name,
                logo=inst.logo,
                direction="long" if row.get("isBuy", True) else "short",
                leverage=int(row.get("leverage") or 1),
                units=float(row.get("units") or 0.0),
                invested=round(invested, 2),
                value=round(invested + pnl, 2),
                pnl=round(pnl, 2),
                pnl_pct=round(pnl / invested * 100, 2) if invested else 0.0,
                open_rate=open_rate,
                current_rate=float(upnl["closeRate"]) if upnl.get("closeRate") else None,
                stop_loss=_protective(row.get("stopLossRate"), open_rate, kind="sl", disabled=bool(row.get("isNoStopLoss"))),
                take_profit=_protective(row.get("takeProfitRate"), open_rate, kind="tp", disabled=bool(row.get("isNoTakeProfit"))),
                opened_at=_ms(row.get("openDateTime")),
            )
        )
    out.sort(key=lambda p: p.opened_at, reverse=True)
    return out


def parse_trades(payload: list[dict[str, Any]], instruments: dict[int, Instrument]) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for row in payload or []:
        iid = int(row.get("instrumentId") or 0)
        inst = _lookup(instruments, iid)
        open_rate = float(row.get("openRate") or 0.0)
        out.append(
            ClosedTrade(
                position_id=int(row.get("positionId") or 0),
                instrument_id=iid,
                symbol=inst.symbol,
                name=inst.name,
                logo=inst.logo,
                direction="long" if row.get("isBuy", True) else "short",
                leverage=int(row.get("leverage") or 1),
                units=float(row.get("units") or 0.0),
                invested=round(float(row.get("investment") or 0.0), 2),
                open_rate=open_rate,
                close_rate=float(row.get("closeRate") or 0.0),
                opened_at=_ms(row.get("openTimestamp")),
                closed_at=_ms(row.get("closeTimestamp")),
                net_profit=round(float(row.get("netProfit") or 0.0), 2),
                fees=round(float(row.get("fees") or 0.0), 2),
            )
        )
    out.sort(key=lambda t: t.closed_at, reverse=True)
    return out


def instrument_ids(pnl_payload: dict[str, Any] | None, history_payload: list[dict[str, Any]] | None) -> set[int]:
    ids = {int(r.get("instrumentID") or 0) for r in ((pnl_payload or {}).get("clientPortfolio") or {}).get("positions") or []}
    ids |= {int(r.get("instrumentId") or 0) for r in history_payload or []}
    ids.discard(0)
    return ids


def build_portfolio(
    source: DataSource,
    pnl_payload: dict[str, Any],
    trades: list[ClosedTrade],
    instruments: dict[int, Instrument],
    *,
    realized_since: int,
    now_ms: int,
) -> Portfolio:
    client = pnl_payload.get("clientPortfolio") or {}
    positions = parse_positions(pnl_payload, instruments)
    cash = float(client.get("credit") or 0.0)
    invested = sum(p.invested for p in positions)
    unrealized = float(client.get("unrealizedPnL") or sum(p.pnl for p in positions))
    window = [t for t in trades if t.closed_at >= realized_since]
    return Portfolio(
        source=source,
        equity=round(cash + invested + unrealized, 2),
        cash=round(cash, 2),
        invested=round(invested, 2),
        unrealized_pnl=round(unrealized, 2),
        realized_pnl=round(sum(t.net_profit for t in window), 2),
        realized_since=realized_since,
        trades_count=len(window),
        wins=sum(1 for t in window if t.net_profit > 0),
        positions=positions,
        trades=trades[:50],
        updated_at=now_ms,
    )


def describe_open(p: Position) -> str:
    side = "BUY" if p.direction == "long" else "SELL"
    guards = []
    if p.stop_loss:
        guards.append(f"SL {p.stop_loss:g}")
    if p.take_profit:
        guards.append(f"TP {p.take_profit:g}")
    lev = f" x{p.leverage}" if p.leverage > 1 else ""
    tail = f" · {' '.join(guards)}" if guards else ""
    return f"{side} {p.symbol}{lev} ${p.invested:,.2f} @ {p.open_rate:g}{tail}"


def describe_close(t: ClosedTrade) -> str:
    side = "long" if t.direction == "long" else "short"
    return f"closed {side} {t.symbol} ${t.invested:,.2f} · {t.open_rate:g} → {t.close_rate:g}"


# --- network ------------------------------------------------------------------


class EtoroClient:
    """Minimal read-only client. Holds no state beyond its HTTP connection."""

    def __init__(
        self,
        api_key: str,
        user_key: str,
        *,
        base_url: str = DEFAULT_BASE,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"x-api-key": api_key, "x-user-key": user_key, "accept": "application/json"},
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._http.get(path, params=params, headers={"x-request-id": str(uuid.uuid4())})
        if response.status_code == 429:
            retry = float(response.headers.get("Retry-After") or 60)
            raise EtoroError(429, "rate limited", retry_after=retry)
        if response.status_code >= 400:
            raise EtoroError(response.status_code, response.text[:200] or response.reason_phrase)
        return response.json()

    async def aclose(self) -> None:
        await self._http.aclose()


Publish = Callable[[Portfolio, list[ActivityEvent], bool], Awaitable[None] | None]


@dataclass
class EtoroFeed:
    """Polls the account and turns changes into dashboard events.

    Positions every ``poll_s`` seconds; closed trades every ``history_s`` seconds,
    or immediately when a position disappears so a close shows up without lag.
    """

    client: EtoroClient
    account: str
    make_event: Callable[..., ActivityEvent]
    publish: Publish
    poll_s: float = 10.0
    history_s: float = 60.0
    history_days: int = 90

    instruments: dict[int, Instrument] = field(default_factory=dict)
    trades: list[ClosedTrade] = field(default_factory=list)
    portfolio: Portfolio | None = None
    last_error: str | None = None
    _known_positions: set[int] = field(default_factory=set)
    _known_trades: set[int] = field(default_factory=set)
    _primed: bool = False
    _history_due: bool = True
    _last_history: float = 0.0

    @property
    def source(self) -> DataSource:
        return DataSource.ETORO_DEMO if self.account == "demo" else DataSource.ETORO_REAL

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self.poll_once(loop.time())
                self.last_error = None
                await asyncio.sleep(self.poll_s)
            except asyncio.CancelledError:
                raise
            except EtoroError as exc:
                self.last_error = str(exc)
                log.warning("%s", exc)
                if self.portfolio is not None:
                    self.portfolio.error = self.last_error
                wait = exc.retry_after if exc.retry_after else max(self.poll_s, 30.0)
                if exc.status in (401, 403):
                    wait = 300.0  # bad keys will not fix themselves; do not hammer
                await asyncio.sleep(wait)
            except Exception as exc:  # network blips must not kill the monitor
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("eToro poll failed: %s", self.last_error)
                await asyncio.sleep(max(self.poll_s, 30.0))

    async def poll_once(self, clock: float) -> Portfolio:
        routes = ROUTES["demo" if self.account == "demo" else "real"]
        pnl_payload = await self.client.get(routes["pnl"])

        live_ids = {int(r.get("positionID") or 0) for r in (pnl_payload.get("clientPortfolio") or {}).get("positions") or []}
        if self._known_positions - live_ids:
            self._history_due = True  # something closed: fetch its result now

        history_payload: list[dict[str, Any]] | None = None
        if self._history_due or clock - self._last_history >= self.history_s:
            history_payload = await self._fetch_history()
            self._last_history = clock
            self._history_due = False

        missing = instrument_ids(pnl_payload, history_payload) - set(self.instruments)
        if missing:
            payload = await self.client.get(INSTRUMENTS, {"instrumentIds": ",".join(map(str, sorted(missing)))})
            self.instruments.update(parse_instruments(payload))

        if history_payload is not None:
            self.trades = parse_trades(history_payload, self.instruments)

        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        since = int((datetime.now(timezone.utc) - timedelta(days=self.history_days)).timestamp() * 1000)
        portfolio = build_portfolio(self.source, pnl_payload, self.trades, self.instruments, realized_since=since, now_ms=now)
        events = self._diff(portfolio)
        rebuilt = not self._primed
        self._primed = True
        self.portfolio = portfolio
        result = self.publish(portfolio, events, rebuilt)
        if asyncio.iscoroutine(result):
            await result
        return portfolio

    async def _fetch_history(self) -> list[dict[str, Any]]:
        routes = ROUTES["demo" if self.account == "demo" else "real"]
        min_date = (datetime.now(timezone.utc) - timedelta(days=self.history_days)).date().isoformat()
        rows: list[dict[str, Any]] = []
        for page in range(1, 11):  # at most 2000 trades per window
            batch = await self.client.get(routes["history"], {"minDate": min_date, "page": page, "pageSize": 200})
            rows.extend(batch or [])
            if len(batch or []) < 200:
                break
        return rows

    def _diff(self, portfolio: Portfolio) -> list[ActivityEvent]:
        """Turn the change since the last poll into activity lines."""
        events: list[ActivityEvent] = []
        position_ids = {p.position_id for p in portfolio.positions}
        trade_ids = {t.position_id for t in self.trades}

        if not self._primed:
            # First sight: replay recent closes so the log opens with real history.
            for trade in sorted(self.trades[:25], key=lambda t: t.closed_at):
                events.append(self._close_event(trade, at=trade.closed_at))
            for pos in sorted(portfolio.positions, key=lambda p: p.opened_at):
                events.append(self._open_event(pos, at=pos.opened_at))
        else:
            for pos in portfolio.positions:
                if pos.position_id not in self._known_positions:
                    events.append(self._open_event(pos))
            for trade in self.trades:
                if trade.position_id not in self._known_trades:
                    events.append(self._close_event(trade))

        self._known_positions = position_ids
        self._known_trades = trade_ids
        return events

    def _open_event(self, pos: Position, at: int | None = None) -> ActivityEvent:
        event = self.make_event("ETORO", "OPEN", describe_open(pos), level=ActivityLevel.INFO, value=None)
        if at:
            event.t = at
        return event

    def _close_event(self, trade: ClosedTrade, at: int | None = None) -> ActivityEvent:
        level = ActivityLevel.SUCCESS if trade.net_profit >= 0 else ActivityLevel.WARN
        event = self.make_event("ETORO", "CLOSE", describe_close(trade), level=level, value=trade.net_profit)
        if at:
            event.t = at
        return event
