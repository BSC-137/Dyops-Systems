# Dyops Systems — Basis Guard

> **Full-stack overview** (FastAPI, React, Binance feed, SQLite, deployment): see the repository root [`README.md`](../README.md).

Dyops is a **tokenized-asset basis monitoring** stack: a Rust + PyO3 **Kalman observer** for a mean-reverting OU basis model, Python **sentinel** orchestration with optional **Gemini** risk audits, and a **Streamlit** institutional terminal dashboard.

## Repository layout

| Path | Purpose |
|------|---------|
| `dyops_core/` | Rust crate **`dyops_core`** (Python import **`dyops_core`**) + Python modules |
| `dyops_core/src/` | `lib.rs` (PyO3), `observer.rs` (filter + ring buffer + batch API), `sentinel.rs` (breach/audit policy) |
| `dyops_core/sentinel.py` | Thin `DyopsSentinel` integration wrapper and `AgenticAuditor` (Google **Gen AI** unified SDK) |
| `dyops_core/dashboard.py` | Streamlit **Basis Guard** UI (Binance WebSocket + persistence) |
| `dyops_core/bench_batch.py` | Batch vs loop benchmark |
| `dyops_core/scenarios/` | Headless sentinel scenario catalog, runner, and CLI |
| `dyops_core/tests/` | Python unit, sentinel-policy, replay, and scenario-threshold tests |
| `dyops_core/database.py` | SQLite `PersistenceManager` (events + audits) |
| `dyops_core/binance_feed.py` | Binance Spot WebSocket feed (stable / LST) |
| `dyops_core/audits/` | JSON audit records (artifacts ignored by git; folder tracked) |
| `dyops_core/.streamlit/config.toml` | Streamlit theme |
| `docs/` | Methodology and validation-boundary documentation |
| `reports/` | Generated robustness evidence, including the [partner evidence pack](../reports/robustness_report.md) |
| `scripts/` | Robustness report generation and repository utility scripts |
| `.github/workflows/` | Pull-request and `main` validation workflows |

## Features (engineering summary)

### Rust core (`dyops_core` extension)

- **State** \(x = [b, v, \mu]\): log-basis, velocity, mean level; **critically damped OU** discrete map with mean-reversion speed **θ**.
- **Joseph-form** covariance update for numerical PSD safety.
- **API**: `BasisObserver`, `DyopsSentinelCore`, `update`, `update_batch` (NumPy `float64` arrays), ring buffer diagnostics, `get_window_stats`, `get_criticality_score`, `get_criticality_recent`, `get_last_innovations`, `get_basis_velocity`.

### Sentinel (`src/sentinel.rs`, `sentinel.py`)

- **Rust `SentinelPolicy` / `DyopsSentinelCore`**: owns the observer and applies breach (Mahalanobis > 3), rolling-criticality audit, and snapshot cooldown policy.
- **Python `DyopsSentinel`**: delegates each event to Rust and retains persistence, logging, callbacks, and optional async audit dispatch. **`AUDIT_COOLDOWN_TICKS=25`** limits repeated snapshots during a sustained AUDIT state to one every 25 processed ticks.
- **Telemetry naming**: `process_event` / `EventResult` (with `process_tick` / `TickResult` aliases).
- **AgenticAuditor**: **`google-genai`** client, model default **`gemini-3-flash`**, `GenerateContentConfig` with **`system_instruction`** and **`response_mime_type="application/json"`**; network I/O via **`asyncio.to_thread`**.

### Dashboard (`dashboard.py`)

- Glass-style **institutional terminal** UI, live **Plotly** basis vs innovation, sidebar **Intelligence feed** from `audits/`, **Binance** WebSocket feed feeding the sentinel (optional **SQLite** persistence).

## Prerequisites

- **Rust** (stable), **Python** 3.10+
- **Gemini** (optional, for auditor): `GEMINI_API_KEY` or `GOOGLE_API_KEY`

## Quick start

```bash
cd dyops_core

# Python env
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip maturin
pip install -e .   # or: pip install numpy loguru streamlit plotly google-genai websockets
```

Build and install the Rust/PyO3 extension (from `dyops_core/`):

```bash
maturin develop --release
```

That compiles the `dyops_core` native module and installs it into the active venv.

### Run the dashboard

```bash
cd dyops_core
streamlit run dashboard.py
```

### Run tests / bench

```bash
cargo test                 # Rust unit tests
python -m unittest discover -s tests -v
python bench_batch.py      # After maturin develop
python -m scenarios.run --all --quiet --summary  # Threshold-gated; exits 1 on failure
```

## Configuration

| Variable | Role |
|----------|------|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini (unified SDK) |
| `DYOPS_GEMINI_MODEL` | Override model id (default `gemini-3-flash`) |
| `DYOPS_SQLITE_PATH` | SQLite database path (optional) |
| `DYOPS_BINANCE_FEED` | `stable` or LST mode aliases (see `binance_feed.py`) |

## License / compliance notes

- **Export Compliance Report** in the UI is a **placeholder** for a future signed/regulator-facing bundle.
- Do not commit **`audits/*.json`** or API keys; see `.gitignore`.

## Contributing

Use **`maturin develop --release`** when changing Rust. For UI work, prefer **`width="stretch"`** on `st.plotly_chart` (avoid deprecated `use_container_width`).
