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
    bot.last_ping_ms = round(event.ping_ms, 1)
    bot.last_seen = int(time.time() * 1000)
    bot.load = event.load
    bot.throughput = event.throughput
    bot.queue_depth = event.queue_depth
    if event.task:
        bot.task = event.task
    bot.uptime_s = max(0, (bot.last_seen - state.session.started_at) // 1000)

    if previous is not bot.status:
        level = {
            NodeStatus.ONLINE: ActivityLevel.SUCCESS,
            NodeStatus.DEGRADED: ActivityLevel.WARN,
            NodeStatus.OFFLINE: ActivityLevel.CRITICAL,
        }[bot.status]
        return [
            state.log(
                bot.id,
                "NODE",
                f"{previous.value} → {bot.status.value} · ping {bot.last_ping_ms:.1f}ms",
                level=level,
                value=bot.last_ping_ms,
            )
        ]
    return []


def _apply_handoff(state: TerminalState, event: BotHandoff) -> list[ActivityEvent]:
    key = (event.source, event.target)
    edge = state.edges.get(key)
    if edge is None:
        return []
    # Exponential moving average keeps a single outlier from re-drawing the mesh.
    edge.latency_ms = round(edge.latency_ms * 0.7 + event.latency_ms * 0.3, 2)
    edge.volume += 1
    edge.throughput = round(edge.throughput * 0.9 + 1.0, 1)
    message = event.message or f"handoff accepted → {event.target} @ {event.latency_ms:.1f}ms"
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
    return [
        state.log(
            event.bot_id,
            "SIGNAL",
            event.message,
            level=event.level,
            value=event.confidence or None,
        )
    ]
