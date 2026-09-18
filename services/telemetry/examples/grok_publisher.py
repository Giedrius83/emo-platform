#!/usr/bin/env python3
"""Example Grok bot publishing telemetry into the hub.

Run the service, then run this. Within a few seconds the hub's simulator stands
down and the dashboard shows only what this publisher sends - which is how you
verify a real bot fleet is actually wired through.

    python examples/grok_publisher.py --url ws://127.0.0.1:8000/ws/ingest

Pass --http to use the batch REST endpoint instead of the socket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import urllib.request

BOTS = ["SCOUT", "SIGNAL", "QUANT", "VECTOR", "NEXUS", "PULSE", "GUARD", "CORE"]
LINKS = [
    ("SCOUT", "SIGNAL"),
    ("SIGNAL", "QUANT"),
    ("QUANT", "NEXUS"),
    ("NEXUS", "GUARD"),
    ("GUARD", "CORE"),
]
SYMBOLS = ["ETH/USDC", "WBTC/ETH", "ARB/ETH"]


def make_events(rng: random.Random) -> list[dict]:
    """One beat of a fleet: heartbeats, a handoff, and occasionally a fill."""
    events: list[dict] = []
    for bot in BOTS:
        load = rng.uniform(0.15, 0.95)
        events.append(
            {
                "kind": "heartbeat",
                "bot_id": bot,
                "status": "degraded" if load > 0.9 else "online",
                "ping_ms": round(2 + 28 * load, 1),
                "task": f"grok::{bot.lower()} working",
                "load": round(load, 3),
                "throughput": round(40 + 400 * load, 1),
                "queue_depth": rng.randint(0, 18),
            }
        )

    source, target = rng.choice(LINKS)
    events.append(
        {
            "kind": "handoff",
            "source": source,
            "target": target,
            "latency_ms": round(rng.uniform(1.5, 30.0), 2),
            "message": f"grok handoff {source}→{target}",
        }
    )

    if rng.random() < 0.5:
        events.append(
            {
                "kind": "fill",
                "bot_id": "CORE",
                "side": rng.choice(["BUY", "SELL"]),
                "symbol": rng.choice(SYMBOLS),
                "size_eth": round(rng.uniform(0.05, 1.5), 3),
                "price_usd": round(rng.uniform(3200, 3400), 2),
                "pnl_usd": round(rng.gauss(12, 40), 2),
            }
        )

    if rng.random() < 0.35:
        events.append(
            {
                "kind": "signal",
                "bot_id": "QUANT",
                "message": f"grok model flags {rng.choice(SYMBOLS)}",
                "level": "warn" if rng.random() < 0.3 else "info",
                "confidence": round(rng.uniform(0.5, 0.99), 2),
            }
        )
    return events


async def publish_ws(url: str, beats: int, interval: float) -> None:
    try:
        import websockets
    except ImportError:
        sys.exit("pip install websockets, or run with --http")

    rng = random.Random()
    async with websockets.connect(url) as socket:
        for beat in range(beats):
            for event in make_events(rng):
                await socket.send(json.dumps(event))
                ack = json.loads(await socket.recv())
                if not ack.get("ok"):
                    print("rejected:", ack)
            print(f"beat {beat + 1}/{beats} published")
            await asyncio.sleep(interval)


def publish_http(url: str, beats: int, interval: float) -> None:
    import time

    rng = random.Random()
    for beat in range(beats):
        payload = json.dumps({"events": make_events(rng)}).encode()
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            print(f"beat {beat + 1}/{beats}:", json.load(response))
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/ingest")
    parser.add_argument("--http", action="store_true", help="POST to /api/ingest instead of the socket")
    parser.add_argument("--beats", type=int, default=60)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    if args.http:
        url = args.url if args.url.startswith("http") else "http://127.0.0.1:8000/api/ingest"
        publish_http(url, args.beats, args.interval)
    else:
        asyncio.run(publish_ws(args.url, args.beats, args.interval))


if __name__ == "__main__":
    main()
