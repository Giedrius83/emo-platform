"""Trader process entry point.

    python -m app.trading run                     start the pipeline (needs TRADING_PIPELINE_ENABLED=true)
    python -m app.trading status                  print halt state, limits and live positions
    python -m app.trading recover --reason TEXT   clear an emergency halt after checking the account

The dashboard never runs this and has no way to call it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from .broker import Broker
from .config import ConfigError, banner, load_config
from .db import Store, now_ms


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its lines carry URLs, not credentials, but add noise


def _status(store: Store) -> int:
    runtime = store.get_state("runtime", {}) or {}
    print(json.dumps({
        "env": store.get_state("env"),
        "halt": store.get_state("halt", {}),
        "mode": runtime.get("mode"),
        "heartbeat_age_s": round((now_ms() - runtime.get("heartbeat", 0)) / 1000, 1) if runtime else None,
        "live_positions": [dict(r) for r in store.live_positions()],
        "unresolved_orders": [dict(r) for r in store.unresolved_orders()],
    }, indent=2, default=str))
    return 0


def _recover(store: Store, reason: str) -> int:
    halt = store.get_state("halt", {}) or {}
    if not halt.get("emergency"):
        print("no emergency halt is set")
        return 0
    unresolved = store.unresolved_orders()
    if unresolved:
        print(f"refusing: {len(unresolved)} order(s) are unresolved. Let the trader reconcile them first.")
        return 2
    store.set_state("halt", {"emergency": False, "recovered_reason": reason, "previous": halt, "at": now_ms()})
    store.risk_event("RECOVERED", "INFO", reason)
    store.record_event(message_id=f"recover_{now_ms()}", ts=now_ms(), signal_id=None, source_bot="MANAGER",
                       target_bot=None, status="INFO", kind="RECOVERED", detail=reason)
    print(f"emergency halt cleared ({halt.get('reason')}): {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.trading")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    recover = sub.add_parser("recover")
    recover.add_argument("--reason", required=True, help="what you checked before clearing the halt")
    args = parser.parse_args(argv)
    _setup_logging()

    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print(f"configuration refused: {exc}", file=sys.stderr)
        return 2
    store = Store(cfg.db_path)
    if args.command == "status":
        return _status(store)
    if args.command == "recover":
        return _recover(store, args.reason)

    print(banner(cfg), flush=True)
    if not cfg.pipeline_enabled:
        print("TRADING_PIPELINE_ENABLED is not true: the trader stays off. Nothing will be traded.", flush=True)
        return 0
    from .manager import Manager

    broker = Broker(cfg.trading_env, cfg.api_key, cfg.user_key, base_url=cfg.api_base)
    manager = Manager(cfg, store, broker)
    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
