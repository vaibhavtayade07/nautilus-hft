#!/usr/bin/env python3
"""LiveNode builder: Bybit TESTNET data + execution clients.

Mirrors the proven pattern from the reference checkout
(``connect_bybit.py`` / ``examples/live/bybit``): ``LINEAR`` product type,
``TESTNET`` environment, ``BYBIT-001`` account, reconciliation enabled.
Credentials come from :class:`config.settings.Settings` (``.env`` or
``BYBIT_TESTNET_*`` env vars) — never hardcoded.
"""

from __future__ import annotations

from nautilus_trader.core.nautilus_pyo3 import (
    AccountId,
    BybitDataClientConfig,
    BybitDataClientFactory,
    BybitEnvironment,
    BybitExecClientConfig,
    BybitExecutionClientFactory,
    BybitProductType,
    Environment,
    LiveNode,
    TraderId,
    TransportBackend,
)

from config.settings import Settings
from config.settings import SettingsError


TRADER_ID = TraderId("HFT-001")
ACCOUNT_ID = AccountId("BYBIT-001")
NODE_NAME = "BYBIT-TESTNET-HFT"


def build_node(settings: Settings) -> LiveNode:
    """Build (but do not run) the Bybit TESTNET live node. Fail-closed on creds."""
    if not settings.api_key or not settings.api_secret:
        raise SettingsError(
            "Cannot build node without Bybit testnet credentials "
            "(BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET). "
            "No network attempt was made.",
        )

    transport = (
        TransportBackend.SOCKUDO
        if settings.transport_backend == "SOCKUDO"
        else TransportBackend.TUNGSTENITE
    )

    data_config = BybitDataClientConfig(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        product_types=[BybitProductType.LINEAR],
        environment=BybitEnvironment.TESTNET,
        recv_window_ms=settings.recv_window_ms,
        heartbeat_interval_secs=settings.heartbeat_interval_secs,
        transport_backend=transport,
    )
    exec_config = BybitExecClientConfig(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        product_types=[BybitProductType.LINEAR],
        environment=BybitEnvironment.TESTNET,
        account_id=ACCOUNT_ID,
        recv_window_ms=settings.recv_window_ms,
        heartbeat_interval_secs=settings.heartbeat_interval_secs,
        transport_backend=transport,
    )

    return (
        LiveNode.builder(NODE_NAME, TRADER_ID, Environment.LIVE)
        .with_reconciliation(reconciliation=True)
        .add_data_client(None, BybitDataClientFactory(), data_config)
        .add_exec_client(None, BybitExecutionClientFactory(TRADER_ID, ACCOUNT_ID), exec_config)
        .build()
    )
