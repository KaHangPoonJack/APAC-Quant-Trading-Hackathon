"""Typed configuration loaded from config/settings.yaml.

Raw YAML is parsed once into frozen dataclasses so the rest of the codebase
reads strongly-typed attributes (cfg.strategy.pairs) instead of dict keys.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# Repo root = parent of this file's package.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "settings.yaml"
LOCAL_CONFIG = ROOT / "config" / "settings.local.yaml"


@dataclass(frozen=True)
class RoostooConfig:
    """Roostoo REST broker. Keys are SECRETS -> settings.local.yaml or env.

    Two key sets exist (competition FAQ Q30): one for the TEST/general account,
    one for the official round. `env` picks which pair is used, so switching to
    the competition account is one explicit config edit, never an accident.
    """
    base_url: str = "https://mock-api.roostoo.com"
    env: str = "TEST"                       # TEST | COMPETITION
    # env -> {api_key, secret_key}; repr=False keeps secrets out of logs/tracebacks
    keys: Dict[str, Dict[str, str]] = field(default_factory=dict, repr=False)
    # The FAQ limit is 30 calls/min across ALL endpoints; keep headroom.
    max_calls_per_minute: int = 25
    timeout_seconds: float = 10.0
    max_retries: int = 3

    @property
    def api_key(self) -> str:
        return (self.keys.get(self.env.upper()) or {}).get("api_key", "")

    @property
    def secret_key(self) -> str:
        return (self.keys.get(self.env.upper()) or {}).get("secret_key", "")

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.secret_key)


@dataclass(frozen=True)
class BinanceConfig:
    """Binance PUBLIC spot market data (no key). Roostoo has no OHLCV."""
    base_url: str = "https://data-api.binance.vision/api/v3"
    quote_asset: str = "USDT"               # BTC/USD on Roostoo <-> BTCUSDT on Binance
    timeout_seconds: float = 10.0
    futures_base_url: str = "https://fapi.binance.com/fapi/v1"   # USDS-M perps (volume only)


@dataclass(frozen=True)
class StrategyConfig:
    """Which strategy the trader runs and what data it is fed.

    `name` selects the class in engine/factory.py's STRATEGIES registry and is
    also the deployment / Telegram channel key. `params` is passed through to
    the strategy untouched, so strategy-specific knobs need no config.py edit.
    """
    name: str = "example_noop"
    pairs: List[str] = field(default_factory=list)   # Roostoo pairs, e.g. BTC/USD
    bar_interval: str = "1h"                # Binance kline interval
    lookback_bars: int = 200
    rebalance_seconds: int = 3600           # how often target_weights() is asked
    # "perp" also fetches Binance USDS-M bars into ctx.volume_bars (liquidity only)
    volume_source: str = "spot"
    params: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskConfig:
    """Portfolio-level limits applied to the strategy's target weights."""
    max_gross_exposure: float = 1.0         # sum |w|
    max_net_exposure: float = 1.0           # |sum w|
    max_weight_per_pair: float = 0.5        # |w| per pair
    min_order_usd: float = 10.0             # skip deltas smaller than this
    allow_short: bool = False               # /v6 shorts (competition allows them)
    cash_buffer_pct: float = 0.01           # never deploy the last 1% (fees/rounding)


@dataclass(frozen=True)
class EngineConfig:
    poll_interval_seconds: int = 60
    snapshot_interval_minutes: int = 60   # equity snapshot cadence (lower for testing)
    stale_order_seconds: int = 300        # cancel unfilled limit orders after this
    fill_sync_every_n_polls: int = 5      # query_order is a rate-limited call
    order_type: str = "MARKET"            # MARKET (fills at bid/ask) or LIMIT
    status_report_minutes: int = 60       # Telegram equity/holdings report; 0 = off


@dataclass(frozen=True)
class PersistenceConfig:
    enabled: bool
    db_path: str


@dataclass(frozen=True)
class FxConfig:
    reporting_currency: str = "USD"
    rates: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkConfig:
    enabled: bool
    history_days: int
    indices: List[dict]        # [{name, symbol}] — symbol is a Binance spot symbol


@dataclass(frozen=True)
class RunnersConfig:
    """What `scripts.run_all` (the one-terminal supervisor) starts. Each entry in
    `enabled` maps a runner name to whether it gets a child process at all.
    CLI --only/--skip override these per invocation."""
    enabled: Dict[str, bool] = field(default_factory=dict)
    auto_restart: bool = True   # restart crashed runners with backoff


_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


def looks_like_bot_token(token: str) -> bool:
    """True if `token` has Telegram's `<bot_id>:<hash>` shape.

    This exists to catch pasted PLACEHOLDERS (e.g. `<system bot token>`), which
    are non-empty and so read as configured, but build a URL containing spaces —
    `http.client` then raised `InvalidURL` straight out of `send()`. An
    unparseable token now means the channel is simply not ready, so routing falls
    back to the system bot (then a no-op) instead of crashing the caller.
    """
    return bool(_BOT_TOKEN_RE.match((token or "").strip()))


@dataclass(frozen=True)
class TelegramChannel:
    """One Telegram bot destination (a strategy's bot, or the 'system' bot)."""
    bot_token: str = ""
    chat_id: str = ""

    @property
    def token_valid(self) -> bool:
        return looks_like_bot_token(self.bot_token)

    @property
    def status(self) -> str:
        """Why this channel is/isn't usable — surfaced by scripts.test_notification."""
        if not self.bot_token:
            return "no token"
        if not self.token_valid:
            return "token is not a Telegram token (placeholder?)"
        if not self.chat_id:
            return "no chat_id"
        return "ready"

    @property
    def ready(self) -> bool:
        return bool(self.token_valid and self.chat_id)


@dataclass(frozen=True)
class NotificationsConfig:
    """Multi-bot Telegram routing. Each strategy can have its own bot (keyed by
    strategy name / deployment id), plus a `system` bot for errors and alerts.
    The legacy single-bot fields are kept as the fallback/system default so
    existing single-bot setups keep working until per-strategy bots are added."""
    enabled: bool
    telegram_bot_token: str = ""   # legacy single bot → used as the system default
    telegram_chat_id: str = ""
    channels: Dict[str, TelegramChannel] = field(default_factory=dict)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.enabled and looks_like_bot_token(self.telegram_bot_token)
                    and self.telegram_chat_id)

    def channel(self, key: str) -> Optional[TelegramChannel]:
        return self.channels.get(key)


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    dir: str


@dataclass(frozen=True)
class AppConfig:
    roostoo: RoostooConfig
    binance: BinanceConfig
    strategy: StrategyConfig
    risk: RiskConfig
    engine: EngineConfig
    persistence: PersistenceConfig
    fx: FxConfig
    allocations: Dict[str, float]
    benchmarks: BenchmarkConfig
    notifications: NotificationsConfig
    runners: RunnersConfig
    logging: LoggingConfig


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base` (override wins)."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: os.PathLike | str | None = None) -> AppConfig:
    """Load settings.yaml, overlaying settings.local.yaml if present."""
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if path == DEFAULT_CONFIG and LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as fh:
            raw = _deep_merge(raw, yaml.safe_load(fh) or {})

    return AppConfig(
        roostoo=_load_roostoo(raw.get("roostoo", {})),
        binance=BinanceConfig(**(raw.get("binance") or {})),
        strategy=_load_strategy(raw.get("strategy", {})),
        risk=RiskConfig(**(raw.get("risk") or {})),
        engine=EngineConfig(**(raw.get("engine") or {})),
        persistence=PersistenceConfig(**raw["persistence"]),
        fx=FxConfig(**(raw.get("fx") or {})),
        allocations={k: float(v) for k, v in (raw.get("allocations") or {}).items()},
        benchmarks=BenchmarkConfig(**raw["benchmarks"]),
        notifications=_load_notifications(raw.get("notifications", {})),
        runners=_load_runners(raw.get("runners", {})),
        logging=LoggingConfig(**raw["logging"]),
    )


def _load_roostoo(raw: dict) -> RoostooConfig:
    raw = dict(raw or {})
    keys_raw = raw.pop("keys", None) or {}
    keys: Dict[str, Dict[str, str]] = {}
    for env in set(list(keys_raw.keys()) + ["TEST", "COMPETITION"]):
        k = keys_raw.get(env) or {}
        env_u = str(env).upper()
        keys[env_u] = {
            "api_key": os.getenv(f"ROOSTOO_API_KEY_{env_u}", str(k.get("api_key", "") or "")),
            "secret_key": os.getenv(f"ROOSTOO_SECRET_KEY_{env_u}",
                                    str(k.get("secret_key", "") or "")),
        }
    env = os.getenv("ROOSTOO_ENV", str(raw.pop("env", "TEST"))).upper()
    if env not in ("TEST", "COMPETITION"):
        raise ValueError(f"roostoo.env must be TEST or COMPETITION, got {env!r}")
    return RoostooConfig(env=env, keys=keys, **raw)


def _load_strategy(raw: dict) -> StrategyConfig:
    raw = dict(raw or {})
    pairs = [str(p).upper() for p in (raw.pop("pairs", None) or [])]
    params = dict(raw.pop("params", None) or {})
    if raw.get("volume_source", "spot") not in ("spot", "perp"):
        raise ValueError(f"strategy.volume_source must be spot or perp, "
                         f"got {raw['volume_source']!r}")
    return StrategyConfig(pairs=pairs, params=params, **raw)


def _load_runners(raw: dict) -> RunnersConfig:
    enabled = {str(k): bool(v) for k, v in (raw.get("enabled") or {}).items()}
    return RunnersConfig(enabled=enabled,
                         auto_restart=bool(raw.get("auto_restart", True)))


def _load_notifications(raw: dict) -> NotificationsConfig:
    tg = raw.get("telegram", {})
    default_chat = str(tg.get("chat_id", "") or "")
    legacy_token = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token", "") or "")
    legacy_chat = os.getenv("TELEGRAM_CHAT_ID", default_chat)

    # Per-strategy / system bots. Each channel: {bot_token, chat_id}. chat_id
    # falls back to the shared telegram.chat_id so one chat can host every bot.
    # Optional env override per channel: TELEGRAM_BOT_TOKEN_<KEY>/_CHAT_ID_<KEY>
    # where <KEY> is the channel name upper-cased with non-alphanumerics → '_'.
    channels: Dict[str, TelegramChannel] = {}
    for name, ch in (tg.get("channels", {}) or {}).items():
        ch = ch or {}
        env_key = "".join(c if c.isalnum() else "_" for c in str(name)).upper()
        token = os.getenv(f"TELEGRAM_BOT_TOKEN_{env_key}",
                          str(ch.get("bot_token", "") or ""))
        # An EMPTY chat_id means "not set", so it must fall back to the shared
        # telegram.chat_id. (`.get(key, default)` would NOT: settings.yaml ships
        # every channel with an explicit `chat_id: ""` placeholder, so the key
        # exists and the default never applied — leaving every channel
        # ready=False and silently routing all alerts to a no-op notifier even
        # when the tokens were correctly filled in.)
        chat = os.getenv(f"TELEGRAM_CHAT_ID_{env_key}",
                         str(ch.get("chat_id") or default_chat or ""))
        channels[str(name)] = TelegramChannel(bot_token=token, chat_id=chat)

    return NotificationsConfig(
        enabled=bool(raw.get("enabled", False)),
        telegram_bot_token=legacy_token,
        telegram_chat_id=legacy_chat,
        channels=channels,
    )
