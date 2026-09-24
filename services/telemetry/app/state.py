"""Authoritative in-memory state of the swarm.

The hub keeps exactly one :class:`TerminalState`.  Real bots mutate it through
the ingest API; the simulator mutates it when no real bot is publishing.  Either
way the dashboard reads the same structures, so the UI never needs to know where
a number came from.
"""

from __future__ import annotations

import itertools
import math
import random
import uuid
from collections import deque
from dataclasses import dataclass, field

from .models import (
    ActivityEvent,
    ActivityLevel,
    BalancePoint,
    BotNode,
    ClosedTrade,
    DataSource,
    HandoffEdge,
    NodeStatus,
    Pipeline,
    PipelineStage,
    Portfolio,
    SessionInfo,
    Snapshot,
    SwarmState,
    TailCurve,
    TailDistribution,
    Tick,
    Wallet,
    now_ms,
)

# The eight sub-bots the terminal ships with, in display order.
# The swarm as it runs in Grok Bot. Order is display order.
BOT_ROSTER: list[tuple[str, str, str]] = [
    ("SCOUT", "Finds setups", "scanning markets"),
    ("PLANNER", "Plans the session", "planning the day"),
    ("QUANT", "Scores and approves", "scoring setups"),
    ("GUARD", "Risk check", "checking risk limits"),
    ("TRADER", "Places orders on eToro", "watching open trades"),
    ("MANAGER", "Runs the swarm", "coordinating the swarm"),
    ("ORKA", "Monitors the bots", "checking bot health"),
    ("CODER", "Builds tools", "maintaining tools"),
]

# How work is known to flow. A handoff between any other pair of known bots
# adds its own link the first time it is reported.
HANDOFF_LINKS: list[tuple[str, str]] = [
    ("PLANNER", "SCOUT"),
    ("SCOUT", "QUANT"),
    ("QUANT", "GUARD"),
    ("GUARD", "TRADER"),
    ("TRADER", "MANAGER"),
    ("MANAGER", "SCOUT"),
    ("ORKA", "GUARD"),
    ("ORKA", "MANAGER"),
    ("CODER", "PLANNER"),
]

PIPELINE_STAGES: list[tuple[str, str, list[str]]] = [
    ("SIGNAL", "Signal", ["SCOUT", "PLANNER"]),
    ("STRATEGY", "Strategy", ["QUANT", "GUARD"]),
    ("EXECUTION", "Execution", ["TRADER", "MANAGER"]),
]

TAIL_HORIZONS: list[tuple[str, str, int]] = [
    ("H1", "1m horizon", 1),
    ("H5", "5m horizon", 2),
    ("H15", "15m horizon", 3),
]

SYMBOLS = ["ETH/USDC", "ETH/USDT", "WBTC/ETH", "ARB/ETH", "PEPE/ETH", "SOL/ETH"]

# The equity chart shows one consistent-resolution window: 10 minutes of
# 500ms samples. Seeding the same span means the session clock, the bot uptimes
# and the chart's x-axis all agree from the first frame.
HISTORY_SPAN_S = 600
HISTORY_STEP_S = 0.5
HISTORY_LIMIT = int(HISTORY_SPAN_S / HISTORY_STEP_S) + 40
ACTIVITY_LIMIT = 300
# With a broker feed the chart spans weeks of closed trades plus one live
# sample a minute, so it needs more room than the 10-minute simulated window.
BROKER_HISTORY_LIMIT = 3000
LIVE_SAMPLE_MS = 60_000
# A bot that has said nothing for this long is shown idle, not online.
STALE_AFTER_MS = 15 * 60_000
TAIL_THRESHOLD = -2.5  # percent


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class TerminalState:
    """Everything the dashboard draws, held in one place."""

    eth_usd: float = 3_291.44
    start_equity_usd: float = 24_500.00
    start_balance_eth: float = 0.9127
    balance_eth: float = 1.3383
    realized_usd: float = 3_108.44
    unrealized_usd: float = 1_300.23
    open_positions: int = 3
    fills: int = 148

    session: SessionInfo = field(
        default_factory=lambda: SessionInfo(
            id=f"LRPX-{_new_id().upper()[:8]}",
            started_at=now_ms() - HISTORY_SPAN_S * 1000,
            bots_total=len(BOT_ROSTER),
        )
    )
    bots: dict[str, BotNode] = field(default_factory=dict)
    edges: dict[tuple[str, str], HandoffEdge] = field(default_factory=dict)
    pipeline: Pipeline = field(default_factory=lambda: Pipeline(stages=[], consensus_pct=0.0))
    tails: TailDistribution = field(
        default_factory=lambda: TailDistribution(threshold=TAIL_THRESHOLD, domain=[-8.0, 8.0], curves=[])
    )
    history: deque[BalancePoint] = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    activity: deque[ActivityEvent] = field(default_factory=lambda: deque(maxlen=ACTIVITY_LIMIT))

    source: DataSource = DataSource.SIMULATED
    portfolio: Portfolio | None = None
    swarm_live: bool = False
    source_error: str | None = None

    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _last_live_t: int = 0

    def __post_init__(self) -> None:
        if not self.bots:
            self._seed_bots()
        if not self.edges:
            self._seed_edges()
        if not self.pipeline.stages:
            self._seed_pipeline()
        if not self.tails.curves:
            self.rebuild_tails(vol=1.0, skew=0.0)
        if not self.history:
            self._seed_history()

    # -- seeding -------------------------------------------------------------

    def _seed_bots(self) -> None:
        started = self.session.started_at
        for slot, (bot_id, role, task) in enumerate(BOT_ROSTER, start=1):
            self.bots[bot_id] = BotNode(
                id=bot_id,
                role=role,
                status=NodeStatus.ONLINE,
                last_ping_ms=round(random.uniform(4.0, 28.0), 1),
                last_seen=now_ms(),
                task=task,
                load=round(random.uniform(0.25, 0.8), 3),
                throughput=round(random.uniform(40, 320), 1),
                queue_depth=random.randint(0, 12),
                uptime_s=max(0, (now_ms() - started) // 1000),
                color_slot=slot,
            )
        self.session.bots_connected = self.online_count()

    def _seed_edges(self) -> None:
        for source, target in HANDOFF_LINKS:
            self.edges[(source, target)] = HandoffEdge(
                source=source,
                target=target,
                latency_ms=round(random.uniform(1.8, 22.0), 2),
                throughput=round(random.uniform(8, 90), 1),
                volume=random.randint(200, 4000),
                saturation=round(random.uniform(0.15, 0.7), 3),
            )

    def _seed_pipeline(self) -> None:
        self.pipeline = Pipeline(
            stages=[
                PipelineStage(
                    id=stage_id,
                    label=label,
                    state="STREAMING",
                    members=members,
                    inflight=random.randint(2, 24),
                    latency_ms=round(random.uniform(3, 30), 2),
                    throughput=round(random.uniform(20, 120), 1),
                )
                for stage_id, label, members in PIPELINE_STAGES
            ],
            consensus_pct=round(random.uniform(62, 92), 1),
            votes_for=6,
            votes_against=2,
            decision="ACCUMULATE",
        )

    def _seed_history(self) -> None:
        """Back-fill a plausible equity curve so the chart is never empty.

        A Brownian bridge gives a path that actually wanders but still lands
        exactly on the reported balance and PnL, so the chart and the metrics
        bar can never disagree.
        """
        now = now_ms()
        span_ms = HISTORY_SPAN_S * 1000
        steps = int(HISTORY_SPAN_S / HISTORY_STEP_S)
        pnl_final = self.pnl_usd()
        bal_delta = self.balance_eth - self.start_balance_eth

        walk = [0.0]
        for _ in range(steps):
            walk.append(walk[-1] + random.gauss(0, 1))
        end = walk[-1]

        for i in range(steps + 1):
            frac = i / steps
            bridge = walk[i] - frac * end
            shape = frac + bridge * 0.022
            if i == 0:
                shape = 0.0
            elif i == steps:
                shape = 1.0
            t = now - span_ms + int(frac * span_ms)
            balance = self.start_balance_eth + bal_delta * shape
            self.history.append(
                BalancePoint(
                    t=t,
                    balance=round(max(0.02, balance), 6),
                    pnl=round(pnl_final * shape, 2),
                )
            )
        self.balance_eth = self.history[-1].balance

    # -- derived -------------------------------------------------------------

    def next_seq(self) -> int:
        return next(self._seq)

    def online_count(self) -> int:
        return sum(1 for bot in self.bots.values() if bot.status is NodeStatus.ONLINE)

    def pnl_usd(self) -> float:
        """Session PnL in USD: booked profit plus the mark on the open wallet.

        Deliberately *not* ``wallet delta x price`` - a scalping session books
        most of its profit out of the hot wallet, so wallet delta understates it.
        """
        return round(self.realized_usd + self.unrealized_usd, 2)

    def pnl_pct(self) -> float:
        if self.start_equity_usd <= 0:
            return 0.0
        return round(self.pnl_usd() / self.start_equity_usd * 100, 2)

    def exposure_usd(self) -> float:
        return round(self.balance_eth * self.eth_usd, 2)

    def wallet(self) -> Wallet:
        if self.portfolio is None and self.source is not DataSource.SIMULATED:
            # Broker mode before the first good poll: report nothing rather
            # than let simulated money appear under a "real account" label.
            return Wallet(
                balance_eth=0.0, balance_usd=0.0, eth_usd=0.0, pnl_usd=0.0, pnl_pct=0.0,
                realized_usd=0.0, unrealized_usd=0.0, open_positions=0, fills=0, currency="USD",
            )
        if self.portfolio is not None:
            p = self.portfolio
            total = round(p.realized_pnl + p.unrealized_pnl, 2)
            base = round(p.equity - total, 2)
            return Wallet(
                balance_eth=0.0,
                balance_usd=p.equity,
                eth_usd=0.0,
                pnl_usd=total,
                pnl_pct=round(total / base * 100, 2) if base > 0 else 0.0,
                realized_usd=p.realized_pnl,
                unrealized_usd=p.unrealized_pnl,
                open_positions=len(p.positions),
                fills=p.trades_count,
                exposure_usd=round(p.invested + p.unrealized_pnl, 2),
                start_equity_usd=base,
                currency=p.currency,
                cash_usd=p.cash,
            )
        return Wallet(
            balance_eth=round(self.balance_eth, 4),
            balance_usd=round(self.balance_eth * self.eth_usd, 2),
            eth_usd=round(self.eth_usd, 2),
            pnl_usd=self.pnl_usd(),
            pnl_pct=self.pnl_pct(),
            realized_usd=self.realized_usd,
            unrealized_usd=self.unrealized_usd,
            open_positions=self.open_positions,
            fills=self.fills,
            exposure_usd=self.exposure_usd(),
            start_equity_usd=round(self.start_equity_usd, 2),
        )

    def session_info(self) -> SessionInfo:
        self.session.bots_connected = self.online_count()
        self.session.source = self.source
        self.session.swarm_live = self.swarm_live
        self.session.source_error = self.source_error
        degraded = sum(1 for b in self.bots.values() if b.status is NodeStatus.DEGRADED)
        offline = sum(1 for b in self.bots.values() if b.status is NodeStatus.OFFLINE)
        if offline:
            self.session.network = "DEGRADED"
        elif degraded:
            self.session.network = "PARTIAL"
        else:
            self.session.network = "ACTIVE"
        return self.session

    def swarm(self) -> SwarmState:
        order = {bot_id: i for i, (bot_id, _, _) in enumerate(BOT_ROSTER)}
        return SwarmState(
            bots=sorted(self.bots.values(), key=lambda b: order.get(b.id, 99)),
            edges=list(self.edges.values()),
            pipeline=self.pipeline,
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(
            session=self.session_info(),
            wallet=self.wallet(),
            history=list(self.history),
            swarm=self.swarm(),
            tails=self.tails,
            activity=list(self.activity),
            portfolio=self.portfolio,
        )

    def tick(self) -> Tick:
        if self.portfolio is not None:
            # Broker mode: the chart is driven by portfolio polls, not this clock.
            last = self.history[-1] if self.history else BalancePoint(t=now_ms(), balance=0.0, pnl=0.0)
            return Tick(session=self.session_info(), wallet=self.wallet(), point=last)
        point = BalancePoint(
            t=now_ms(),
            balance=round(self.balance_eth, 6),
            pnl=self.pnl_usd(),
        )
        self.history.append(point)
        return Tick(session=self.session_info(), wallet=self.wallet(), point=point)

    # -- mutation ------------------------------------------------------------

    def log(
        self,
        source: str,
        kind: str,
        message: str,
        *,
        level: ActivityLevel = ActivityLevel.INFO,
        target: str | None = None,
        value: float | None = None,
    ) -> ActivityEvent:
        event = ActivityEvent(
            id=_new_id(),
            t=now_ms(),
            level=level,
            source=source,
            target=target,
            kind=kind,
            message=message,
            value=value,
        )
        self.activity.append(event)
        return event

    # -- real bots ---------------------------------------------------------------

    def add_edge(self, source: str, target: str) -> HandoffEdge:
        edge = HandoffEdge(source=source, target=target, latency_ms=0.0, throughput=0.0, volume=0, saturation=0.0)
        self.edges[(source, target)] = edge
        return edge

    def touch(self, bot_id: str) -> BotNode | None:
        """Record that a bot just did something."""
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        bot.last_seen = now_ms()
        if bot.status in (NodeStatus.IDLE, NodeStatus.OFFLINE):
            bot.status = NodeStatus.ONLINE
        return bot

    def reset_swarm_for_live(self) -> None:
        """Forget the simulation: only bots that report will show as active."""
        for bot in self.bots.values():
            bot.status = NodeStatus.IDLE
            bot.task = "not reporting yet"
            bot.load = 0.0
            bot.throughput = 0.0
            bot.queue_depth = 0
            bot.last_ping_ms = 0.0
            bot.last_seen = 0
        for edge in self.edges.values():
            edge.latency_ms = 0.0
            edge.throughput = 0.0
            edge.saturation = 0.0
            edge.volume = 0
        if self.source is DataSource.SIMULATED:
            self.activity.clear()  # invented trades must not sit beside real bot reports
        self.refresh_live(now_ms())

    def refresh_live(self, now: int) -> None:
        """Derive swarm status from real reports: no randomness, no invention."""
        for bot in self.bots.values():
            bot.uptime_s = 0  # unknown for hosted bots; the tile shows last seen instead
            if bot.status is NodeStatus.ONLINE and now - bot.last_seen > STALE_AFTER_MS:
                bot.status = NodeStatus.IDLE
        for edge in self.edges.values():
            # Throughput decays so a link glows while handoffs flow, then cools.
            edge.throughput = round(edge.throughput * 0.9, 2)
            edge.saturation = min(1.0, edge.throughput / 5.0)

        active = 0
        for stage in self.pipeline.stages:
            members = [self.bots[m] for m in stage.members if m in self.bots]
            live = [m for m in members if m.status in (NodeStatus.ONLINE, NodeStatus.DEGRADED)]
            active += len(live)
            stage.ok = all(m.status is not NodeStatus.OFFLINE for m in members)
            stage.inflight = sum(m.queue_depth for m in members)
            pings = [m.last_ping_ms for m in live if m.last_ping_ms]
            stage.latency_ms = round(sum(pings) / len(pings), 2) if pings else 0.0
            stage.throughput = round(sum(m.throughput for m in live), 1)
            stage.state = "ACTIVE" if live else "IDLE"

        reporting = [b for b in self.bots.values() if b.status in (NodeStatus.ONLINE, NodeStatus.DEGRADED)]
        self.pipeline.votes_for = len(reporting)
        self.pipeline.votes_against = len(self.bots) - len(reporting)
        self.pipeline.consensus_pct = round(100.0 * len(reporting) / max(1, len(self.bots)), 1)
        self.pipeline.decision = "ACTIVE" if reporting else "IDLE"

    # -- broker feed -----------------------------------------------------------

    def use_broker(self, source: DataSource) -> None:
        """Switch from simulated account numbers to a real broker feed."""
        self.source = source
        self.history = deque(maxlen=BROKER_HISTORY_LIMIT)
        # Simulated trades must never sit in the log next to real ones.
        self.activity.clear()

    def apply_portfolio(
        self, portfolio: Portfolio, trades: list[ClosedTrade], *, rebuild: bool
    ) -> tuple[BalancePoint, bool]:
        """Record a broker poll.

        Returns the chart point it produced, and True when that point replaced
        the previous tip rather than being appended, so clients can mirror it.
        """
        self.portfolio = portfolio
        total = round(portfolio.realized_pnl + portfolio.unrealized_pnl, 2)
        now = portfolio.updated_at or now_ms()
        if rebuild:
            self._rebuild_history(portfolio, trades, total, now)
        live = BalancePoint(t=now, balance=portfolio.equity, pnl=total)
        if self.history and self._last_live_t and now - self._last_live_t < LIVE_SAMPLE_MS:
            self.history[-1] = live  # keep the live tip current without flooding
            return live, True
        self.history.append(live)
        self._last_live_t = now
        return live, False

    def _rebuild_history(self, portfolio: Portfolio, trades: list[ClosedTrade], total: float, now: int) -> None:
        """Reconstruct the PnL curve from closed trades.

        Equity at each close is back-calculated from today's equity, which is
        exact as long as nothing was deposited or withdrawn inside the window.
        """
        self.history.clear()
        self._last_live_t = 0
        start_equity = portfolio.equity - total
        window = sorted((t for t in trades if t.closed_at >= portfolio.realized_since), key=lambda t: t.closed_at)
        first = min([t.opened_at for t in window] + [p.opened_at for p in portfolio.positions] + [now - 3_600_000])
        self.history.append(BalancePoint(t=first, balance=round(start_equity, 2), pnl=0.0))
        cumulative = 0.0
        for trade in window:
            cumulative += trade.net_profit
            self.history.append(
                BalancePoint(t=trade.closed_at, balance=round(start_equity + cumulative, 2), pnl=round(cumulative, 2))
            )

    def rebuild_tails(self, *, vol: float, skew: float) -> TailDistribution:
        """Recompute the three horizon densities as a skewed-normal mixture.

        The ridge is a real density: each curve integrates to 1 over the domain,
        so the visible left-tail mass is the number the readout reports.
        """
        lo, hi = -8.0, 8.0
        samples = 121
        xs = [lo + (hi - lo) * i / (samples - 1) for i in range(samples)]
        curves: list[TailCurve] = []

        for idx, (curve_id, label, slot) in enumerate(TAIL_HORIZONS):
            # Longer horizons are wider and carry more of the skew.
            scale = vol * (0.85 + 0.55 * idx)
            alpha = skew * (1.0 + 0.4 * idx)
            mean = 0.12 * (idx + 1) * max(-1.0, min(1.0, skew))
            ys = [_skew_normal_pdf(x, mean, scale, alpha) for x in xs]
            step = (hi - lo) / (samples - 1)
            area = sum(ys) * step or 1.0
            ys = [y / area for y in ys]

            tail_mass = sum(y for x, y in zip(xs, ys) if x <= TAIL_THRESHOLD) * step
            var95 = _quantile(xs, ys, step, 0.05)
            curves.append(
                TailCurve(
                    id=curve_id,
                    label=label,
                    color_slot=slot,
                    points=[[round(x, 3), round(y, 6)] for x, y in zip(xs, ys)],
                    var95=round(var95, 2),
                    tail_prob=round(tail_mass * 100, 2),
                    mean=round(mean, 3),
                )
            )

        self.tails = TailDistribution(threshold=TAIL_THRESHOLD, domain=[lo, hi], curves=curves)
        return self.tails


def _skew_normal_pdf(x: float, loc: float, scale: float, alpha: float) -> float:
    scale = max(scale, 0.2)
    z = (x - loc) / scale
    pdf = math.exp(-0.5 * z * z) / (scale * math.sqrt(2 * math.pi))
    cdf = 0.5 * (1 + math.erf(alpha * z / math.sqrt(2)))
    return 2 * pdf * cdf


def _quantile(xs: list[float], ys: list[float], step: float, q: float) -> float:
    acc = 0.0
    for x, y in zip(xs, ys):
        acc += y * step
        if acc >= q:
            return x
    return xs[-1]
