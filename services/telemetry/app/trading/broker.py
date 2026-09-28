"""The only code in this repository that can move money on eToro.

Every route below was taken from the eToro Public API specification
(``get-route-spec``), not guessed. The environment is fixed when the client is
built: a DEMO client holds only DEMO routes and cannot reach a REAL order
route, and the other way round. Nothing can switch it later.

Write semantics
---------------
Each write carries a fresh ``x-request-id`` that the caller has already
stored. eToro echoes it as the order's ``referenceId``. When a write times out
or fails ambiguously the caller must not resend. It calls
``lookup_by_reference`` first and learns whether the order exists.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

import httpx

from .config import Secret

log = logging.getLogger("trading.broker")

ROUTES: dict[str, MappingProxyType] = {
    "demo": MappingProxyType({
        "pnl": "/api/v1/trading/info/demo/pnl",
        "history": "/api/v1/trading/info/trade/demo/history",
        "open": "/api/v1/trading/execution/demo/market-open-orders/by-amount",
        "close": "/api/v1/trading/execution/demo/market-close-orders/positions/{position_id}",
        "order": "/api/v1/trading/info/demo/orders/{order_id}",
        "close_order": "/api/v1/trading/info/demo/close-orders/{order_id}",
        "lookup": "/api/v2/trading/info/demo/orders:lookup",
        "eligibility": "/api/v2/trading/info/demo/eligibility",
    }),
    "real": MappingProxyType({
        "pnl": "/api/v1/trading/info/real/pnl",
        "history": "/api/v1/trading/info/trade/history",
        "open": "/api/v1/trading/execution/market-open-orders/by-amount",
        "close": "/api/v1/trading/execution/market-close-orders/positions/{position_id}",
        "order": "/api/v1/trading/info/real/orders/{order_id}",
        "close_order": "/api/v1/trading/info/real/close-orders/{order_id}",
        "lookup": "/api/v2/trading/info/orders:lookup",
        "eligibility": "/api/v2/trading/info/eligibility",
    }),
}
# Shared by both environments: prices, candles and the instrument catalogue.
ME = "/api/v1/me"
RATES = "/api/v2/market-data/rates"
CANDLES = "/api/v1/market-data/instruments/{instrument_id}/history/candles/{direction}/{interval}/{count}"
SEARCH = "/api/v1/market-data/search"

# Scopes that allow order placement, per environment (from the route specs).
WRITE_SCOPES = {
    "demo": {"etoro-public:demo:write", "etoro-public:trade.demo:write"},
    "real": {"etoro-public:real:write", "etoro-public:trade.real:write"},
}

# Order status ids from the order-info schema.
STATUS_FILLED = 3
STATUS_REJECTED = {4, 7, 8, 10}  # Rejected, Canceled, Expired, RejectedPartiallyFilled
STATUS_PARTIAL = {5, 9}  # PartiallyFilled, CanceledPartiallyFilled
DEFINITIVE_4XX = {400, 404, 409, 422}


class BrokerError(Exception):
    """eToro answered and the answer is final (the request was refused)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"eToro {status}: {message}")
        self.status = status


class AuthError(BrokerError):
    """401/403: the keys are wrong, expired, or lack permission."""


class BrokerUncertain(Exception):
    """No definitive answer (timeout, network, 5xx, 429). A write may or may not
    have happened: look it up by reference, never resend blindly."""


def _clip(text: str) -> str:
    return " ".join(text.split())[:200]


class Broker:
    def __init__(
        self,
        env: str,
        api_key: Secret,
        user_key: Secret,
        *,
        base_url: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if env not in ROUTES:
            raise ValueError(f"unknown trading environment {env!r}")
        self._env = env
        self._routes = ROUTES[env]
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "x-api-key": api_key.reveal(),
                "x-user-key": user_key.reveal(),
                "accept": "application/json",
            },
            transport=transport,
        )

    @property
    def env(self) -> str:
        return self._env

    def __repr__(self) -> str:
        return f"Broker(env={self._env!r}, keys=***)"

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport ---------------------------------------------------------

    async def _send(self, method: str, path: str, request_id: str, *, params: dict[str, Any] | None = None,
                    body: dict[str, Any] | None = None, allow_404: bool = False) -> Any:
        try:
            response = await self._http.request(
                method, path, params=params, json=body, headers={"x-request-id": request_id}
            )
        except httpx.HTTPError as exc:
            raise BrokerUncertain(f"{method} {path}: {type(exc).__name__}") from None
        status = response.status_code
        if status in (401, 403):
            raise AuthError(status, _clip(response.text) or response.reason_phrase)
        if status == 404 and allow_404:
            return None
        if status in DEFINITIVE_4XX:
            raise BrokerError(status, _clip(response.text) or response.reason_phrase)
        if status >= 400:
            raise BrokerUncertain(f"{method} {path}: HTTP {status}")
        try:
            return response.json()
        except ValueError:
            raise BrokerUncertain(f"{method} {path}: response was not JSON") from None

    async def _get(self, path: str, request_id: str, params: dict[str, Any] | None = None, *,
                   allow_404: bool = False) -> Any:
        return await self._send("GET", path, request_id, params=params, allow_404=allow_404)

    # -- reads -------------------------------------------------------------

    async def me(self, request_id: str) -> dict[str, Any]:
        return await self._get(ME, request_id)

    async def portfolio(self, request_id: str) -> dict[str, Any]:
        return await self._get(self._routes["pnl"], request_id)

    async def history(self, request_id: str, min_date: str) -> list[dict[str, Any]]:
        return await self._get(self._routes["history"], request_id, {"minDate": min_date, "page": 1, "pageSize": 100})

    async def rates(self, request_id: str, instrument_ids: list[int]) -> dict[str, Any]:
        return await self._get(RATES, request_id, {"instrumentIds": ",".join(map(str, instrument_ids))})

    async def candles(self, request_id: str, instrument_id: int, interval: str, count: int) -> dict[str, Any]:
        path = CANDLES.format(instrument_id=instrument_id, direction="desc", interval=interval, count=count)
        return await self._get(path, request_id)

    async def search_crypto(self, request_id: str, page: int, page_size: int) -> dict[str, Any]:
        fields = (
            "instrumentId,displayname,internalSymbolFull,internalAssetClassId,isCurrentlyTradable,"
            "isBuyEnabled,isDelisted,isHiddenFromClient,isActiveInPlatform"
        )
        # internalAssetClassId=10 is Crypto (verified against the live catalogue).
        params = {"fields": fields, "internalAssetClassId": 10, "pageSize": page_size, "pageNumber": page}
        return await self._get(SEARCH, request_id, params)

    async def eligibility(self, request_id: str, instrument_ids: list[int]) -> dict[str, Any]:
        # Read-semantics POST: it reports trading rules and changes nothing.
        body = {"instrumentIds": instrument_ids, "currency": "USD"}
        return await self._send("POST", self._routes["eligibility"], request_id, body=body)

    async def order_info(self, request_id: str, order_id: int) -> dict[str, Any] | None:
        return await self._get(self._routes["order"].format(order_id=order_id), request_id, allow_404=True)

    async def close_order_info(self, request_id: str, order_id: int) -> dict[str, Any] | None:
        return await self._get(self._routes["close_order"].format(order_id=order_id), request_id, allow_404=True)

    async def lookup_by_reference(self, request_id: str, reference_id: str) -> dict[str, Any] | None:
        """Find an order by the x-request-id it was submitted with. None if eToro has no such order."""
        return await self._get(self._routes["lookup"], request_id, {"referenceId": reference_id}, allow_404=True)

    # -- writes: the single execution path ---------------------------------

    async def open_by_amount(self, request_id: str, *, instrument_id: int, is_buy: bool, amount: float,
                             leverage: int, stop_loss_rate: float, take_profit_rate: float) -> dict[str, Any]:
        body = {
            "InstrumentID": instrument_id,
            "IsBuy": is_buy,
            "Leverage": leverage,
            "Amount": amount,
            "StopLossRate": stop_loss_rate,
            "TakeProfitRate": take_profit_rate,
            "IsTslEnabled": False,
            "IsNoStopLoss": False,
            "IsNoTakeProfit": False,
        }
        log.info("SUBMIT open env=%s request_id=%s instrument=%s amount=%.2f", self._env, request_id, instrument_id, amount)
        return await self._send("POST", self._routes["open"], request_id, body=body)

    async def close_position(self, request_id: str, *, position_id: int, instrument_id: int) -> dict[str, Any]:
        body = {"InstrumentID": instrument_id, "UnitsToDeduct": None}
        log.info("SUBMIT close env=%s request_id=%s position=%s", self._env, request_id, position_id)
        return await self._send("POST", self._routes["close"].format(position_id=position_id), request_id, body=body)
