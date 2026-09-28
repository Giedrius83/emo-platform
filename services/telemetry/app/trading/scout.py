"""SCOUT: rank the whole tradable crypto universe and emit signals.

Two passes keep the API budget small without narrowing the universe:

1. ``prefilter`` looks at every discovered instrument using one rates call:
   fresh, realtime, spread under the limit, not already held. Instruments are
   ordered by their recent drift (from Scout's own rolling mid-price history)
   minus a spread penalty. On the very first scan there is no history yet, so
   the order falls back to tightest spread.
2. ``score`` fetches 1m candles for the top few and computes the ranking
   factors. Every factor and weight is configurable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field

from .config import TradingConfig
from .market import Candle, Instrument, Quote, move, pct, total_volume, volatility

HISTORY_POINTS = 12  # rolling mids kept per instrument (one per scan)


@dataclass
class RankedAsset:
    instrument_id: int
    symbol: str
    name: str
    bid: float
    ask: float
    mid: float
    spread_pct: float
    quote_ts: int
    quote_age_s: float
    drift_pct: float | None = None
    move_1m: float | None = None
    move_5m: float | None = None
    volatility_1m: float | None = None
    volume: float | None = None
    score: float | None = None
    status: str = "RANKED"  # RANKED | CANDIDATE | SIGNAL | SPREAD | STALE | DELAYED | HELD | NO_DATA
    closes: list[float] = field(default_factory=list)
    candle_ts: list[int] = field(default_factory=list)

    def public(self) -> dict:
        row = asdict(self)
        row["volume_status"] = "OK" if self.volume is not None else "DATA_UNAVAILABLE"
        return row


class Scout:
    def __init__(self, cfg: TradingConfig) -> None:
        self.cfg = cfg
        self._mids: dict[int, deque[float]] = {}

    def remember(self, quotes: dict[int, Quote]) -> None:
        for iid, quote in quotes.items():
            self._mids.setdefault(iid, deque(maxlen=HISTORY_POINTS)).append(quote.mid)

    def drift(self, iid: int) -> float | None:
        mids = self._mids.get(iid)
        if not mids or len(mids) < 2:
            return None
        return pct(mids[0], mids[-1])

    def prefilter(self, universe: dict[int, Instrument], quotes: dict[int, Quote], now_ms: int,
                  held: set[int]) -> tuple[list[RankedAsset], list[RankedAsset]]:
        """Return (candidates in priority order, every row for the dashboard)."""
        self.remember({iid: q for iid, q in quotes.items() if iid in universe})
        rows: list[RankedAsset] = []
        for iid, inst in universe.items():
            quote = quotes.get(iid)
            if quote is None:
                continue
            row = RankedAsset(
                instrument_id=iid, symbol=inst.symbol, name=inst.name, bid=quote.bid, ask=quote.ask,
                mid=quote.mid, spread_pct=round(quote.spread_pct, 4), quote_ts=quote.ts_ms,
                quote_age_s=round(quote.age_s(now_ms), 2), drift_pct=self.drift(iid),
            )
            if not quote.realtime:
                row.status = "DELAYED"
            elif quote.age_s(now_ms) > self.cfg.max_market_data_age_seconds:
                row.status = "STALE"
            elif quote.spread_pct > self.cfg.max_spread_percent:
                row.status = "SPREAD"
            elif iid in held:
                row.status = "HELD"
            rows.append(row)

        w = self.cfg
        def priority(r: RankedAsset) -> float:
            return (r.drift_pct or 0.0) * w.rank_weight_momentum_5m - r.spread_pct * w.rank_weight_spread

        candidates = [r for r in rows if r.status == "RANKED"]
        if any(r.drift_pct is not None for r in candidates):
            candidates.sort(key=priority, reverse=True)
        else:
            candidates.sort(key=lambda r: r.spread_pct)
        rows.sort(key=priority, reverse=True)
        return candidates[: self.cfg.candidates_per_scan], rows

    def score(self, row: RankedAsset, candles_1m: list[Candle]) -> RankedAsset:
        """Fill in the candle factors and the final score. NO_DATA when history is short."""
        row.move_1m = move(candles_1m, 1)
        row.move_5m = move(candles_1m, 5)
        row.volatility_1m = volatility(candles_1m[-16:])
        row.volume = total_volume(candles_1m)
        row.closes = [c.close for c in candles_1m[-30:]]
        row.candle_ts = [c.ts_ms for c in candles_1m[-30:]]
        if row.move_1m is None or row.move_5m is None or row.volatility_1m is None:
            row.status = "NO_DATA"
            return row
        w = self.cfg
        row.score = round(
            w.rank_weight_momentum_5m * row.move_5m
            + w.rank_weight_momentum_1m * row.move_1m
            - w.rank_weight_spread * row.spread_pct
            - w.rank_weight_volatility * row.volatility_1m,
            5,
        )
        row.status = "CANDIDATE"
        return row

    def signals(self, scored: list[RankedAsset]) -> list[RankedAsset]:
        """Candidates worth sending to Quant, best first. Long-only unless shorts are enabled."""
        out = []
        for row in scored:
            if row.status != "CANDIDATE" or row.score is None:
                continue
            up = row.move_5m >= self.cfg.min_momentum_5m_percent and row.move_1m > 0
            down = self.cfg.allow_short and row.move_5m <= -self.cfg.min_momentum_5m_percent and row.move_1m < 0
            if down:  # mirror the momentum terms so a strong fall ranks like a strong rise
                w = self.cfg
                row.score = round(row.score - 2 * (w.rank_weight_momentum_5m * row.move_5m
                                                   + w.rank_weight_momentum_1m * row.move_1m), 5)
            if up or down:
                row.status = "SIGNAL"
                out.append(row)
        out.sort(key=lambda r: r.score, reverse=True)
        return out


def side_of(row: RankedAsset) -> str:
    return "long" if (row.move_5m or 0) >= 0 else "short"
