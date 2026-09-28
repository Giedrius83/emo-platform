"""QUANT: confirm a Scout signal on fresh 1m and 5m data.

Pure function. Missing or stale data for one instrument is a REJECTED
candidate (the pipeline moves on). Data that contradicts itself is INVALID.
"""

from __future__ import annotations

from .config import TradingConfig
from .market import Candle, DataError, Quote, candles_fresh, pct, volatility
from .messages import QuantPayload, ScoutPayload


def validate(signal: ScoutPayload, quote: Quote | None, c1: list[Candle], c5: list[Candle],
             cfg: TradingConfig, now_ms: int) -> tuple[str, QuantPayload | None, str]:
    if quote is None:
        return "REJECTED", None, "NO_QUOTE"
    if quote.instrument_id != signal.instrument_id:
        return "INVALID", None, "QUOTE_FOR_WRONG_INSTRUMENT"
    if not quote.realtime:
        return "REJECTED", None, "DELAYED_QUOTE"
    age = quote.age_s(now_ms)
    if age > cfg.max_market_data_age_seconds:
        return "REJECTED", None, f"STALE_QUOTE age={age:.1f}s"
    if age < -5:
        return "INVALID", None, f"QUOTE_FROM_FUTURE age={age:.1f}s"
    if quote.spread_pct > cfg.max_spread_percent:
        return "REJECTED", None, f"SPREAD {quote.spread_pct:.3f}% > {cfg.max_spread_percent}%"
    if len(c1) < 6 or len(c5) < 2:
        return "REJECTED", None, "INSUFFICIENT_HISTORY"
    if not candles_fresh(c1, now_ms, cfg.max_candle_age_seconds):
        return "REJECTED", None, "STALE_CANDLES_1M"
    if not candles_fresh(c5, now_ms, 300 + cfg.max_candle_age_seconds):
        return "REJECTED", None, "STALE_CANDLES_5M"
    if any(c.high < c.low for c in c1[-6:] + c5[-2:]):
        raise DataError("candle high below low")

    move_1m = pct(c1[-2].close, quote.mid)
    trend_5m = pct(c5[-2].close, c5[-1].close)
    vol = volatility(c1[-16:]) or 0.0
    body = QuantPayload(
        spread_pct=round(quote.spread_pct, 4),
        quote_age_s=round(age, 2),
        move_1m=round(move_1m, 4),
        trend_5m=round(trend_5m, 4),
        candle_1m_ts=c1[-1].ts_ms,
        candle_5m_ts=c5[-1].ts_ms,
        volatility_1m=round(vol, 4),
    )
    if signal.side == "long":
        agree = move_1m > 0 and trend_5m > 0
    else:
        agree = move_1m < 0 and trend_5m < 0
    if not agree:
        return "REJECTED", body, f"TIMEFRAMES_DISAGREE 1m={move_1m:+.3f}% 5m={trend_5m:+.3f}%"
    return "APPROVED", body, "1m and 5m agree"
