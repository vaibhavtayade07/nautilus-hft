#!/usr/bin/env python3
"""Fail-closed settings loader for the nautilus-hft testnet node.

Stdlib only (no third-party ``dotenv`` dependency) by design, so settings can
be loaded by ``--mode validate`` without the trading stack installed.

Precedence: ``os.environ`` > ``hft/.env`` > built-in defaults.

Fail-closed contract: when ``require_credentials=True`` (benchmark mode) and
any credential is missing/empty, :func:`load_settings` raises
:class:`SettingsError` with a remediation message. The caller must not attempt
any network activity after this exception. This module itself never touches the
network.
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

API_KEY_VAR = "BYBIT_TESTNET_API_KEY"
API_SECRET_VAR = "BYBIT_TESTNET_API_SECRET"
REQUIRED_CREDENTIALS = (API_KEY_VAR, API_SECRET_VAR)

ENVIRONMENT = "TESTNET"

DEFAULT_SYMBOLS = ("BTCUSDT-LINEAR.BYBIT", "ETHUSDT-LINEAR.BYBIT")

# Bybit v5 public order-book topics are orderbook.{1|50|200|500}; Nautilus
# passes `depth` through to the adapter, so only these values are accepted.
DEFAULT_DEPTH = 50
ALLOWED_DEPTHS = (1, 50, 200, 500)

DEFAULT_TRANSPORT_BACKEND = "TUNGSTENITE"
ALLOWED_TRANSPORT_BACKENDS = ("TUNGSTENITE", "SOCKUDO")

DEFAULT_RECV_WINDOW_MS = 5_000
DEFAULT_HEARTBEAT_INTERVAL_SECS = 20

DEFAULT_SAMPLES = 5
MAX_SAMPLES = 20  # safety cap: bounds testnet order flow per benchmark run

DEFAULT_TOB_OFFSET_TICKS = 500  # resting distance for post-only probe orders
DEFAULT_QUOTE_TIMEOUT_SECS = 120.0

# Minimum probe quantities per symbol (post-only LIMIT, far from touch).
DEFAULT_MIN_QTY = {
    "BTCUSDT-LINEAR.BYBIT": "0.001",
    "ETHUSDT-LINEAR.BYBIT": "0.01",
}


class SettingsError(Exception):
    """Missing or invalid configuration. Do not proceed to any network call."""


def parse_env_text(text: str) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` text (the ``hft/.env`` format).

    - Blank lines and ``#`` comments are ignored.
    - Surrounding single/double quotes on values are stripped.
    - Anything else without exactly ``KEY=VALUE`` shape is a hard error.
    """
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SettingsError(
                f"Invalid line {lineno} in .env (expected KEY=VALUE): {raw!r}",
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or key.startswith("export "):
            raise SettingsError(
                f"Invalid line {lineno} in .env (expected KEY=VALUE): {raw!r}",
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv_file(path: str | Path | None = None) -> dict[str, str]:
    """Read ``.env`` into a dict. A missing file is fine (env vars may apply)."""
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsError(f"Cannot read .env file at {env_path}: {exc}") from exc
    try:
        return parse_env_text(text)
    except SettingsError as exc:
        raise SettingsError(f"{env_path}: {exc}") from exc


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    api_secret: str | None
    environment: str = ENVIRONMENT
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    depth: int = DEFAULT_DEPTH
    transport_backend: str = DEFAULT_TRANSPORT_BACKEND
    recv_window_ms: int = DEFAULT_RECV_WINDOW_MS
    heartbeat_interval_secs: int = DEFAULT_HEARTBEAT_INTERVAL_SECS
    samples: int = DEFAULT_SAMPLES
    tob_offset_ticks: int = DEFAULT_TOB_OFFSET_TICKS
    quote_timeout_secs: float = DEFAULT_QUOTE_TIMEOUT_SECS
    min_qty: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MIN_QTY))

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def redacted(self) -> dict[str, object]:
        """Log-safe view: secrets masked, everything else plain."""

        def _mask(value: str | None) -> str:
            if not value:
                return "<missing>"
            if len(value) <= 8:
                return "****"
            return f"{value[:4]}...{value[-2:]} (len={len(value)})"

        return {
            "environment": self.environment,
            "symbols": list(self.symbols),
            "depth": self.depth,
            "transport_backend": self.transport_backend,
            "recv_window_ms": self.recv_window_ms,
            "heartbeat_interval_secs": self.heartbeat_interval_secs,
            "samples": self.samples,
            "tob_offset_ticks": self.tob_offset_ticks,
            "quote_timeout_secs": self.quote_timeout_secs,
            "min_qty": dict(self.min_qty),
            API_KEY_VAR: _mask(self.api_key),
            API_SECRET_VAR: "<set>" if self.api_secret else "<missing>",
        }


def _get_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid integer for {name}: {raw!r}") from exc


def _get_float(raw: str | None, name: str, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid number for {name}: {raw!r}") from exc


def load_settings(
    env_path: str | Path | None = None,
    require_credentials: bool = True,
) -> Settings:
    """Load settings from ``.env`` + environment (env wins). No network use.

    :param require_credentials: ``True`` for benchmark mode (fail closed when
        keys are missing); ``False`` for offline ``validate`` mode.
    """
    file_values = load_dotenv_file(env_path)

    def _get(name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name, file_values.get(name, default))  # noqa: PLW1508
        if value is not None:
            value = value.strip()
        return value or default if default is not None else value

    api_key = _get(API_KEY_VAR)
    api_secret = _get(API_SECRET_VAR)

    if require_credentials:
        missing = [name for value, name in ((api_key, API_KEY_VAR), (api_secret, API_SECRET_VAR)) if not value]
        if missing:
            raise SettingsError(
                "Missing Bybit testnet credentials: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and set them, or export "
                + "BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET. "
                + "No network attempt was made.",
            )

    symbols_raw = _get("HFT_SYMBOLS", ",".join(DEFAULT_SYMBOLS)) or ",".join(DEFAULT_SYMBOLS)
    symbols = tuple(s.strip() for s in symbols_raw.split(",") if s.strip())
    if not symbols:
        raise SettingsError("HFT_SYMBOLS is empty: configure at least one instrument.")
    for symbol in symbols:
        if not symbol.endswith(".BYBIT") or "-LINEAR" not in symbol:
            raise SettingsError(
                f"Invalid symbol {symbol!r}: expected e.g. 'BTCUSDT-LINEAR.BYBIT'.",
            )

    depth = _get_int(_get("HFT_DEPTH"), "HFT_DEPTH", DEFAULT_DEPTH)
    if depth not in ALLOWED_DEPTHS:
        raise SettingsError(
            f"Invalid HFT_DEPTH={depth}: Bybit order-book topics support {list(ALLOWED_DEPTHS)}.",
        )

    transport_backend = (_get("HFT_TRANSPORT_BACKEND", DEFAULT_TRANSPORT_BACKEND) or "").upper()
    if transport_backend not in ALLOWED_TRANSPORT_BACKENDS:
        raise SettingsError(
            f"Invalid HFT_TRANSPORT_BACKEND={transport_backend!r}: "
            f"expected one of {list(ALLOWED_TRANSPORT_BACKENDS)}.",
        )

    recv_window_ms = _get_int(_get("HFT_RECV_WINDOW_MS"), "HFT_RECV_WINDOW_MS", DEFAULT_RECV_WINDOW_MS)
    if recv_window_ms <= 0:
        raise SettingsError(f"Invalid HFT_RECV_WINDOW_MS={recv_window_ms}: must be > 0.")

    heartbeat_interval_secs = _get_int(
        _get("HFT_HEARTBEAT_INTERVAL_SECS"),
        "HFT_HEARTBEAT_INTERVAL_SECS",
        DEFAULT_HEARTBEAT_INTERVAL_SECS,
    )
    if heartbeat_interval_secs <= 0:
        raise SettingsError(
            f"Invalid HFT_HEARTBEAT_INTERVAL_SECS={heartbeat_interval_secs}: must be > 0.",
        )

    samples = _get_int(_get("HFT_SAMPLES"), "HFT_SAMPLES", DEFAULT_SAMPLES)
    if samples < 1 or samples > MAX_SAMPLES:
        raise SettingsError(
            f"Invalid HFT_SAMPLES={samples}: must be between 1 and {MAX_SAMPLES}.",
        )

    tob_offset_ticks = _get_int(
        _get("HFT_TOB_OFFSET_TICKS"),
        "HFT_TOB_OFFSET_TICKS",
        DEFAULT_TOB_OFFSET_TICKS,
    )
    if tob_offset_ticks < 1:
        raise SettingsError(f"Invalid HFT_TOB_OFFSET_TICKS={tob_offset_ticks}: must be >= 1.")

    quote_timeout_secs = _get_float(
        _get("HFT_QUOTE_TIMEOUT_SECS"),
        "HFT_QUOTE_TIMEOUT_SECS",
        DEFAULT_QUOTE_TIMEOUT_SECS,
    )
    if quote_timeout_secs <= 0:
        raise SettingsError(
            f"Invalid HFT_QUOTE_TIMEOUT_SECS={quote_timeout_secs}: must be > 0.",
        )

    min_qty = dict(DEFAULT_MIN_QTY)
    for symbol in symbols:
        short = symbol.split(".")[0].replace("-LINEAR", "")
        override = _get(f"HFT_MIN_QTY_{short}")
        if override:
            min_qty[symbol] = override
    for symbol, qty in min_qty.items():
        try:
            if not (qty and float(qty) > 0):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"Invalid min qty for {symbol}: {qty!r} (must be > 0).") from exc

    return Settings(
        api_key=api_key or None,
        api_secret=api_secret or None,
        symbols=symbols,
        depth=depth,
        transport_backend=transport_backend,
        recv_window_ms=recv_window_ms,
        heartbeat_interval_secs=heartbeat_interval_secs,
        samples=samples,
        tob_offset_ticks=tob_offset_ticks,
        quote_timeout_secs=quote_timeout_secs,
        min_qty=min_qty,
    )
