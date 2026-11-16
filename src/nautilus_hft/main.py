#!/usr/bin/env python3
"""CLI: ``--mode validate`` (offline checks, no network) or ``benchmark`` (testnet)."""

from __future__ import annotations

import argparse
import py_compile
import sys
import time

from pathlib import Path


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BENCHMARK_FAILED = 3

MARKER = Path("config") / "settings.py"


def project_root() -> Path:
    """Locate the ``hft`` project root (holds ``config/settings.py``)."""
    here = Path(__file__).resolve()
    candidates = [Path.cwd(), *here.parents]
    for candidate in candidates:
        if (candidate / MARKER).is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def cmd_validate() -> int:
    """Offline checks only: settings load, imports, compile sanity. NO network."""
    import config.settings as settings_mod

    print("[validate] loading settings (credentials NOT required)...")
    try:
        settings = settings_mod.load_settings(require_credentials=False)
    except settings_mod.SettingsError as exc:
        print(f"[validate] SETTINGS ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"[validate] settings OK: {settings.redacted()}")

    print("[validate] importing trading modules...")
    try:
        import nautilus_hft.benchmark  # noqa: F401
        import nautilus_hft.market  # noqa: F401
        import nautilus_hft.node  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - report any import failure
        print(f"[validate] IMPORT ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print("[validate] imports OK (nautilus_trader + strategies)")

    root = project_root()
    sources = sorted(
        p
        for p in (
            list((root / "config").rglob("*.py"))
            + list((root / "src").rglob("*.py"))
            + list((root / "tests").rglob("*.py"))
        )
        if ".venv" not in p.parts
    )
    if not sources:
        print(f"[validate] ERROR: no sources found under {root}", file=sys.stderr)
        return EXIT_USAGE
    print(f"[validate] compiling {len(sources)} files under {root}...")
    try:
        for path in sources:
            py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"[validate] COMPILE ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print("[validate] compile OK")

    assert settings.symbols, "symbols must not be empty"
    assert settings.depth in settings_mod.ALLOWED_DEPTHS
    assert 1 <= settings.samples <= settings_mod.MAX_SAMPLES
    print("[validate] sanity checks OK")
    print("VALIDATE OK — offline, no network used, no orders placed.")
    return EXIT_OK


def cmd_benchmark() -> int:
    """Boot the TESTNET node, run market + benchmark probes, clean shutdown."""
    from config.settings import SettingsError
    from config.settings import load_settings
    from nautilus_hft.benchmark import BenchmarkConfig
    from nautilus_hft.benchmark import BenchmarkStrategy
    from nautilus_hft.market import MarketRecorder
    from nautilus_hft.market import MarketRecorderConfig
    from nautilus_hft.node import build_node
    from nautilus_trader.model import InstrumentId

    try:
        settings = load_settings(require_credentials=True)
    except SettingsError as exc:
        print(f"SETTINGS ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"Environment : {settings.environment} (Bybit TESTNET)")
    print(f"Symbols     : {list(settings.symbols)}")
    print(f"Depth       : orderbook.{settings.depth}")
    print(f"Probes      : up to {settings.samples} post-only LIMIT, qty={settings.min_qty}")

    node = build_node(settings)
    instrument_ids = [InstrumentId.from_str(s) for s in settings.symbols]

    recorder = MarketRecorder(
        MarketRecorderConfig(
            strategy_id="RECORDER-001",
            instrument_ids=instrument_ids,
            depth=settings.depth,
        ),
    )
    strategy = BenchmarkStrategy(
        BenchmarkConfig(
            strategy_id="BENCH-001",
            order_id_tag="BENCH",
            instrument_ids=instrument_ids,
            depth=settings.depth,
            samples=settings.samples,
            quantities={s: settings.min_qty[s] for s in settings.symbols},
            tob_offset_ticks=settings.tob_offset_ticks,
        ),
    )
    node.add_strategy(recorder)
    node.add_strategy(strategy)

    deadline_secs = settings.quote_timeout_secs + settings.samples * 30 + 60
    print(f"Overall deadline: {deadline_secs:.0f}s. Starting node (Ctrl+C aborts)...")

    # 1.231.0: LiveNode is unsendable — start/poll/stop/dispose must all run on
    # this (creating) thread. poll() is the non-blocking host-loop integration.
    started = time.monotonic()
    try:
        node.start()
        while True:
            try:
                node.poll()
            except Exception as exc:  # noqa: BLE001 - poll failure fails the run
                strategy._fail(  # noqa: SLF001 - single owner shutdown signal
                    f"Node poll failed: {exc}",
                )
                break
            if strategy.done_event.is_set():
                break
            if time.monotonic() - started > deadline_secs:
                strategy._fail(  # noqa: SLF001 - single owner shutdown signal
                    f"Benchmark deadline exceeded ({deadline_secs:.0f}s) "
                    f"after {strategy.submitted_count} probes. "
                    "Possible causes: no testnet quotes, auth failure, or stuck acks.",
                )
                break
            if strategy.done_event.wait(timeout=0.1):
                break
    except KeyboardInterrupt:
        print("\nInterrupted by user; shutting down...", file=sys.stderr)
        strategy._fail("Interrupted by user")  # noqa: SLF001
    finally:
        try:
            node.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must proceed
            print(f"Warning: node.stop() raised (ignored): {exc}", file=sys.stderr)
        try:
            node.dispose()
        except Exception as exc:  # noqa: BLE001 - shutdown must proceed
            print(f"Warning: node.dispose() raised (ignored): {exc}", file=sys.stderr)

    print(strategy.format_report_text())
    if strategy.error is not None:
        print(f"BENCHMARK FAILED: {strategy.error}", file=sys.stderr)
        return EXIT_BENCHMARK_FAILED
    if not strategy.finished:
        print("BENCHMARK FAILED: did not complete cleanly.", file=sys.stderr)
        return EXIT_BENCHMARK_FAILED
    print("BENCHMARK OK.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nautilus-hft",
        description="Bybit TESTNET node + latency benchmark (Milestone 0+1).",
    )
    parser.add_argument(
        "--mode",
        choices=("validate", "benchmark"),
        default="validate",
        help="validate = offline checks only (default); benchmark = live testnet run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "benchmark":
        return cmd_benchmark()
    return cmd_validate()


if __name__ == "__main__":
    raise SystemExit(main())
