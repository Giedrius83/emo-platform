"""Fan-out hub: one producer loop, many dashboard subscribers.

Each connected dashboard gets a bounded queue.  A slow client drops its oldest
frames rather than stalling the producer, which is what you want on a trading
terminal: fresh data beats complete data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .etoro import EtoroFeed
from .models import ActivityEvent, Frame, FrameType, Portfolio
from .simulator import SwarmSimulator
from .state import TerminalState

log = logging.getLogger("telemetry.hub")

TICK_INTERVAL_S = 0.5
SWARM_INTERVAL_S = 1.5
ACTIVITY_INTERVAL_S = 0.9
CLIENT_QUEUE_SIZE = 64


class Subscriber:
    def __init__(self, hub: "TelemetryHub") -> None:
        self._hub = hub
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self.dropped = 0

    def offer(self, frame: Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Shed the oldest frame; a terminal would rather skip than lag.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(frame)

    async def __aenter__(self) -> "Subscriber":
        self._hub.subscribers.add(self)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._hub.subscribers.discard(self)


class TelemetryHub:
    def __init__(self, state: TerminalState | None = None, *, seed: int | None = None) -> None:
        self.state = state or TerminalState()
        self.simulator = SwarmSimulator(self.state, seed=seed)
        self.subscribers: set[Subscriber] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self.feed: EtoroFeed | None = None

    @property
    def broker(self) -> bool:
        """True once a broker feed supplies the account numbers."""
        return self.feed is not None

    def attach_feed(self, feed: EtoroFeed) -> None:
        """Take account numbers from the broker instead of the simulator."""
        self.feed = feed
        self.state.use_broker(feed.source)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._swarm_loop(), name="telemetry-swarm"),
        ]
        if self.feed is not None:
            self._tasks.append(asyncio.create_task(self.feed.run(), name="telemetry-etoro"))
        else:
            # Only the simulated account needs a market clock and invented trades.
            self._tasks.append(asyncio.create_task(self._market_loop(), name="telemetry-market"))
            self._tasks.append(asyncio.create_task(self._activity_loop(), name="telemetry-activity"))
        log.info("telemetry hub started: %d loops", len(self._tasks))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        log.info("telemetry hub stopped")

    # -- publishing ----------------------------------------------------------

    def frame(self, kind: FrameType, payload: dict[str, Any]) -> Frame:
        return Frame(type=kind, seq=self.state.next_seq(), payload=payload)

    def broadcast(self, frame: Frame) -> None:
        for subscriber in list(self.subscribers):
            subscriber.offer(frame)

    def snapshot_frame(self) -> Frame:
        return self.frame(FrameType.SNAPSHOT, self.state.snapshot().model_dump(mode="json"))

    # -- loops ---------------------------------------------------------------

    async def _market_loop(self) -> None:
        while True:
            try:
                if not self.simulator.dormant:
                    self.simulator.step_market(TICK_INTERVAL_S)
                self.broadcast(self.frame(FrameType.TICK, self.state.tick().model_dump(mode="json")))
            except Exception:  # keep the terminal alive through a bad beat
                log.exception("market loop beat failed")
            await asyncio.sleep(TICK_INTERVAL_S)

    async def _swarm_loop(self) -> None:
        while True:
            try:
                self.state.swarm_live = self.simulator.dormant
                self.state.source_error = self.feed.last_error if self.feed else None
                if not self.simulator.dormant:
                    self.simulator.step_swarm()
                swarm = self.state.swarm().model_dump(mode="json")
                swarm["session"] = self.state.session_info().model_dump(mode="json")
                self.broadcast(self.frame(FrameType.SWARM, swarm))
                if not self.broker:
                    # The tail model is simulated; never show it beside real money.
                    self.broadcast(self.frame(FrameType.TAILS, self.state.tails.model_dump(mode="json")))
            except Exception:
                log.exception("swarm loop beat failed")
            await asyncio.sleep(SWARM_INTERVAL_S)

    async def _activity_loop(self) -> None:
        while True:
            try:
                if not self.simulator.dormant:
                    events = self.simulator.emit_activity()
                    if events:
                        self.publish_activity(events)
            except Exception:
                log.exception("activity loop beat failed")
            await asyncio.sleep(ACTIVITY_INTERVAL_S)

    def publish_portfolio(self, portfolio: Portfolio, events: list[ActivityEvent], rebuilt: bool) -> None:
        """Called by the broker feed after every successful poll."""
        trades = self.feed.trades if self.feed else portfolio.trades
        point, replaced = self.state.apply_portfolio(portfolio, trades, rebuild=rebuilt)
        payload = {
            "portfolio": portfolio.model_dump(mode="json"),
            "wallet": self.state.wallet().model_dump(mode="json"),
            "session": self.state.session_info().model_dump(mode="json"),
            "point": point.model_dump(mode="json"),
            "replace": replaced,
        }
        if rebuilt:
            payload["history"] = [p.model_dump(mode="json") for p in self.state.history]
        self.broadcast(self.frame(FrameType.PORTFOLIO, payload))
        if events:
            self.publish_activity(events)

    def publish_activity(self, events: list) -> None:
        payload = {"events": [event.model_dump(mode="json") for event in events]}
        self.broadcast(self.frame(FrameType.ACTIVITY, payload))
