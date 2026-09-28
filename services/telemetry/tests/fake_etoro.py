"""A stateful stand-in for the eToro Public API, shaped after the route specs.

It keeps a cash balance, positions, orders by id and by x-request-id, closed
trade history and quotes, and can inject the failures the trader must survive:
timeouts before or after eToro accepts an order, rejects, 5xx and 401.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

BASE = "https://etoro.test"
MIN = 60_000


def iso(ms: int, zone: bool = True) -> str:
    text = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return text + ("Z" if zone else "")


class FakeClock:
    def __init__(self, start_ms: int = 1_790_000_000_000) -> None:
        self.ms = start_ms

    def now(self) -> int:
        return self.ms

    async def sleep(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


def crypto_row(iid: int, symbol: str, **flags: Any) -> dict[str, Any]:
    row = {"instrumentId": iid, "displayname": symbol.title(), "internalSymbolFull": symbol, "internalAssetClassId": 10,
           "isCurrentlyTradable": True, "isBuyEnabled": True, "isDelisted": False, "isHiddenFromClient": False,
           "isActiveInPlatform": True}
    row.update(flags)
    return row


class FakeEtoro:
    def __init__(self, clock: FakeClock, env: str = "demo") -> None:
        self.clock = clock
        self.env = env
        self.cash = 10_000.0
        self.scopes = [f"etoro-public:{env}:write"]
        self.catalogue: list[dict[str, Any]] = []
        self.quotes: dict[int, tuple[float, float]] = {}
        self.quote_age_ms = 500
        self.quote_type = "realtime"
        self.closes_1m: dict[int, list[float]] = {}
        self.closes_5m: dict[int, list[float]] = {}
        self.candle_age_ms = 20_000
        self.eligibility_overrides: dict[int, dict[str, Any]] = {}
        self.positions: dict[int, dict[str, Any]] = {}
        self.orders: dict[int, dict[str, Any]] = {}
        self.close_orders: dict[int, dict[str, Any]] = {}
        self.by_ref: dict[str, int] = {}
        self.history: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.open_mode = "fill"  # fill | reject | timeout_before | timeout_after | hang
        self.fail_paths: dict[str, Any] = {}  # substring -> "timeout" | status code
        self.drop_protection = False
        self._next_id = 5_000_000

    # -- setup helpers -------------------------------------------------------

    def add_coin(self, iid: int, symbol: str, bid: float, ask: float, trend: float = 0.002, **flags: Any) -> None:
        """A coin whose last closes rise by ``trend`` (fraction) per candle."""
        self.catalogue.append(crypto_row(iid, symbol, **flags))
        self.quotes[iid] = (bid, ask)
        mid = (bid + ask) / 2
        self.closes_1m[iid] = [mid / (1 + trend) ** (29 - i) for i in range(30)]
        self.closes_5m[iid] = [mid / (1 + trend * 5) ** (3 - i) for i in range(4)]

    def add_foreign_position(self, iid: int, amount: float = 50.0) -> int:
        pid = self._id()
        bid, ask = self.quotes.get(iid, (1.0, 1.0))
        self.positions[pid] = self._position_row(pid, iid, amount, ask, amount / ask, None, None)
        return pid

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def posts(self) -> list[str]:
        return [path for method, path, _ in self.calls if method == "POST" and "/execution/" in path]

    # -- transport -----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.headers)))
        for fragment, action in self.fail_paths.items():
            if fragment in path:
                if action == "timeout":
                    raise httpx.ReadTimeout("simulated timeout", request=request)
                return httpx.Response(int(action), json={"error": "injected"})
        d = "/demo" if self.env == "demo" else ""
        routes = {
            ("GET", "/api/v1/me"): self._me,
            ("GET", f"/api/v1/trading/info{d or '/real'}/pnl"): self._pnl,
            ("GET", f"/api/v1/trading/info/trade{d}/history"): self._history,
            ("GET", "/api/v2/market-data/rates"): self._rates,
            ("GET", "/api/v1/market-data/search"): self._search,
            ("POST", f"/api/v2/trading/info{d}/eligibility"): self._eligibility,
            ("POST", f"/api/v1/trading/execution{d}/market-open-orders/by-amount"): self._open,
            ("GET", f"/api/v2/trading/info{d}/orders:lookup"): self._lookup,
        }
        handler = routes.get((request.method, path))
        if handler:
            return handler(request)
        if path.startswith("/api/v1/market-data/instruments/") and "/history/candles/" in path:
            return self._candles(request)
        if request.method == "POST" and path.startswith(f"/api/v1/trading/execution{d}/market-close-orders/positions/"):
            return self._close(request, int(path.rsplit("/", 1)[1]))
        if request.method == "GET" and path.startswith(f"/api/v1/trading/info{d or '/real'}/orders/"):
            info = self.orders.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=info) if info else httpx.Response(404, json={"title": "not found"})
        if request.method == "GET" and path.startswith(f"/api/v1/trading/info{d or '/real'}/close-orders/"):
            info = self.close_orders.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=info) if info else httpx.Response(404, json={"title": "not found"})
        return httpx.Response(404, json={"title": f"no route {request.method} {path}"})

    # -- reads ---------------------------------------------------------------

    def _me(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"gcid": 1, "realCid": 2, "demoCid": 3, "username": "t", "scopes": self.scopes})

    def _pnl(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"clientPortfolio": {
            "positions": list(self.positions.values()), "credit": self.cash, "unrealizedPnL": 0.0,
            "orders": [], "ordersForOpen": [], "entryOrders": [], "mirrors": []}})

    def _history(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.history)

    def _rates(self, request: httpx.Request) -> httpx.Response:
        ids = [int(x) for x in request.url.params["instrumentIds"].split(",") if x]
        results = [{"instrumentId": i, "bid": self.quotes[i][0], "ask": self.quotes[i][1],
                    "date": iso(self.clock.now() - self.quote_age_ms, zone=False), "quoteType": self.quote_type}
                   for i in ids if i in self.quotes]
        return httpx.Response(200 if len(results) == len(ids) else 206, json={"results": results})

    def _candles(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        iid, interval = int(parts[5]), parts[9]
        closes = self.closes_1m if interval == "OneMinute" else self.closes_5m
        step = MIN if interval == "OneMinute" else 5 * MIN
        series = closes.get(iid, [])
        last_start = (self.clock.now() - self.candle_age_ms) // step * step
        rows = []
        for i, close in enumerate(reversed(series)):  # desc: newest first
            prev = series[-2 - i] if i + 1 < len(series) else close
            rows.append({"instrumentID": iid, "fromDate": iso(last_start - i * step), "open": prev,
                         "high": max(prev, close) * 1.0001, "low": min(prev, close) * 0.9999, "close": close,
                         "volume": None})
        return httpx.Response(200, json={"interval": interval, "candles": [{"instrumentId": iid, "candles": rows}]})

    def _search(self, request: httpx.Request) -> httpx.Response:
        size = int(request.url.params.get("pageSize", 100))
        page = int(request.url.params.get("pageNumber", 1))
        items = self.catalogue[(page - 1) * size: page * size]
        return httpx.Response(200, json={"page": page, "pageSize": size, "totalItems": len(self.catalogue), "items": items})

    def _eligibility(self, request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["instrumentIds"]
        out = []
        for iid in ids:
            row = {"instrumentId": iid, "symbol": str(iid), "minPositionExposure": 10.0, "allowOpenPosition": True,
                   "allowClosePosition": True, "leverageConfigs": [
                       {"settlementType": "real", "direction": "long", "leverageValues": [1], "isPotential": False,
                        "minPositionAmount": 10.0, "allowEditStopLoss": True, "minStopLossPercentage": 10.0,
                        "allowStopLossTakeProfit": True}]}
            row.update(self.eligibility_overrides.get(iid, {}))
            out.append(row)
        return httpx.Response(200, json={"currency": "usd", "eligibilities": out, "notFoundInstrumentIds": []})

    def _lookup(self, request: httpx.Request) -> httpx.Response:
        ref = request.url.params.get("referenceId")
        oid = self.by_ref.get(ref or "")
        if oid is None:
            return httpx.Response(404, json={"title": "Order not found"})
        info = self.orders.get(oid) or {"orderId": oid, "status": {"id": 3, "name": "Filled"}, "positionExecutions": []}
        return httpx.Response(200, json=info)

    # -- writes --------------------------------------------------------------

    def _position_row(self, pid: int, iid: int, amount: float, rate: float, units: float,
                      sl: float | None, tp: float | None) -> dict[str, Any]:
        return {"positionID": pid, "instrumentID": iid, "isBuy": True, "amount": amount, "units": units,
                "openRate": rate, "stopLossRate": sl or 0.0001, "takeProfitRate": tp or 0,
                "isNoStopLoss": sl is None, "isNoTakeProfit": tp is None, "leverage": 1,
                "openDateTime": iso(self.clock.now()), "unrealizedPnL": {"pnL": 0.0}}

    def _open(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ref = request.headers["x-request-id"]
        if self.open_mode == "reject":
            return httpx.Response(400, json={"title": "Insufficient funds"})
        if self.open_mode == "timeout_before":
            raise httpx.ReadTimeout("no answer", request=request)
        iid, amount = body["InstrumentID"], body["Amount"]
        bid, ask = self.quotes[iid]
        oid, pid = self._id(), self._id()
        units = amount / ask
        sl = None if self.drop_protection else body["StopLossRate"]
        tp = None if self.drop_protection else body["TakeProfitRate"]
        self.positions[pid] = self._position_row(pid, iid, amount, ask, units, sl, tp)
        self.cash -= amount
        self.by_ref[ref] = oid
        self.orders[oid] = {
            "orderId": oid, "action": "open", "status": {"id": 3, "name": "Filled", "errorCode": 0},
            "asset": {"instrumentId": iid, "leverage": 1, "side": "long"},
            "positionExecutions": [{"positionId": pid, "state": "open", "remainingUnits": units,
                                    "stopLossRate": sl, "takeProfitRate": tp,
                                    "openingData": {"orderId": oid, "executionTime": iso(self.clock.now()),
                                                    "units": units, "avgPrice": ask}}],
        }
        if self.open_mode == "timeout_after":
            raise httpx.ReadTimeout("accepted but the answer was lost", request=request)
        return httpx.Response(200, json={"orderForOpen": {"instrumentID": iid, "amount": amount, "orderID": oid,
                                                          "statusID": 1}, "token": ref})

    def _close(self, request: httpx.Request, pid: int) -> httpx.Response:
        ref = request.headers["x-request-id"]
        pos = self.positions.pop(pid, None)
        if pos is None:
            return httpx.Response(400, json={"title": "Position not found"})
        bid, _ = self.quotes[pos["instrumentID"]]
        oid = self._id()
        proceeds = pos["units"] * bid
        self.cash += proceeds
        self.by_ref[ref] = oid
        self.close_orders[oid] = {"orderID": oid, "statusID": 3, "instrumentID": pos["instrumentID"],
                                  "positions": [{"positionID": pid, "rate": bid, "occurred": iso(self.clock.now()),
                                                 "units": pos["units"]}]}
        self.history.append({"positionId": pid, "instrumentId": pos["instrumentID"], "openRate": pos["openRate"],
                             "closeRate": bid, "netProfit": round(proceeds - pos["amount"], 4),
                             "closeTimestamp": iso(self.clock.now())})
        return httpx.Response(200, json={"orderForClose": {"positionID": pid, "orderID": oid, "statusID": 1}})

    def broker_close(self, pid: int, rate: float) -> None:
        """eToro closes a position by itself (its TP or SL fired)."""
        pos = self.positions.pop(pid)
        self.cash += pos["units"] * rate
        self.history.append({"positionId": pid, "instrumentId": pos["instrumentID"], "openRate": pos["openRate"],
                             "closeRate": rate, "netProfit": round(pos["units"] * rate - pos["amount"], 4),
                             "closeTimestamp": iso(self.clock.now())})
