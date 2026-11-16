#!/usr/bin/env python3
"""Order-path latency benchmark on Bybit TESTNET.

After first quotes arrive, places at most ``N`` (default 5) tiny post-only
LIMIT BUY orders per run, priced ``tob_offset_ticks`` below the best bid so
they rest instead of filling. For each probe it measures:

- ``submit -> accepted`` (order-entry latency, nanoseconds), then cancels
  immediately and measures
- ``cancel -> canceled`` (cancel latency, nanoseconds).

Prints p50/p95/max for both plus the quote-latency rings, then finishes.
Any venue rejection, unexpected fill, or error fails the run with a non-zero
exit and NO retry hammering (single attempt per probe, bounded ``N``).
"""

from __future__ import annotations

import threading
import time

from decimal import Decimal
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.nautilus_pyo3 import Strategy
from nautilus_trader.model import InstrumentId
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderDenied
from nautilus_trader.model.events import OrderExpired
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected

from nautilus_hft.market import LatencyRing
from nautilus_hft.market import NS_PER_MS
from nautilus_hft.market import describe_ns


class BenchmarkConfig(StrategyConfig):
    instrument_ids: list[InstrumentId]
    depth: int = 50
    samples: int = 5
    quantities: dict[str, str] | None = None  # symbol str -> qty str
    tob_offset_ticks: int = 500
    max_ring: int = 10_000


class BenchmarkStrategy(Strategy):
    """Sequential post-only probe orders with submit/cancel ack timing."""

    def __init__(self, config: BenchmarkConfig) -> None:
        super().__init__(config)
        self.books: dict[str, LatencyRing] = {}
        self.trades: dict[str, LatencyRing] = {}
        for instrument_id in config.instrument_ids:
            key = str(instrument_id)
            self.books[key] = LatencyRing(maxlen=config.max_ring)
            self.trades[key] = LatencyRing(maxlen=config.max_ring)
        self.quantities: dict[str, str] = dict(config.quantities or {})
        self.started_ns: int | None = None
        self.submitted_count = 0
        self.in_flight: tuple[str, int] | None = None  # (client_order_id, submit_ns)
        self.cancel_ns: int | None = None
        self.submit_acks_ns: list[int] = []
        self.cancel_acks_ns: list[int] = []
        self.finished = False
        self.error: str | None = None
        self.done_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def on_start(self) -> None:
        self.started_ns = time.time_ns()
        for instrument_id in self.config.instrument_ids:
            self.subscribe_book_deltas(
                instrument_id,
                book_type=BookType.L2_MBP,
                depth=self.config.depth,
            )
            self.subscribe_trades(instrument_id)
        self.log.info(
            f"Benchmark armed: up to {self.config.samples} post-only probes, "
            f"{self.config.tob_offset_ticks} ticks from touch, "
            f"qty={self.quantities}",
        )

    def on_stop(self) -> None:
        # Best-effort cleanup: never leave a resting probe behind on stop.
        if self.in_flight is not None and not self.finished:
            try:
                order = self.cache.order(self.in_flight[0])  # type: ignore[arg-type]
                if order is not None and order.is_open:
                    self.cancel_order(order.client_order_id)
                    self.log.warning(f"Cancelled leftover in-flight order {self.in_flight[0]}")
            except Exception as exc:  # noqa: BLE001 - shutdown path must not raise
                self.log.warning(f"Leftover-cancel failed (ignored): {exc}")
        self.log.info(f"Benchmark summary: {self.format_report()}")

    # -- market data -------------------------------------------------------
    def on_book_deltas(self, deltas: OrderBookDeltas) -> None:
        ring = self.books.get(str(deltas.instrument_id))
        if ring is not None:
            ring.record(deltas.ts_event, deltas.ts_init)
        self._maybe_submit(deltas.instrument_id)

    def on_trade(self, tick: TradeTick) -> None:
        ring = self.trades.get(str(tick.instrument_id))
        if ring is not None:
            ring.record(tick.ts_event, tick.ts_init)
        self._maybe_submit(tick.instrument_id)

    # -- probe sequencing --------------------------------------------------
    def _maybe_submit(self, instrument_id: InstrumentId) -> None:
        if self.error is not None or self.finished or self.in_flight is not None:
            return
        if self.submitted_count >= self.config.samples:  # hard cap: never exceed N
            return
        key = str(instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return  # wait for instrument + quote data, no polling hammer
        quote = self.cache.quote(instrument_id)
        if quote is None:
            return
        try:
            bid = Decimal(str(quote.bid_price))
            tick = Decimal(str(instrument.price_increment))
            raw = bid - (tick * self.config.tob_offset_ticks)
            if raw <= 0:
                self._fail(f"Non-positive probe price for {key}: bid={bid}")
                return
            floored = (raw // tick) * tick
            price = instrument.make_price(floored)
            qty_str = self.quantities.get(key)
            if not qty_str:
                self._fail(f"No probe quantity configured for {key}")
                return
            quantity = instrument.make_qty(Decimal(qty_str))
            order = self.order_factory.limit(
                instrument_id,
                OrderSide.BUY,
                quantity,
                price,
                time_in_force=TimeInForce.GTC,
                post_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - probe construction failure is fatal
            self._fail(f"Probe construction failed for {key}: {exc}")
            return

        client_order_id = str(order.client_order_id)
        submit_ns = time.time_ns()
        self.submit_order(order)
        self.in_flight = (client_order_id, submit_ns)
        self.submitted_count += 1
        self.log.info(
            f"Probe {self.submitted_count}/{self.config.samples}: BUY {quantity} "
            f"{key} @ {price} post-only (id={client_order_id})",
        )

    # -- order events ------------------------------------------------------
    def on_order_accepted(self, event: OrderAccepted) -> None:
        if self.in_flight is None or str(event.client_order_id) != self.in_flight[0]:
            return
        ack_ns = time.time_ns() - self.in_flight[1]
        self.submit_acks_ns.append(ack_ns)
        self.log.info(f"Probe accepted in {ack_ns}ns ({ack_ns / NS_PER_MS:.3f}ms)")
        order = self.cache.order(event.client_order_id)
        if order is None or not order.is_open:
            self._fail(f"Accepted probe {self.in_flight[0]} not open for cancel")
            return
        self.cancel_ns = time.time_ns()
        self.cancel_order(order.client_order_id)  # cancel immediately after ack

    def on_order_canceled(self, event: OrderCanceled) -> None:
        if self.in_flight is None or str(event.client_order_id) != self.in_flight[0]:
            return
        assert self.cancel_ns is not None
        ack_ns = time.time_ns() - self.cancel_ns
        self.cancel_acks_ns.append(ack_ns)
        self.log.info(f"Probe canceled in {ack_ns}ns ({ack_ns / NS_PER_MS:.3f}ms)")
        self.in_flight = None
        self.cancel_ns = None
        if len(self.cancel_acks_ns) >= self.config.samples:
            self.finished = True
            self.log.info("Benchmark complete")
            self.done_event.set()
        # else: next probe fires on the next market-data event (no polling)

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._fail(f"Probe rejected: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        self._fail(f"Probe denied: {event.reason}")

    def on_order_expired(self, event: OrderExpired) -> None:
        self._fail(f"Probe expired unexpectedly: {str(event.client_order_id)}")

    def on_order_filled(self, event: OrderFilled) -> None:
        # A resting far-from-touch post-only probe must never fill.
        self._fail(
            f"Probe filled unexpectedly (should rest): "
            f"{str(event.client_order_id)} qty={event.last_qty}",
        )

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        self._fail(f"Probe cancel rejected: {event.reason}")

    # -- helpers -----------------------------------------------------------
    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = message
            self.log.error(message)
            self.done_event.set()

    def format_report(self) -> dict[str, Any]:
        return {
            "probes_submitted": self.submitted_count,
            "submit_ack": describe_ns(self.submit_acks_ns),
            "cancel_ack": describe_ns(self.cancel_acks_ns),
            "markets": {
                symbol: {
                    "book": self.books[symbol].stats(),
                    "trades": self.trades[symbol].stats(),
                }
                for symbol in self.books
            },
        }

    def format_report_text(self) -> str:
        report = self.format_report()

        def _line(name: str, stats: dict[str, Any]) -> str:
            if not stats["count"]:
                return f"  {name}: no samples"
            p50 = stats["p50_ns"] / NS_PER_MS
            p95 = stats["p95_ns"] / NS_PER_MS
            mx = stats["max_ns"] / NS_PER_MS
            return (
                f"  {name}: n={stats['count']} "
                f"p50={p50:.3f}ms p95={p95:.3f}ms max={mx:.3f}ms"
            )

        lines = [
            "=== BENCHMARK REPORT (Bybit TESTNET) ===",
            f"Probes submitted: {report['probes_submitted']}",
            _line("submit->accepted", report["submit_ack"]),
            _line("cancel->canceled", report["cancel_ack"]),
        ]
        for symbol, market in report["markets"].items():
            lines.append(f"-- {symbol} --")
            lines.append(_line("quote latency (book)", market["book"]))
            lines.append(_line("trade latency", market["trades"]))
        return "\n".join(lines)
