"""Translate inbound Grok-bot events into state mutations."""

from __future__ import annotations

import time

from .models import (
    ActivityEvent,
    ActivityLevel,
    BotFill,
    BotHandoff,
    BotHeartbeat,
    BotSignal,
    NodeStatus,
)
from .state import TerminalState


def apply_event(state: TerminalState, event) -> list[ActivityEvent]:
    """Apply one ingest event and return the activity lines it produced."""
    if isinstance(event, BotHeartbeat):
        return _apply_heartbeat(state, event)
    if isinstance(event, BotHandoff):
        return _apply_handoff(state, event)
    if isinstance(event, BotFill):
        return _apply_fill(state, event)
    if isinstance(event, BotSignal):
        return _apply_signal(state, event)
    return []


def _apply_heartbeat(state: TerminalState, event: BotHeartbeat) -> list[ActivityEvent]:
    bot = state.bots.get(event.bot_id)
    if bot is None:
        return []
    previous = bot.status
    bot.status = event.status
    bot.last_seen = int(time.time() * 1000)
    # Grok bots rarely know their latency or load; keep what we have.
    if event.ping_ms is not None:
        bot.last_ping_ms = round(event.ping_ms, 1)
    if event.load is not None:
        bot.load = event.load
    if event.throughput is not None:
        bot.throughput = event.throughput
    if event.queue_depth is not None:
        bot.queue_depth = event.queue_depth
    if event.task:
        bot.task = event.task
    bot.uptime_s = max(0, (bot.last_seen - state.session.started_at) // 1000)

    if previous is not bot.status:
        level = {
            NodeStatus.ONLINE: ActivityLevel.SUCCESS,
            NodeStatus.DEGRADED: ActivityLevel.WARN,
            NodeStatus.OFFLINE: ActivityLevel.CRITICAL,
            NodeStatus.IDLE: ActivityLevel.INFO,
        }[bot.status]
        return [
            state.log(
                bot.id,
                "NODE",
                f"{previous.value} → {bot.status.value}" + (f" · {bot.task}" if event.task else ""),
                level=level,
                value=bot.last_ping_ms,
            )
        ]
    return []


def _apply_handoff(state: TerminalState, event: BotHandoff) -> list[ActivityEvent]:
    key = (event.source, event.target)
    edge = state.edges.get(key)
    if edge is None:
        if event.source not in state.bots or event.target not in state.bots:
            return []
        edge = state.add_edge(event.source, event.target)
    # A handoff proves both ends are alive right now.
    now = int(time.time() * 1000)
    for name in key:
        bot = state.bots[name]
        bot.last_seen = now
        if bot.status is not NodeStatus.DEGRADED:
            bot.status = NodeStatus.ONLINE
    # Exponential moving average keeps a single outlier from re-drawing the mesh.
    edge.latency_ms = round(edge.latency_ms * 0.7 + event.latency_ms * 0.3, 2) if edge.volume else event.latency_ms
    edge.volume += 1
    edge.throughput = round(edge.throughput * 0.9 + 1.0, 1)
    sender = state.bots[event.source]
    sender.task = (f"→ {event.target}: {event.message}" if event.message else f"handed work to {event.target}")[:80]
    message = event.message or f"handed work to {event.target}"
    return [
        state.log(
            event.source,
            "HANDOFF",
            message,
            target=event.target,
            value=event.latency_ms,
        )
    ]


def _apply_fill(state: TerminalState, event: BotFill) -> list[ActivityEvent]:
    state.touch(event.bot_id)
    state.fills += 1
    if event.price_usd > 0:
        state.eth_usd = round(state.eth_usd * 0.9 + event.price_usd * 0.1, 2)
    if event.pnl_usd:
        state.realized_usd = round(state.realized_usd + event.pnl_usd, 2)
        if state.eth_usd > 0:
            state.balance_eth = max(0.0, state.balance_eth + event.pnl_usd / state.eth_usd)
    return [
        state.log(
            event.bot_id,
            "ORDER",
            f"{event.side} {event.size_eth:.3f} {event.symbol} @ ${event.price_usd:,.2f}",
            level=ActivityLevel.SUCCESS if event.pnl_usd >= 0 else ActivityLevel.WARN,
            value=event.pnl_usd,
        )
    ]


def _apply_signal(state: TerminalState, event: BotSignal) -> list[ActivityEvent]:
    bot = state.touch(event.bot_id)
    if bot is not None and event.message:
        bot.task = event.message[:80]
    return [
        state.log(
            event.bot_id,
            "SIGNAL",
            event.message,
            level=event.level,
            value=event.confidence or None,
        )
    ]
