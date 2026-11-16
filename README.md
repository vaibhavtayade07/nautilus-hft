# NautilusHFT

**Bybit testnet node + order-path latency lab, built on NautilusTrader.**

<p>
<img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
<img alt="NautilusTrader" src="https://img.shields.io/badge/NautilusTrader-1.231-1f6feb">
<img alt="Bybit" src="https://img.shields.io/badge/Bybit-TESTNET-F7A600">
<img alt="uv" src="https://img.shields.io/badge/uv-managed-DE5FE1">
<img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

Milestone 0+1 of the HFT-lite micro-scalp plan. **No strategy code yet**: this
project boots a NautilusTrader `LiveNode` against **Bybit TESTNET**, streams
`orderbook.50` + public trades for `BTCUSDT-LINEAR` / `ETHUSDT-LINEAR`, and
measures order-path latency with tiny post-only probe orders.

Reference patterns only were read from the sibling `nautilus_trader` checkout
(`connect_bybit.py`, `demo_bybit.py`, `examples/live/bybit/*`,
`docs/integrations/bybit.md`, `crates/adapters/bybit/src/config.rs`).
That repo is **never modified** by this project.

## Dependency approach: PyPI wheel (not a local path)

`pyproject.toml` depends on **`nautilus_trader==1.231.0` from PyPI**.
Why: the reference checkout's `.venv` resolves `nautilus_trader` from its
`site-packages` (prebuilt wheel, verified via
`.venv/Scripts/python.exe -c "import nautilus_trader; print(...)"`), so a
local editable path (`../nautilus_trader/python`) would force a full Rust
rebuild through that repo's toolchain. The pinned PyPI wheel is the working,
portable approach; bump it deliberately after validating a new release.

## Setup

Requirements: Python ≥ 3.13, [uv](https://docs.astral.sh/uv/).

```bash
cd hft
uv sync                      # create .venv + install nautilus_trader wheel
cp .env.example .env          # then fill in testnet keys (never commit .env)
```

Create a testnet key (desktop browser):

1. Open [testnet.bybit.com](https://testnet.bybit.com), sign in / register
   (new accounts may wait ~48h before API-key creation works).
2. **Assets → Assets Overview → Request Test Coins** so the account is funded.
3. **API Management** → **Create New Key**.
4. Permissions: **Orders + Positions = Read-Write** (contracts), **Withdrawals = OFF**.
5. Copy the key/secret into `.env` (or export `BYBIT_TESTNET_API_KEY` /
   `BYBIT_TESTNET_API_SECRET`; env vars override `.env`).

## Run

```bash
uv run nautilus-hft --mode validate     # offline checks only (default)
uv run nautilus-hft --mode benchmark    # live TESTNET benchmark (needs keys)
# equivalent: uv run python -m nautilus_hft.main --mode benchmark
uv run pytest                           # offline unit tests
```

`validate` passes with **no network and no `.env`** (credentials optional,
nothing connects). `benchmark` is fail-closed: missing keys → clear error,
no network attempt.

## What the benchmark does

1. Builds the node: `BybitDataClientConfig` + `BybitExecClientConfig` with
   `product_types=[LINEAR]`, `environment=TESTNET`, `account_id=BYBIT-001`,
   reconciliation on, configurable `transport_backend`
   (`TUNGSTENITE` default, `SOCKUDO` optional) and `recv_window_ms`.
2. `MarketRecorder` subscribes `orderbook.50` deltas + public trades per
   symbol, recording first-arrival times and per-event
   `ts_init − ts_event` (ns) latency rings.
3. `BenchmarkStrategy` waits for first quotes, then places up to `N=5`
   (`HFT_SAMPLES`, capped at 20) post-only LIMIT BUYs at
   `bid − 500 ticks` (`HFT_TOB_OFFSET_TICKS`), min qty
   (`0.001` BTC / `0.01` ETH, overridable per symbol). Each acked probe is
   cancelled immediately.
4. Prints `submit→accepted` and `cancel→canceled` **p50/p95/max** plus
   quote/trade latency stats, then shuts the node down cleanly.

## Interpreting results

- `submit→accepted` ≈ order-entry path (strategy → trade WebSocket
  `wss://stream-testnet.bybit.com/v5/trade` → ack), the number that matters
  for micro-scalp feasibility. Expect testnet to be slower/noisier than
  mainnet; compare runs, not single samples.
- `cancel→canceled` ≈ cancel path; routinely faster than submit.
- `quote latency (book)` ≈ `ts_init − ts_event` on book deltas — one-way
  venue→local feed delay (Bybit pushes depth-1 quotes at ~10ms cadence).
- `n < N` or p95 ≫ p50 → investigate (rate limits, `retCode 10006`, WiFi,
  `recv_window_ms` too tight) before sizing anything.

## Safety notes

- **TESTNET only.** `TESTNET` is hardcoded (`config/settings.py`); no flag
  can point this at mainnet.
- Probe orders are post-only, minimum size, 500 ticks from touch — they rest
  and are cancelled on ack. An unexpected fill aborts the run (non-zero exit).
- Bounded by design: at most `N` submits, one in flight at a time, no retry
  loops, overall deadline with clean `node.stop()`/`dispose()` shutdown.
- Stoped runs cancel any leftover in-flight probe best-effort on shutdown.
- Never put mainnet keys in `.env`; withdrawals stay OFF on the testnet key.
- Bybit IP rate ceiling (600 req/5s, 10-min ban on breach) is venue-side;
  this tool's tiny request volume stays far below it — do not parallelize
  benchmark runs against the same key/IP.
