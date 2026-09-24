"""The eToro feed, tested against real (anonymized) API responses."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.etoro import (
    EtoroClient,
    EtoroError,
    EtoroFeed,
    build_portfolio,
    parse_instruments,
    parse_positions,
    parse_trades,
)
from app.hub import TelemetryHub
from app.models import DataSource, FrameType
from app.state import TerminalState

FIX = Path(__file__).parent / "fixtures"
PNL = json.loads((FIX / "etoro_pnl.json").read_text())
HISTORY = json.loads((FIX / "etoro_history.json").read_text())
INSTRUMENTS = json.loads((FIX / "etoro_instruments.json").read_text())


# --- parsing ---------------------------------------------------------------------


def test_instruments_resolve_symbols_and_logos() -> None:
    inst = parse_instruments(INSTRUMENTS)
    assert inst[100003].symbol == "XRP"
    assert inst[100040].symbol == "LINK" and inst[100040].name == "Chainlink"
    assert inst[100003].logo.endswith("50x50.png")


def test_open_position_matches_the_account() -> None:
    [pos] = parse_positions(PNL, parse_instruments(INSTRUMENTS))
    assert pos.symbol == "XRP" and pos.direction == "long"
    assert pos.invested == 40.0
    assert pos.pnl == -3.16
    assert pos.value == 36.84
    assert pos.pnl_pct == pytest.approx(-7.9, abs=0.01)
    assert pos.current_rate == 1.4553
    assert pos.stop_loss == 1.4 and pos.take_profit == 1.72457


def test_unknown_instrument_still_renders() -> None:
    trades = parse_trades(HISTORY, parse_instruments(INSTRUMENTS))
    polkadot = next(t for t in trades if t.instrument_id == 100037)
    assert polkadot.symbol == "#100037"


def test_placeholder_stop_loss_and_take_profit_are_hidden() -> None:
    """eToro sends 0.00001 / 131.68 when a trade had no real SL or TP."""
    raw = [dict(HISTORY[-1])]
    [trade] = parse_trades(raw, {})
    assert trade.open_rate == 1.3039
    # Closed trades carry no SL/TP fields in the model; check the position path.
    pnl = copy.deepcopy(PNL)
    row = pnl["clientPortfolio"]["positions"][0]
    row["stopLossRate"], row["takeProfitRate"] = 0.00001, 131.6838
    [pos] = parse_positions(pnl, {})
    assert pos.stop_loss is None and pos.take_profit is None


def test_disabled_flags_hide_protection() -> None:
    pnl = copy.deepcopy(PNL)
    pnl["clientPortfolio"]["positions"][0]["isNoStopLoss"] = True
    [pos] = parse_positions(pnl, {})
    assert pos.stop_loss is None and pos.take_profit == 1.72457


def test_trades_are_newest_first_with_real_profit() -> None:
    trades = parse_trades(HISTORY, parse_instruments(INSTRUMENTS))
    assert [t.closed_at for t in trades] == sorted((t.closed_at for t in trades), reverse=True)
    assert trades[0].net_profit == -0.05
    assert sum(t.net_profit for t in trades) == pytest.approx(2.00, abs=0.001)


def test_portfolio_totals_add_up() -> None:
    inst = parse_instruments(INSTRUMENTS)
    trades = parse_trades(HISTORY, inst)
    p = build_portfolio(DataSource.ETORO_REAL, PNL, trades, inst, realized_since=0, now_ms=1)
    assert p.cash == 3.23 and p.invested == 40.0 and p.unrealized_pnl == -3.16
    assert p.equity == pytest.approx(3.23 + 40.0 - 3.16)
    assert p.realized_pnl == pytest.approx(2.00, abs=0.001)
    assert p.trades_count == 6 and p.wins == 2


def test_realized_window_excludes_older_trades() -> None:
    trades = parse_trades(HISTORY, {})
    cutoff = trades[2].closed_at
    p = build_portfolio(DataSource.ETORO_REAL, PNL, trades, {}, realized_since=cutoff, now_ms=1)
    assert p.trades_count == 3
    assert p.realized_pnl == pytest.approx(-0.05 + 0.02 - 0.05)


# --- the feed over a fake eToro ------------------------------------------------------


class FakeEtoro:
    """Serves the fixtures and records what was asked for."""

    def __init__(self) -> None:
        self.pnl = copy.deepcopy(PNL)
        self.history = copy.deepcopy(HISTORY)
        self.calls: list[str] = []
        self.headers: list[httpx.Headers] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        self.headers.append(request.headers)
        assert request.method == "GET", "the feed must never send anything but GET"
        if self.fail_with:
            return httpx.Response(self.fail_with, headers={"Retry-After": "7"}, text="nope")
        path = request.url.path
        if path.endswith("/pnl"):
            return httpx.Response(200, json=self.pnl)
        if "history" in path:
            return httpx.Response(200, json=self.history if request.url.params.get("page") == "1" else [])
        if path.endswith("/instruments"):
            return httpx.Response(200, json=INSTRUMENTS)
        return httpx.Response(404)


def make_feed(fake: FakeEtoro, account: str = "real") -> tuple[EtoroFeed, TerminalState, list]:
    state = TerminalState()
    published: list = []
    client = EtoroClient("app-key", "user-key", transport=httpx.MockTransport(fake.handler))
    feed = EtoroFeed(
        client=client,
        account=account,
        make_event=state.log,
        publish=lambda p, e, r: published.append((p, e, r)),
    )
    state.use_broker(feed.source)
    return feed, state, published


def run(coro):
    return asyncio.run(coro)


def test_requests_carry_both_keys_and_a_request_id() -> None:
    fake = FakeEtoro()
    feed, _, _ = make_feed(fake)
    run(feed.poll_once(0.0))
    h = fake.headers[0]
    assert h["x-api-key"] == "app-key" and h["x-user-key"] == "user-key"
    assert len(h["x-request-id"]) == 36


def test_demo_account_uses_demo_routes() -> None:
    fake = FakeEtoro()
    feed, _, _ = make_feed(fake, account="demo")
    run(feed.poll_once(0.0))
    assert "/api/v1/trading/info/demo/pnl" in fake.calls
    assert "/api/v1/trading/info/trade/demo/history" in fake.calls
    assert feed.source is DataSource.ETORO_DEMO


def test_first_poll_replays_history_into_the_log() -> None:
    fake = FakeEtoro()
    feed, _, published = make_feed(fake)
    run(feed.poll_once(0.0))
    portfolio, events, rebuilt = published[0]
    assert rebuilt is True
    kinds = [e.kind for e in events]
    assert kinds.count("CLOSE") == 6 and kinds.count("OPEN") == 1
    # Replayed lines keep their real timestamps, oldest first.
    assert [e.t for e in events if e.kind == "CLOSE"] == sorted(e.t for e in events if e.kind == "CLOSE")
    assert "BUY XRP $40.00 @ 1.58" in next(e.message for e in events if e.kind == "OPEN")


def test_quiet_poll_emits_nothing_and_skips_history() -> None:
    fake = FakeEtoro()
    feed, _, published = make_feed(fake)
    run(feed.poll_once(0.0))
    fake.calls.clear()
    run(feed.poll_once(5.0))
    assert published[-1][1] == [] and published[-1][2] is False
    assert not any("history" in c for c in fake.calls), "history is rate-limited to once a minute"
    assert not any("instruments" in c for c in fake.calls), "instrument names are cached"


def test_a_close_is_reported_on_the_very_next_poll() -> None:
    fake = FakeEtoro()
    feed, _, published = make_feed(fake)
    run(feed.poll_once(0.0))

    # The XRP position closes: it leaves the portfolio and lands in history.
    closed = fake.pnl["clientPortfolio"]["positions"].pop()
    fake.pnl["clientPortfolio"]["credit"] = 40.5
    fake.pnl["clientPortfolio"]["unrealizedPnL"] = 0.0
    fake.history.insert(0, {**HISTORY[0], "positionId": closed["positionID"], "netProfit": 1.27,
                            "closeTimestamp": "2026-09-24T10:00:00Z"})
    fake.calls.clear()
    run(feed.poll_once(5.0))  # well inside the 60s history interval

    assert any("history" in c for c in fake.calls), "a vanished position forces a history fetch"
    [event] = published[-1][1]
    assert event.kind == "CLOSE" and event.value == 1.27 and event.level.value == "success"
    assert published[-1][0].positions == []


def test_a_new_position_is_reported() -> None:
    fake = FakeEtoro()
    feed, _, published = make_feed(fake)
    run(feed.poll_once(0.0))
    extra = copy.deepcopy(fake.pnl["clientPortfolio"]["positions"][0])
    extra.update(positionID=999, instrumentID=100040, openRate=12.5, amount=15.0, isBuy=False,
                 leverage=2, unrealizedPnL={"pnL": 0.1, "closeRate": 12.4})
    fake.pnl["clientPortfolio"]["positions"].append(extra)
    run(feed.poll_once(5.0))
    [event] = published[-1][1]
    assert event.kind == "OPEN" and event.message.startswith("SELL LINK x2 $15.00 @ 12.5")


def test_errors_surface_with_retry_after() -> None:
    fake = FakeEtoro()
    fake.fail_with = 429
    feed, _, _ = make_feed(fake)
    with pytest.raises(EtoroError) as err:
        run(feed.poll_once(0.0))
    assert err.value.status == 429 and err.value.retry_after == 7


# --- state and hub in broker mode ------------------------------------------------------


def test_chart_rebuilds_from_trades_and_ends_at_live_equity() -> None:
    fake = FakeEtoro()
    feed, state, published = make_feed(fake)
    run(feed.poll_once(0.0))
    portfolio, _, _ = published[0]
    state.apply_portfolio(portfolio, feed.trades, rebuild=True)

    history = list(state.history)
    assert history[0].pnl == 0.0
    assert [p.t for p in history] == sorted(p.t for p in history)
    total = round(portfolio.realized_pnl + portfolio.unrealized_pnl, 2)
    assert history[-1].pnl == total and history[-1].balance == portfolio.equity
    # Equity and PnL move together on every point: equity - pnl is constant.
    assert len({round(p.balance - p.pnl, 2) for p in history}) == 1


def test_live_samples_are_throttled_to_one_a_minute() -> None:
    fake = FakeEtoro()
    feed, state, published = make_feed(fake)
    run(feed.poll_once(0.0))
    portfolio = published[0][0]
    state.apply_portfolio(portfolio, feed.trades, rebuild=True)
    size = len(state.history)
    for offset in (10_000, 20_000, 30_000):
        _, replaced = state.apply_portfolio(
            portfolio.model_copy(update={"updated_at": portfolio.updated_at + offset}), feed.trades, rebuild=False
        )
        assert replaced is True
    assert len(state.history) == size, "polls inside a minute update the tip in place"
    _, replaced = state.apply_portfolio(
        portfolio.model_copy(update={"updated_at": portfolio.updated_at + 61_000}), feed.trades, rebuild=False
    )
    assert replaced is False and len(state.history) == size + 1


def test_wallet_reports_real_dollars() -> None:
    fake = FakeEtoro()
    feed, state, published = make_feed(fake)
    run(feed.poll_once(0.0))
    state.apply_portfolio(published[0][0], feed.trades, rebuild=True)
    w = state.wallet()
    assert w.currency == "USD"
    assert w.balance_usd == pytest.approx(40.07, abs=0.001)
    assert w.cash_usd == 3.23 and w.open_positions == 1
    assert w.pnl_usd == pytest.approx(2.00 - 3.16, abs=0.001)


def test_broker_mode_never_mixes_in_simulated_trades() -> None:
    state = TerminalState()
    assert state.activity == state.activity  # seeded sim state may hold lines
    state.log("SCOUT", "ORDER", "simulated fill")
    state.use_broker(DataSource.ETORO_REAL)
    assert list(state.activity) == []
    assert state.session_info().source is DataSource.ETORO_REAL


def test_hub_publishes_portfolio_frames_and_no_tail_model() -> None:
    fake = FakeEtoro()
    hub = TelemetryHub()
    client = EtoroClient("a", "u", transport=httpx.MockTransport(fake.handler))
    feed = EtoroFeed(client=client, account="real", make_event=hub.state.log, publish=hub.publish_portfolio,
                     poll_s=0.05)
    hub.attach_feed(feed)

    async def scenario():
        from app.hub import Subscriber
        async with Subscriber(hub) as sub:
            await hub.start()
            await asyncio.sleep(1.8)
            await hub.stop()
            frames = []
            while not sub.queue.empty():
                frames.append(sub.queue.get_nowait())
            return frames

    frames = asyncio.run(scenario())
    kinds = {f.type for f in frames}
    assert FrameType.PORTFOLIO in kinds and FrameType.SWARM in kinds
    assert FrameType.TAILS not in kinds, "the simulated tail model is hidden on real data"
    assert FrameType.TICK not in kinds, "no simulated market clock in broker mode"
    first = next(f for f in frames if f.type is FrameType.PORTFOLIO)
    assert "history" in first.payload and first.payload["wallet"]["currency"] == "USD"
    later = [f for f in frames if f.type is FrameType.PORTFOLIO][1:]
    assert later and all("history" not in f.payload for f in later)


def test_broker_mode_shows_no_money_before_the_first_good_poll() -> None:
    state = TerminalState()
    state.use_broker(DataSource.ETORO_REAL)
    state.source_error = "eToro API 401: bad keys"
    w = state.wallet()
    assert w.currency == "USD" and w.balance_usd == 0.0 and w.pnl_usd == 0.0
    assert w.balance_eth == 0.0, "simulated ETH must never leak into broker mode"
    assert state.session_info().source_error == "eToro API 401: bad keys"
