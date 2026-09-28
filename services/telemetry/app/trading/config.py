"""Every trading setting in one place, read once from the environment.

Invalid or missing values stop the trader before it can place an order: a
typo in a risk limit must never turn into a looser limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

REAL_CONFIRMATION = "I_ACCEPT_REAL_MONEY_RISK"


class ConfigError(ValueError):
    """The configuration is unsafe or incomplete. The trader must not start."""


class Secret:
    """A credential that never prints. ``str()`` and ``repr()`` are masked."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    __str__ = __repr__


@dataclass(frozen=True)
class TradingConfig:
    trading_env: str
    pipeline_enabled: bool
    api_key: Secret
    user_key: Secret
    api_base: str
    db_path: str

    # market data
    max_spread_percent: float
    max_market_data_age_seconds: float
    max_candle_age_seconds: float
    # sizing and exits
    max_position_percent: float
    max_position_usd: float
    tp_percent_min: float
    tp_percent_max: float
    sl_percent_min: float
    sl_percent_max: float
    max_hold_seconds: int
    max_entry_slippage_percent: float
    broker_sl_buffer_percent: float
    leverage: int
    allow_short: bool
    # account limits
    max_daily_loss_usd: float
    max_consecutive_losses: int
    max_open_positions: int
    max_total_exposure_percent: float
    max_trade_count_per_day: int
    # failure handling
    api_failure_halt_count: int
    order_confirm_timeout_seconds: float
    signal_ttl_seconds: float
    emergency_close_positions: bool
    # cadence and ranking
    scan_interval_seconds: float
    monitor_interval_seconds: float
    universe_refresh_seconds: float
    candidates_per_scan: int
    min_momentum_5m_percent: float
    rank_weight_momentum_5m: float
    rank_weight_momentum_1m: float
    rank_weight_spread: float
    rank_weight_volatility: float

    @property
    def is_real(self) -> bool:
        return self.trading_env == "real"

    def public(self) -> dict[str, Any]:
        """Settings safe to show on the dashboard. Credentials are left out."""
        hidden = {"api_key", "user_key", "api_base", "db_path"}
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in hidden}


_DEFAULTS: dict[str, str] = {
    "TRADING_ENV": "demo",
    "TRADING_PIPELINE_ENABLED": "false",
    "ETORO_API_BASE": "https://public-api.etoro.com",
    "TRADING_DB_PATH": "trading.db",
    "MAX_SPREAD_PERCENT": "0.20",
    "MAX_MARKET_DATA_AGE_SECONDS": "15",
    "MAX_CANDLE_AGE_SECONDS": "150",
    "MAX_POSITION_PERCENT": "25",
    "MAX_POSITION_USD": "0",
    "TP_PERCENT_MIN": "0.4",
    "TP_PERCENT_MAX": "0.8",
    "SL_PERCENT_MIN": "0.3",
    "SL_PERCENT_MAX": "0.5",
    "MAX_HOLD_SECONDS": "900",
    "MAX_ENTRY_SLIPPAGE_PERCENT": "0.15",
    "BROKER_SL_BUFFER_PERCENT": "1.0",
    "LEVERAGE": "1",
    "ALLOW_SHORT": "false",
    "MAX_DAILY_LOSS_USD": "25",
    "MAX_CONSECUTIVE_LOSSES": "3",
    "MAX_OPEN_POSITIONS": "2",
    "MAX_TOTAL_EXPOSURE_PERCENT": "50",
    "MAX_TRADE_COUNT_PER_DAY": "20",
    "API_FAILURE_HALT_COUNT": "5",
    "ORDER_CONFIRM_TIMEOUT_SECONDS": "30",
    "SIGNAL_TTL_SECONDS": "20",
    "EMERGENCY_CLOSE_POSITIONS": "false",
    "SCAN_INTERVAL_SECONDS": "30",
    "MONITOR_INTERVAL_SECONDS": "2",
    "UNIVERSE_REFRESH_SECONDS": "3600",
    "CANDIDATES_PER_SCAN": "6",
    "MIN_MOMENTUM_5M_PERCENT": "0.05",
    "RANK_WEIGHT_MOMENTUM_5M": "1.0",
    "RANK_WEIGHT_MOMENTUM_1M": "0.5",
    "RANK_WEIGHT_SPREAD": "2.0",
    "RANK_WEIGHT_VOLATILITY": "0.25",
}


def _get(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if value is None or not str(value).strip():
        return _DEFAULTS.get(key, "")
    return str(value).strip()


def _bool(env: Mapping[str, str], key: str) -> bool:
    raw = _get(env, key).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")


def _num(env: Mapping[str, str], key: str, lo: float, hi: float, *, integer: bool = False) -> Any:
    raw = _get(env, key)
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{key}={value} is outside the allowed range {lo}..{hi}")
    return value


def load_config(env: Mapping[str, str]) -> TradingConfig:
    trading_env = _get(env, "TRADING_ENV").lower()
    if trading_env not in {"demo", "real"}:
        raise ConfigError(f"TRADING_ENV must be 'demo' or 'real', got {trading_env!r}")
    if trading_env == "real" and _get(env, "TRADING_REAL_CONFIRM") != REAL_CONFIRMATION:
        raise ConfigError(
            "TRADING_ENV=real needs TRADING_REAL_CONFIRM set to the exact confirmation phrase "
            "(see docs/TRADING.md). Refusing to trade real money without it."
        )

    cfg = TradingConfig(
        trading_env=trading_env,
        pipeline_enabled=_bool(env, "TRADING_PIPELINE_ENABLED"),
        # Separate keys from the dashboard's read-only ones, on purpose: the
        # monitor key can stay read-only while only the trader can write.
        api_key=Secret(_get(env, "ETORO_TRADING_API_KEY")),
        user_key=Secret(_get(env, "ETORO_TRADING_USER_KEY")),
        api_base=_get(env, "ETORO_API_BASE").rstrip("/"),
        db_path=_get(env, "TRADING_DB_PATH"),
        max_spread_percent=_num(env, "MAX_SPREAD_PERCENT", 0.001, 2.0),
        max_market_data_age_seconds=_num(env, "MAX_MARKET_DATA_AGE_SECONDS", 1, 120),
        max_candle_age_seconds=_num(env, "MAX_CANDLE_AGE_SECONDS", 60, 900),
        max_position_percent=_num(env, "MAX_POSITION_PERCENT", 0.1, 100),
        max_position_usd=_num(env, "MAX_POSITION_USD", 0, 10_000_000),
        tp_percent_min=_num(env, "TP_PERCENT_MIN", 0.05, 20),
        tp_percent_max=_num(env, "TP_PERCENT_MAX", 0.05, 20),
        sl_percent_min=_num(env, "SL_PERCENT_MIN", 0.05, 20),
        sl_percent_max=_num(env, "SL_PERCENT_MAX", 0.05, 20),
        max_hold_seconds=_num(env, "MAX_HOLD_SECONDS", 30, 86_400, integer=True),
        max_entry_slippage_percent=_num(env, "MAX_ENTRY_SLIPPAGE_PERCENT", 0.0, 5.0),
        broker_sl_buffer_percent=_num(env, "BROKER_SL_BUFFER_PERCENT", 0.1, 20),
        leverage=_num(env, "LEVERAGE", 1, 100, integer=True),
        allow_short=_bool(env, "ALLOW_SHORT"),
        max_daily_loss_usd=_num(env, "MAX_DAILY_LOSS_USD", 0.01, 10_000_000),
        max_consecutive_losses=_num(env, "MAX_CONSECUTIVE_LOSSES", 1, 1000, integer=True),
        max_open_positions=_num(env, "MAX_OPEN_POSITIONS", 1, 100, integer=True),
        max_total_exposure_percent=_num(env, "MAX_TOTAL_EXPOSURE_PERCENT", 0.1, 100),
        max_trade_count_per_day=_num(env, "MAX_TRADE_COUNT_PER_DAY", 1, 10_000, integer=True),
        api_failure_halt_count=_num(env, "API_FAILURE_HALT_COUNT", 1, 100, integer=True),
        order_confirm_timeout_seconds=_num(env, "ORDER_CONFIRM_TIMEOUT_SECONDS", 3, 300),
        signal_ttl_seconds=_num(env, "SIGNAL_TTL_SECONDS", 2, 300),
        emergency_close_positions=_bool(env, "EMERGENCY_CLOSE_POSITIONS"),
        scan_interval_seconds=_num(env, "SCAN_INTERVAL_SECONDS", 5, 3600),
        monitor_interval_seconds=_num(env, "MONITOR_INTERVAL_SECONDS", 0.5, 60),
        universe_refresh_seconds=_num(env, "UNIVERSE_REFRESH_SECONDS", 60, 86_400),
        candidates_per_scan=_num(env, "CANDIDATES_PER_SCAN", 1, 30, integer=True),
        min_momentum_5m_percent=_num(env, "MIN_MOMENTUM_5M_PERCENT", 0.0, 10),
        rank_weight_momentum_5m=_num(env, "RANK_WEIGHT_MOMENTUM_5M", 0, 100),
        rank_weight_momentum_1m=_num(env, "RANK_WEIGHT_MOMENTUM_1M", 0, 100),
        rank_weight_spread=_num(env, "RANK_WEIGHT_SPREAD", 0, 100),
        rank_weight_volatility=_num(env, "RANK_WEIGHT_VOLATILITY", 0, 100),
    )

    if cfg.tp_percent_min > cfg.tp_percent_max:
        raise ConfigError("TP_PERCENT_MIN is above TP_PERCENT_MAX")
    if cfg.sl_percent_min > cfg.sl_percent_max:
        raise ConfigError("SL_PERCENT_MIN is above SL_PERCENT_MAX")
    if cfg.max_position_percent > cfg.max_total_exposure_percent:
        raise ConfigError("MAX_POSITION_PERCENT cannot exceed MAX_TOTAL_EXPOSURE_PERCENT")
    if not cfg.api_base.startswith("https://"):
        raise ConfigError("ETORO_API_BASE must be an https:// address")
    if cfg.pipeline_enabled and not (cfg.api_key and cfg.user_key):
        raise ConfigError("ETORO_TRADING_API_KEY and ETORO_TRADING_USER_KEY are required to trade")
    return cfg


def banner(cfg: TradingConfig) -> str:
    return f"TRADING ENVIRONMENT: {cfg.trading_env.upper()}"
