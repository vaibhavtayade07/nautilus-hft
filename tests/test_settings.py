#!/usr/bin/env python3
"""Pure unit tests for the .env parser + fail-closed settings loader.

No network, no nautilus_trader import: stdlib only.
Run from the project root: ``python -m pytest`` (or ``uv run pytest``).
"""

from __future__ import annotations

import pytest

from config.settings import (
    DEFAULT_DEPTH,
    DEFAULT_SAMPLES,
    DEFAULT_SYMBOLS,
    MAX_SAMPLES,
    SettingsError,
    load_dotenv_file,
    load_settings,
    parse_env_text,
)


def _write_env(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure credential + HFT_* vars do not leak in from the real environment."""
    for var in (
        "BYBIT_TESTNET_API_KEY",
        "BYBIT_TESTNET_API_SECRET",
        "HFT_SYMBOLS",
        "HFT_DEPTH",
        "HFT_TRANSPORT_BACKEND",
        "HFT_RECV_WINDOW_MS",
        "HFT_HEARTBEAT_INTERVAL_SECS",
        "HFT_SAMPLES",
        "HFT_TOB_OFFSET_TICKS",
        "HFT_QUOTE_TIMEOUT_SECS",
        "HFT_MIN_QTY_BTCUSDT",
        "HFT_MIN_QTY_ETHUSDT",
    ):
        monkeypatch.delenv(var, raising=False)


# -- parser ---------------------------------------------------------------

def test_parse_basic_values_comments_and_blanks():
    text = (
        "# comment\n"
        "\n"
        "BYBIT_TESTNET_API_KEY = abc123 \n"
        "BYBIT_TESTNET_API_SECRET= secret==with==equals \n"
    )
    assert parse_env_text(text) == {
        "BYBIT_TESTNET_API_KEY": "abc123",
        "BYBIT_TESTNET_API_SECRET": "secret==with==equals",
    }


def test_parse_strips_surrounding_quotes():
    text = 'A="double quoted"\nB=\'single quoted\'\nC=plain\n'
    assert parse_env_text(text) == {"A": "double quoted", "B": "single quoted", "C": "plain"}


def test_parse_invalid_line_raises():
    with pytest.raises(SettingsError, match="expected KEY=VALUE"):
        parse_env_text("THIS LINE HAS NO EQUALS\n")


def test_parse_export_prefix_rejected():
    with pytest.raises(SettingsError, match="expected KEY=VALUE"):
        parse_env_text("export BYBIT_TESTNET_API_KEY=abc\n")


def test_missing_dotenv_file_is_empty_dict(tmp_path):
    assert load_dotenv_file(tmp_path / "does-not-exist.env") == {}


def test_dotenv_file_syntax_error_reports_path(tmp_path):
    path = _write_env(tmp_path, "NO EQUALS HERE\n")
    with pytest.raises(SettingsError, match=r"\.env"):
        load_dotenv_file(path)


# -- fail-closed credentials ----------------------------------------------

def test_missing_credentials_fail_closed(clean_env, tmp_path):
    with pytest.raises(SettingsError, match="BYBIT_TESTNET_API_KEY"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=True)


def test_partial_credentials_name_the_missing_key(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "key-only")
    with pytest.raises(SettingsError, match="BYBIT_TESTNET_API_SECRET"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=True)


def test_empty_credentials_treated_as_missing(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "  ")
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", "")
    with pytest.raises(SettingsError, match="Missing Bybit testnet credentials"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=True)


def test_validate_mode_loads_without_credentials(clean_env, tmp_path):
    settings = load_settings(env_path=tmp_path / "missing.env", require_credentials=False)
    assert settings.api_key is None
    assert settings.api_secret is None
    assert not settings.has_credentials
    assert settings.symbols == DEFAULT_SYMBOLS
    assert settings.depth == DEFAULT_DEPTH
    assert settings.samples == DEFAULT_SAMPLES


def test_environ_overrides_dotenv_file(clean_env, tmp_path, monkeypatch):
    path = _write_env(
        tmp_path,
        "BYBIT_TESTNET_API_KEY=file-key\nBYBIT_TESTNET_API_SECRET=file-secret\n",
    )
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "env-key")
    settings = load_settings(env_path=path, require_credentials=True)
    assert settings.api_key == "env-key"
    assert settings.api_secret == "file-secret"
    assert settings.has_credentials


# -- value validation ------------------------------------------------------

def test_invalid_depth_rejected(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_DEPTH", "20")
    with pytest.raises(SettingsError, match="HFT_DEPTH"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=False)


def test_valid_depths_accepted(clean_env, tmp_path, monkeypatch):
    for depth in ("1", "50", "200", "500"):
        monkeypatch.setenv("HFT_DEPTH", depth)
        assert load_settings(
            env_path=tmp_path / "missing.env", require_credentials=False
        ).depth == int(depth)


def test_invalid_transport_rejected(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_TRANSPORT_BACKEND", "websockets")
    with pytest.raises(SettingsError, match="HFT_TRANSPORT_BACKEND"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=False)


def test_samples_bounds_enforced(clean_env, tmp_path, monkeypatch):
    for bad in ("0", str(MAX_SAMPLES + 1)):
        monkeypatch.setenv("HFT_SAMPLES", bad)
        with pytest.raises(SettingsError, match="HFT_SAMPLES"):
            load_settings(env_path=tmp_path / "missing.env", require_credentials=False)


def test_invalid_symbol_rejected(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_SYMBOLS", "BTCUSDT.BYBIT")
    with pytest.raises(SettingsError, match="Invalid symbol"):
        load_settings(env_path=tmp_path / "missing.env", require_credentials=False)


def test_redacted_view_never_contains_secrets(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "supersecretkey123")
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", "anothersecret456")
    settings = load_settings(env_path=tmp_path / "missing.env", require_credentials=True)
    view = repr(settings.redacted())
    assert "supersecretkey123" not in view
    assert "anothersecret456" not in view
