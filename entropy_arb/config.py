"""Entropy spread-gated timed market cycle configuration."""
from __future__ import annotations

import math
import os
import warnings
from dataclasses import dataclass
import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HEDGE_VENUES = ("lighter-rh",)


@dataclass(frozen=True)
class LighterProfile:
    name: str = "robinhood"
    api_url: str = "https://api.rh.lighter.xyz"
    ws_url: str = "wss://api.rh.lighter.xyz/stream"
    chain_id: int = 466324


LIGHTER_PROFILES = {"lighter-rh": LighterProfile()}


@dataclass
class HLCreds:
    private_key: str | None
    account_address: str | None

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str
    kind: str
    label: str
    symbol: str
    fee_bps: float = 0.0
    cap_usd: float = 1000.0
    orders_per_min: int = 120
    hl_dex: str = "io"
    hl_creds: HLCreds | None = None
    lighter_profile: LighterProfile | None = None


@dataclass
class Config:
    symbol: str
    entropy: VenueConf
    hedge: VenueConf | None = None  # legacy construction compatibility; never connected
    hedge_venue: str = "lighter-rh"
    direction: str = "random"
    cycles: int = 0
    max_spread_bps: float = 2.0
    close_delay_ms: float = 50.0
    virtual_depth: int = 4  # legacy setting, ignored
    virtual_requote_sec: float = 3.0
    quantity: float = 0.0  # 0 sizes from order_notional
    order_notional: float = 50.0
    max_order_notional: float = 500.0
    min_order_notional: float = 10.0
    cooldown_sec: float = 2.0
    settle_timeout_sec: float = 5.0
    leg_slippage_bps: float = 20.0
    leg2_slippage_bps: float | None = None
    leg4_slippage_bps: float | None = None
    max_order_attempts: int = 3
    retry_delay_sec: float = 1.0
    rate_limit_pause_sec: float = 10.0
    staleness_sec: float = 10.0
    reconcile_sec: float = 15.0  # legacy; position checks now follow each close
    http_keepalive_sec: float = 10.0
    max_hold_sec: float = 0.0
    recorder_enabled: bool = True
    recorder_csv: str = "logs/minutes.csv"
    log_level: str = "INFO"
    status_interval_sec: float = 30.0
    account_refresh_sec: float = 10.0
    trades_csv: str = "logs/legs.csv"
    state_file: str = "logs/cycle.json"
    dashboard: bool = True
    log_file: str = "logs/engine.log"
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        return bool(self.entropy.hl_creds and self.entropy.hl_creds.complete)


class ConfigError(ValueError):
    pass


_SCHEMA = {
    "cycle": {"direction": ("direction", str), "cycles": ("cycles", int),
              "max_spread_bps": ("max_spread_bps", float),
              "close_delay_ms": ("close_delay_ms", float),
              "virtual_depth": ("virtual_depth", int),
              "virtual_requote_sec": ("virtual_requote_sec", float),
              "max_hold_sec": ("max_hold_sec", float), "state_file": ("state_file", str)},
    "sizing": {"quantity": ("quantity", float), "order_notional_usd": ("order_notional", float),
               "max_order_notional_usd": ("max_order_notional", float),
               "min_order_notional_usd": ("min_order_notional", float)},
    "execution": {k: (k, t) for k, t in {
        "cooldown_sec": float, "settle_timeout_sec": float, "leg_slippage_bps": float,
        "leg2_slippage_bps": float, "leg4_slippage_bps": float,
        "max_order_attempts": int, "retry_delay_sec": float, "rate_limit_pause_sec": float,
        "staleness_sec": float, "reconcile_sec": float, "http_keepalive_sec": float}.items()},
    "recorder": {"enabled": ("recorder_enabled", bool), "csv": ("recorder_csv", str)},
    "logging": {"level": ("log_level", str), "status_interval_sec": ("status_interval_sec", float),
                "account_refresh_sec": ("account_refresh_sec", float),
                "trades_csv": ("trades_csv", str), "dashboard": ("dashboard", bool),
                "file": ("log_file", str)},
    "entropy": {"dex": ("hl_dex", str), "taker_fee_bps": ("fee_bps", float),
                "max_position_usd": ("cap_usd", float), "max_orders_per_min": ("orders_per_min", int)},
}


def load_config(config_file="config.yaml", env_file=".env", *,
                symbol: str, hedge_venue="lighter-rh") -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {config_file}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    if hedge_venue != "lighter-rh":
        raise ConfigError("--hedge is fixed to lighter-rh")
    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required")
    values, entropy_values = {}, {}
    for section, node in raw.items():
        if section not in _SCHEMA:
            raise ConfigError(f"unknown config key '{section}'")
        if not isinstance(node, dict):
            raise ConfigError(f"'{section}' must be a mapping")
        for key, value in node.items():
            if key not in _SCHEMA[section]:
                raise ConfigError(f"unknown config key '{section}.{key}'")
            field, kind = _SCHEMA[section][key]
            valid = (type(value) in (int, float) and math.isfinite(value)) if kind is float else type(value) is kind
            if not valid:
                raise ConfigError(f"'{section}.{key}' must be a finite {kind.__name__}")
            (entropy_values if section == "entropy" else values)[field] = kind(value)
    if entropy_values.get("hl_dex", "io") != "io":
        raise ConfigError("entropy.dex is fixed to io")
    creds = HLCreds(os.getenv("HL_PRIVATE_KEY", "").strip() or None,
                    os.getenv("HL_ACCOUNT_ADDRESS", "").strip() or None)
    cfg = Config(symbol=symbol,
                 entropy=VenueConf("entropy", "hl", "ENTROPY", symbol, hl_creds=creds, **entropy_values),
                 **values)
    if cfg.direction != "random":
        raise ConfigError("cycle.direction is fixed to random")
    if not 0 <= cfg.max_spread_bps < 10000:
        raise ConfigError("max_spread_bps must be in [0, 10000)")
    for key in ("cycles", "quantity", "cooldown_sec", "max_hold_sec"):
        if getattr(cfg, key) < 0:
            raise ConfigError(f"{key} must be >= 0")
    for key in ("virtual_depth", "virtual_requote_sec", "order_notional", "max_order_notional",
                "min_order_notional", "settle_timeout_sec", "max_order_attempts", "retry_delay_sec",
                "rate_limit_pause_sec", "staleness_sec", "reconcile_sec", "http_keepalive_sec",
                "status_interval_sec", "account_refresh_sec", "close_delay_ms"):
        if getattr(cfg, key) <= 0:
            raise ConfigError(f"{key} must be > 0")
    for key in ("leg_slippage_bps", "leg2_slippage_bps", "leg4_slippage_bps"):
        value = getattr(cfg, key)
        if value is not None and not 0 <= value < 10000:
            raise ConfigError(f"{key} must be in [0, 10000)")
    if not cfg.min_order_notional <= cfg.order_notional <= cfg.max_order_notional:
        raise ConfigError("min_order_notional <= order_notional <= max_order_notional required")
    if cfg.entropy.cap_usd <= 0 or cfg.entropy.orders_per_min < 2 or cfg.entropy.fee_bps < 0:
        raise ConfigError("entropy cap must be positive, orders_per_min >= 2, and fees nonnegative")
    for key in ("state_file", "trades_csv", "recorder_csv", "log_file"):
        if not getattr(cfg, key).strip():
            raise ConfigError(f"{key} must not be empty")
    cfg.log_level = cfg.log_level.upper()
    if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("invalid logging.level")
    for key in ("virtual_depth", "virtual_requote_sec", "max_hold_sec"):
        if key in raw.get("cycle", {}):
            warnings.warn(f"cycle.{key} is ignored by the Entropy-only strategy", stacklevel=2)
    return cfg
