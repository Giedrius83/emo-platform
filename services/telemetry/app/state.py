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
    HandoffEdge,
    NodeStatus,
    Pipeline,
    PipelineStage,
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
BOT_ROSTER: list[tuple[str, str, str]] = [
    ("SCOUT", "Mempool discovery", "scanning pending pool"),
    ("SIGNAL", "Alpha extraction", "ranking candidates"),
    ("QUANT", "Model inference", "pricing tail risk"),
    ("VECTOR", "Route solver", "solving split route"),
    ("NEXUS", "Consensus arbiter", "collecting votes"),
    ("PULSE", "Market pulse", "tracking depth"),
    ("GUARD", "Risk gate", "enforcing limits"),
    ("CORE", "Execution engine", "submitting bundle"),
]

# Directed links of the handoff mesh: how work actually flows through the swarm.
HANDOFF_LINKS: list[tuple[str, str]] = [
    ("SCOUT", "SIGNAL"),
    ("SCOUT", "PULSE"),
    ("SIGNAL", "QUANT"),
    ("PULSE", "QUANT"),
    ("QUANT", "VECTOR"),
    ("QUANT", "NEXUS"),
    ("VECTOR", "NEXUS"),
    ("NEXUS", "GUARD"),
    ("GUARD", "CORE"),
    ("CORE", "PULSE"),
    ("PULSE", "SIGNAL"),
]

PIPELINE_STAGES: list[tuple[str, str, list[str]]] = [
    ("SIGNAL", "Signal", ["SCOUT", "SIGNAL", "PULSE"]),
    ("STRATEGY", "Strategy", ["QUANT", "VECTOR", "NEXUS"]),
    ("EXECUTION", "Execution", ["GUARD", "CORE"]),
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

    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

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
        )

    def tick(self) -> Tick:
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
