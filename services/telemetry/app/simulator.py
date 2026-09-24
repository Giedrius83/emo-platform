"""Synthetic swarm driver.

The hub always has something to show.  When real Grok bots are publishing into
``/ingest`` the simulator stands down automatically; the moment the last
publisher goes quiet for ``IDLE_TAKEOVER_S`` it resumes, so a demo and a live
deployment run the same code path.
"""

from __future__ import annotations

import math
import random
import time

from .models import ActivityLevel, NodeStatus
from .state import SYMBOLS, TerminalState

IDLE_TAKEOVER_S = 6.0

_ORDER_VERBS = [
    "routed limit order",
    "swept resting bid",
    "posted maker quote",
    "cancelled stale quote",
    "split fill across venues",
    "rebalanced inventory",
]

_HANDOFF_VERBS = [
    "handoff accepted",
    "context transferred",
    "lease renewed",
    "workload rebalanced",
    "checkpoint replicated",
]


class SwarmSimulator:
    """Mutates a :class:`TerminalState` into a plausible next instant."""

    def __init__(self, state: TerminalState, *, seed: int | None = None) -> None:
        self.state = state
        self.rng = random.Random(seed)
        self.phase = 0.0
        self.vol = 1.0
        self.skew = -0.25
        self._last_external_event = 0.0
        self.live = False

    # -- external publishers -------------------------------------------------

    def note_external_event(self) -> None:
        self._last_external_event = time.monotonic()
        self.live = True

    @property
    def dormant(self) -> bool:
        """True once a real bot has reported; the simulator never resumes."""
        return self.live

    # -- fast loop (price + equity) ------------------------------------------

    def step_market(self, dt: float) -> None:
        """Advance price and wallet balance by ``dt`` seconds."""
        self.phase += dt
        s = self.state

        # Regime-switching volatility keeps the tape from looking metronomic.
        self.vol = max(0.45, min(2.6, self.vol + self.rng.gauss(0, 0.05) - (self.vol - 1.0) * 0.02))
        self.skew = max(-1.4, min(1.4, self.skew + self.rng.gauss(0, 0.06) - self.skew * 0.03))

        drift = 0.00018 * math.sin(self.phase / 41.0) + 0.00009
        shock = self.rng.gauss(0, 0.0013 * self.vol)

        # Price first, then mark the open wallet against the move it just made.
        previous_price = s.eth_usd
        s.eth_usd = round(max(120.0, s.eth_usd * (1 + drift * 0.2 + shock * 0.35)), 2)
        move = s.eth_usd - previous_price
        s.unrealized_usd = round(s.unrealized_usd + s.balance_eth * move, 2)

        # Scalping books small profits continuously; edge scales with volatility.
        booked = self.rng.gauss(1.15 * self.vol, 3.4 * self.vol)
        s.realized_usd = round(s.realized_usd + booked, 2)
        if s.eth_usd > 0:
            s.balance_eth = max(0.02, s.balance_eth + booked / s.eth_usd)

    # -- slow loop (swarm telemetry) -----------------------------------------

    def step_swarm(self) -> None:
        s = self.state
        load_bias = min(1.0, self.vol / 2.2)

        for bot in s.bots.values():
            bot.load = _clamp(bot.load + self.rng.gauss(0, 0.06) + (load_bias - bot.load) * 0.08, 0.04, 0.99)
            base_ping = 3.0 + 26.0 * bot.load
            bot.last_ping_ms = round(max(0.4, base_ping + self.rng.gauss(0, 2.4)), 1)
            bot.throughput = round(max(2.0, 60 + 420 * bot.load + self.rng.gauss(0, 18)), 1)
            bot.queue_depth = max(0, int(bot.queue_depth + self.rng.gauss(0, 2.2) + (bot.load - 0.6) * 6))
            bot.uptime_s = max(0, (int(time.time() * 1000) - s.session.started_at) // 1000)
            bot.last_seen = int(time.time() * 1000)
            bot.task = self._task_for(bot.id, bot.load)
            bot.status = self._status_for(bot)

        for edge in s.edges.values():
            src = s.bots[edge.source]
            dst = s.bots[edge.target]
            if src.status is NodeStatus.OFFLINE or dst.status is NodeStatus.OFFLINE:
                edge.throughput = round(max(0.0, edge.throughput * 0.4), 1)
                edge.saturation = _clamp(edge.saturation * 0.6, 0.0, 1.0)
                edge.latency_ms = round(min(240.0, edge.latency_ms * 1.4 + 6), 2)
                continue
            link_load = (src.load + dst.load) / 2
            edge.latency_ms = round(max(0.4, 1.5 + 24 * link_load + self.rng.gauss(0, 2.0)), 2)
            edge.throughput = round(max(0.5, 12 + 110 * link_load + self.rng.gauss(0, 7)), 1)
            edge.saturation = _clamp(link_load + self.rng.gauss(0, 0.05), 0.0, 1.0)
            edge.volume += max(0, int(edge.throughput * 0.9))

        self._step_pipeline()
        s.rebuild_tails(vol=self.vol, skew=self.skew)

    def _status_for(self, bot) -> NodeStatus:
        roll = self.rng.random()
        if bot.status is NodeStatus.OFFLINE:
            return NodeStatus.ONLINE if roll < 0.55 else NodeStatus.OFFLINE
        if bot.load > 0.93 or bot.last_ping_ms > 34:
            return NodeStatus.DEGRADED
        if roll < 0.004:
            return NodeStatus.OFFLINE
        if bot.status is NodeStatus.DEGRADED and roll < 0.6:
            return NodeStatus.ONLINE
        return NodeStatus.ONLINE if bot.status is NodeStatus.ONLINE else bot.status

    def _task_for(self, bot_id: str, load: float) -> str:
        pool = {
            "SCOUT": ["scanning markets", "ranking movers", "reading X sentiment"],
            "PLANNER": ["planning the day", "setting watchlist", "reviewing results"],
            "QUANT": ["scoring setups", "checking indicators", "sizing entries"],
            "GUARD": ["checking risk limits", "reviewing exposure", "vetting stop-loss"],
            "TRADER": ["watching open trades", "placing order", "awaiting approval"],
            "MANAGER": ["coordinating the swarm", "reviewing cash", "assigning work"],
            "ORKA": ["checking bot health", "restarting a pass", "reporting status"],
            "CODER": ["maintaining tools", "fixing a script", "testing a tool"],
        }.get(bot_id, ["working"])
        if load > 0.88:
            return f"{pool[0]} (saturated)"
        return self.rng.choice(pool)

    def _step_pipeline(self) -> None:
        s = self.state
        for stage in s.pipeline.stages:
            members = [s.bots[m] for m in stage.members if m in s.bots]
            healthy = [m for m in members if m.status is not NodeStatus.OFFLINE]
            load = sum(m.load for m in healthy) / len(healthy) if healthy else 0.0
            stage.inflight = max(0, int(4 + 28 * load + self.rng.gauss(0, 2)))
            stage.latency_ms = round(max(0.5, 2 + 26 * load + self.rng.gauss(0, 1.8)), 2)
            stage.throughput = round(max(1.0, 18 + 130 * load + self.rng.gauss(0, 8)), 1)
            stage.ok = len(healthy) == len(members)
            if not healthy:
                stage.state = "STALLED"
            elif not stage.ok:
                stage.state = "PARTIAL"
            elif load > 0.9:
                stage.state = "BACKPRESSURE"
            else:
                stage.state = "STREAMING"

        voters = [b for b in s.bots.values() if b.status is not NodeStatus.OFFLINE]
        votes_for = sum(1 for b in voters if b.load < 0.88)
        s.pipeline.votes_for = votes_for
        s.pipeline.votes_against = len(s.bots) - votes_for
        target = 100.0 * votes_for / max(1, len(s.bots))
        # Ease toward the target so the gauge sweeps instead of snapping.
        s.pipeline.consensus_pct = round(
            _clamp(s.pipeline.consensus_pct + (target - s.pipeline.consensus_pct) * 0.35 + self.rng.gauss(0, 1.2), 0, 100),
            1,
        )
        if s.pipeline.consensus_pct >= s.pipeline.quorum_pct:
            s.pipeline.decision = "ACCUMULATE" if self.skew > -0.2 else "SCALE OUT"
        else:
            s.pipeline.decision = "HOLD"

    # -- activity feed -------------------------------------------------------

    def emit_activity(self) -> list:
        """Produce zero or more log lines for this beat."""
        s = self.state
        events = []
        online = [b for b in s.bots.values() if b.status is not NodeStatus.OFFLINE]
        if not online:
            return events

        for _ in range(self.rng.randint(1, 3)):
            roll = self.rng.random()
            if roll < 0.42:
                edge = self.rng.choice(list(s.edges.values()))
                events.append(
                    s.log(
                        edge.source,
                        "HANDOFF",
                        f"{self.rng.choice(_HANDOFF_VERBS)} → {edge.target} @ {edge.latency_ms:.1f}ms",
                        target=edge.target,
                        value=edge.latency_ms,
                    )
                )
            elif roll < 0.74:
                bot = self.rng.choice(online)
                symbol = self.rng.choice(SYMBOLS)
                size = round(self.rng.uniform(0.02, 1.4), 3)
                pnl = round(self.rng.gauss(18, 46), 2)
                s.fills += 1
                events.append(
                    s.log(
                        bot.id,
                        "ORDER",
                        f"{self.rng.choice(_ORDER_VERBS)} {size:.3f} {symbol} @ ${s.eth_usd:,.2f}",
                        level=ActivityLevel.SUCCESS if pnl >= 0 else ActivityLevel.WARN,
                        value=pnl,
                    )
                )
            elif roll < 0.9:
                bot = self.rng.choice(online)
                events.append(
                    s.log(
                        bot.id,
                        "SIGNAL",
                        f"confidence {self.rng.uniform(0.51, 0.99):.2f} on {self.rng.choice(SYMBOLS)}",
                        value=round(self.rng.uniform(0.51, 0.99), 2),
                    )
                )
            else:
                degraded = [b for b in s.bots.values() if b.status is not NodeStatus.ONLINE]
                if degraded:
                    bot = self.rng.choice(degraded)
                    level = ActivityLevel.CRITICAL if bot.status is NodeStatus.OFFLINE else ActivityLevel.WARN
                    events.append(
                        s.log(
                            bot.id,
                            "NODE",
                            f"node {bot.status.value} · ping {bot.last_ping_ms:.1f}ms · queue {bot.queue_depth}",
                            level=level,
                            value=bot.last_ping_ms,
                        )
                    )
                else:
                    events.append(
                        s.log(
                            "MANAGER",
                            "CONSENSUS",
                            f"quorum {s.pipeline.consensus_pct:.1f}% → {s.pipeline.decision}",
                            level=ActivityLevel.SUCCESS,
                            value=s.pipeline.consensus_pct,
                        )
                    )
        return events


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
