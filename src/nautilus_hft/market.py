#!/usr/bin/env python3
"""Market-data subscriptions + in-memory latency rings.

Subscribes to ``orderbook.<depth>`` deltas (default 50, per Bybit v5 public
order-book topics) and public trades for the configured symbols, and records
per-event ``(ts_event, ts_init)`` nanosecond pairs in bounded in-memory rings.
``ts_init - ts_event`` is the venue-to-local quote latency for that event.

No orders are placed here; see :mod:`nautilus_hft.benchmark`.
"""

from __future__ import annotations

import time

from collections import deque
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.nautilus_pyo3 import Strategy
from nautilus_trader.model import InstrumentId
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import BookType


NS_PER_MS = 1_000_000


def percentile_ns(samples: list[int], pct: float) -> int | None:
    """Nearest-rank percentile over integer-nanosecond samples."""
    if not samples:
        return None
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    import math

    rank = max(1, min(len(ordered), math.ceil((pct / 100.0) * len(ordered))))
    return ordered[rank - 1]


def describe_ns(samples: list[int]) -> dict[str, Any]:
    """count / p50 / p95 / max summary for a nanosecond sample list."""
    if not samples:
        return {"count": 0, "p50_ns": None, "p95_ns": None, "max_ns": None}
    return {
        "count": len(samples),
        "p50_ns": percentile_ns(samples, 50),
        "p95_ns": percentile_ns(samples, 95),
        "max_ns": max(samples),
    }


class LatencyRing:
    """Bounded ring of ``(ts_event_ns, ts_init_ns)`` pairs with latency stats."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._events: deque[tuple[int, int]] = deque(maxlen=maxlen)
        self.first_event_ns: int | None = None
        self.first_local_ns: int | None = None

    def record(self, ts_event: int, ts_init: int) -> int:
        """Record one event; returns the ``ts_init - ts_event`` latency (ns)."""
        if self.first_event_ns is None:
            self.first_event_ns = int(ts_event)
            self.first_local_ns = time.time_ns()
        self._events.append((int(ts_event), int(ts_init)))
        return int(ts_init) - int(ts_event)

    def latencies(self) -> list[int]:
        return [ts_init - ts_event for ts_event, ts_init in self._events]

    def stats(self) -> dict[str, Any]:
        summary = describe_ns(self.latencies())
        summary["first_event_ns"] = self.first_event_ns
        return summary

    def __len__(self) -> int:
        return len(self._events)


class MarketRecorderConfig(StrategyConfig):
    instrument_ids: list[InstrumentId]
    depth: int = 50
    max_ring: int = 10_000


class MarketRecorder(Strategy):
    """Subscribe book-deltas + trades; record first arrivals + latency rings."""

    def __init__(self, config: MarketRecorderConfig) -> None:
        super().__init__(config)
        self.books: dict[str, LatencyRing] = {}
        self.trades: dict[str, LatencyRing] = {}
        for instrument_id in config.instrument_ids:
            key = str(instrument_id)
            self.books[key] = LatencyRing(maxlen=config.max_ring)
            self.trades[key] = LatencyRing(maxlen=config.max_ring)

    def on_start(self) -> None:
        for instrument_id in self.config.instrument_ids:
            self.subscribe_book_deltas(
                instrument_id,
                book_type=BookType.L2_MBP,
                depth=self.config.depth,
            )
            self.subscribe_trades(instrument_id)
        self.log.info(
            f"Subscribed orderbook.{self.config.depth} + publicTrade for "
            f"{[str(i) for i in self.config.instrument_ids]}",
        )

    def on_book_deltas(self, deltas: OrderBookDeltas) -> None:
        ring = self.books.get(str(deltas.instrument_id))
        if ring is not None:
            ring.record(deltas.ts_event, deltas.ts_init)

    def on_trade(self, tick: TradeTick) -> None:
        ring = self.trades.get(str(tick.instrument_id))
        if ring is not None:
            ring.record(tick.ts_event, tick.ts_init)

    def has_first_quotes(self) -> bool:
        """True once every symbol has seen its first book delta + first trade."""
        books_ready = all(r.first_event_ns is not None for r in self.books.values())
        trades_ready = all(r.first_event_ns is not None for r in self.trades.values())
        return books_ready and trades_ready

    def summary(self) -> dict[str, Any]:
        return {
            symbol: {
                "book": self.books[symbol].stats(),
                "trades": self.trades[symbol].stats(),
            }
            for symbol in self.books
        }

    def on_stop(self) -> None:
        self.log.info(f"MarketRecorder summary: {self.summary()}")
