# quant-trader

Research, backtesting and paper-trading platform for **US** and **Chilean** equities.
Runs entirely on your machine, on free data sources, with SQLite. Total cost: **$0**.

> **Read this first.** This system measures what prices *did*. It does not forecast.
> Nothing in it establishes that a strategy is profitable, and it is deliberately
> built to make a weak strategy look weak: transaction costs are mandatory, the
> optimiser is walled off from test data, and every indicator is checked for
> look-ahead bias. Live order routing is **not implemented** — `LIVE_TRADING=true`
> is rejected at startup rather than ignored.

**Status: Phase 1 of 8 complete** — data foundation, database and indicators.

Free Chilean data sources were surveyed separately; see
[docs/chilean_data_sources.md](docs/chilean_data_sources.md) for what exists and
what turned out not to.

---

## Table of contents

- [What works today](#what-works-today)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Architecture](#architecture)
- [Data sources and cost](#data-sources-and-cost)
- [Limitations you must know about](#limitations-you-must-know-about)
- [Testing](#testing)
- [Roadmap](#roadmap)

---

## What works today

| Capability | Status |
|---|---|
| Free data providers for US and Chile, behind one abstraction | Done |
| Symbol mapping with empirical resolution (18/18 Chilean names verified) | Done |
| SQLite database, 13 tables, PostgreSQL-ready schema | Done |
| Incremental downloads, gap detection, stale-quote detection | Done |
| 40 technical indicators, all verified free of look-ahead bias | Done |
| CLI for data operations | Done |
| REST API + dark-mode dashboard showing stored data and indicators | Done |
| 328 tests, including look-ahead, leakage and stale-quote detection | Done |
| Strategy engine, scanner, backtester | Phase 2 |
| Optimisation, train/validation/test, walk-forward | Phase 4 |
| Historical analogues and statistical scenarios | Phase 5 |
| Paper trading | Phase 6 |

Commands belonging to later phases are registered and **refuse to run**, naming
the phase they belong to. Nothing prints a fabricated result.

---

## Quick start

### Prerequisites

Python 3.12+, Node.js 20+, Git. All free.

```powershell
# If you don't have them (Windows):
winget install -e --id Python.Python.3.12 --scope user
winget install -e --id OpenJS.NodeJS.LTS
winget install -e --id Git.Git
```

### Backend

```powershell
cd quant-trader

python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt

Copy-Item .env.example .env           # macOS/Linux: cp .env.example .env

python -m app init-db                 # create schema, register 35 assets
python -m app download-data --start 2016-01-01
python -m app coverage                # see what you actually have
```

The download takes roughly 5–8 minutes for the full 34-instrument universe. Each
symbol is committed as it lands, so an interruption keeps the work already done —
rerun the same command and it resumes incrementally.

### Frontend

```powershell
cd frontend
npm install
npm run dev              # http://localhost:5173
```

In a second terminal, with the venv active:

```powershell
python -m app serve      # http://127.0.0.1:8000
```

Vite proxies `/api/*` to the backend, so no URL configuration is needed.

---

## Commands

**Implemented:**

| Command | What it does |
|---|---|
| `python -m app init-db` | Create the schema and register markets and the universe |
| `python -m app download-data` | Download bars (incremental by default; `--full` to redo) |
| `python -m app coverage` | What data is stored, per instrument |
| `python -m app audit` | Gaps, duplicates, staleness, carried-forward quotes |
| `python -m app verify-symbols` | Re-probe provider mappings against live responses |
| `python -m app features SYMBOL` | Latest indicator values for one instrument |
| `python -m app universe` | The configured universe and each market's benchmark |
| `python -m app providers` | Providers, capabilities and cost |
| `python -m app limitations` | Everything this system cannot tell you |
| `python -m app serve` | Run the API |

Useful flags: `--market USA|CHILE`, `--symbols AAPL,SQM-B`, `--timeframe 1D|1H|15m|5m`,
`--start`/`--end`, `--full`.

**Registered but refusing to run:** `scan`, `backtest`, `optimize`, `walk-forward`,
`project`, `paper`, `report`.

---

## Architecture

```
quant-trader/
├── backend/
│   ├── app/
│   │   ├── config.py           Settings from .env, with safety validators
│   │   ├── core/
│   │   │   ├── markets.py      Market definitions: currency, tz, sessions
│   │   │   ├── universe.py     Assets + provider symbol mapping + benchmarks
│   │   │   ├── exceptions.py   Named failure modes
│   │   │   └── logging.py      Console + audit trail, decision logging
│   │   ├── data/
│   │   │   ├── provider.py     DataProvider ABC, validation, timeframes
│   │   │   ├── yfinance_provider.py
│   │   │   ├── chile_provider.py   .SN handling + illiquidity flags
│   │   │   ├── registry.py     Provider lookup by market
│   │   │   └── engine.py       Downloads, gaps, staleness, coverage
│   │   ├── database/
│   │   │   ├── base.py         Engine, session, SQLite pragmas
│   │   │   ├── models.py       13 ORM models with indexes and constraints
│   │   │   └── repository.py   Idempotent upserts, adjusted-price loading
│   │   ├── indicators/
│   │   │   ├── base.py         Wilder RMA, safe division, validation
│   │   │   ├── trend.py        SMA/EMA, trend score
│   │   │   ├── momentum.py     RSI, MACD, ROC
│   │   │   ├── volatility.py   ATR, rolling vol, Bollinger
│   │   │   ├── volume.py       Volume SMA, relative volume, spikes
│   │   │   ├── price.py        Returns, 52-week distances
│   │   │   ├── forward.py      QUARANTINED look-ahead outcome labels
│   │   │   └── registry.py     compute_features() — one vocabulary
│   │   ├── strategies|backtesting|optimization|
│   │   │   projections|portfolio|execution|risk/     (Phase 2+)
│   │   ├── api/main.py         FastAPI endpoints
│   │   └── __main__.py         CLI
│   └── tests/                  328 tests
├── frontend/                   React + TypeScript + Vite + Tailwind + Recharts
├── data/                       SQLite database (gitignored)
├── reports/                    Generated reports (gitignored)
├── docs/
│   ├── symbol_verification.md   What was probed, and what resolved
│   └── chilean_data_sources.md  Free-source survey: what exists, what does not
```

### Design decisions worth knowing

**Daily bars are re-stamped to midnight UTC of the exchange-local session date.**
Yahoo stamps them at local midnight, so the same session lands at 05:00 UTC for New
York and 03:00 UTC for Santiago. Without normalisation a multi-market portfolio can
never join two markets on the same day.

**Nothing is ever interpolated.** A bad bar is dropped and counted; a gap stays a
gap. Fabricated prices produce results that look plausible and are wrong, which is
strictly worse than a visible hole.

**Look-ahead prevention is structural, not aspirational.** Forward-looking
quantities live in one quarantined module, are prefixed `forward_`, are excluded
from `compute_features()` by default, and the strategy engine calls a guard that
raises `DataLeakageError` if any appear. The test suite verifies truncation
invariance across every feature *and* verifies that the same check correctly fails
on the forward columns — so the detector cannot silently become vacuous.

**Transaction costs have no zero default.** Zero friction is not a conservative
assumption, it is a wrong one, and the error grows with turnover. Chile's
placeholder costs are set higher than the US on purpose.

**Split provenance is persisted.** Every backtest row records which partition it
read, and a database `CHECK` constraint prevents an `OptimizationRun` from claiming
anything other than `train`.

---

## Data sources and cost

| Dependency | Licence | Cost | Notes |
|---|---|---|---|
| yfinance | Apache-2.0 | **Free** | No API key. Unofficial Yahoo endpoints, no SLA |
| Banco Central de Chile API BDE | n/a (public service) | **Free** | Optional. Free registration required. Carries the IPSA. Attribution required |
| pandas / numpy | BSD-3 | **Free** | |
| SQLAlchemy | MIT | **Free** | SQLite stdlib driver; PostgreSQL-ready |
| pydantic / pydantic-settings | MIT | **Free** | |
| FastAPI / uvicorn | MIT / BSD-3 | **Free** | |
| typer / rich | MIT | **Free** | |
| Jinja2 | BSD-3 | **Free** | |
| pytest / pytest-cov | MIT | **Free** | |
| React / Vite / Tailwind / Recharts | MIT | **Free** | |
| SQLite | Public domain | **Free** | |

**Deliberately excluded:** TA-Lib (needs a compiled C library — indicators are
implemented in pandas/numpy instead), `alpaca-py` (only for optional paper trading),
`scikit-optimize` (only for optional Bayesian optimisation), `psycopg` (only for
PostgreSQL). No paid API, database, cloud service or market-data feed is wired in
anywhere.

---

## Limitations you must know about

Run `python -m app limitations`, or open the Limitations page in the dashboard.
The most important ones:

### 1. Survivorship bias (high)

The universe lists companies that exist **today**. Firms that were delisted, went
bankrupt or were acquired are absent, so any backtest over this universe is measured
only on survivors and is **optimistic by an unknown amount**. Free data cannot fix
this. It is recorded on every backtest row and printed in every report.

### 2. The IPSA needs a free Banco Central account (medium)

Yahoo serves nothing for the Chilean index: six spellings were probed on 2026-09-27
(`^IPSA`, `IPSA.SN`, `^SPIPSA`, `^SPCLXIPSA`, `^CLX`, `IPSA`) and **all returned zero
rows**.

The **Banco Central de Chile API BDE** does carry it, free of charge, after a free
registration (email + password — no card, no tier). Set `BCCH_USER` and
`BCCH_PASSWORD` in `.env`; see [docs/chilean_data_sources.md](docs/chilean_data_sources.md).

Until you do, the Chilean benchmark falls back to **`ECH`**, a USD-denominated
NYSE-listed ETF — so a CLP strategy compared against it is partly being measured on
currency moves it never made. The code refuses to call ECH "IPSA", and a test
enforces that.

### 3. Yahoo's Chilean feed stalled at Fiestas Patrias (high)

**Measured on the downloaded data, 2026-09-27: all 18 Chilean instruments end in flat
zero-volume bars.** All 15 US instruments are clean.

| Instrument | Last real print | Fabricated bars after it |
|---|---|---|
| SQM-B | 2026-07-17 | 49 |
| ANDINA-B | 2026-07-15 | 49 |
| The other 16 | **2026-09-17** | **5** |

Sixteen instruments stopping on the same date is not sixteen coincidences. Thursday
2026-09-17 was the last real session; Friday the 18th was Fiestas Patrias with the
market closed; Monday 21 through Friday 25 were five sessions that *did* trade and for
which Yahoo returned the 17th's close with zero volume. The feed stalled at the
holiday and did not resume.

This is more dangerous than ordinary stale data, because the bars are *dated today*
and pass any freshness check based on timestamps. The handling is three-part:

1. **Detect** the trailing run of flat zero-volume bars.
2. **Trim** it before computing anything, so no indicator ever sees an invented price.
   Feature extraction does this by default and reports how many bars it removed.
3. **Judge freshness on the last real print.** `assert_fresh()` strips the invented
   tail and applies the age tolerance to what remains — stricter than a blanket ban
   for a name that is genuinely dead, and more permissive for one that just had a
   quiet couple of sessions.

**Current effect: all 18 Chilean instruments are blocked for live signal generation**,
because their last real print is ten days old. That is the correct answer, not a bug.
Historical backtesting over 2016–2026 is unaffected — the ~2,500 real bars per
instrument remain.

Trimming visibly repaired the feature set. Before, on SQM-B: `volume_sma_20 = 0`,
`relative_volume_20 = NaN`, `bb_width = 0.0041`, feature set incomplete. After:
`335,986`, `3.77`, `11.71`, complete. Every one of those defects traced back to the
fabricated tail.

### 4. Transaction costs are placeholders (high)

The values in `.env` are guesses, **not your broker's schedule**. Replace them before
believing any net return.

### 5. No exchange-holiday calendar (medium)

There is no free, reliable holiday calendar for the Bolsa de Santiago, so a missing
bar cannot be distinguished from a closed session. Gap reports list **candidates**,
never confirmed gaps. For reference, the downloaded data shows ~102 missing weekdays
over 10.7 years for US instruments (consistent with market holidays) and 189–322 for
Chilean ones (more holidays plus genuine no-print days).

### 6. Unofficial data source (medium)

`yfinance` scrapes endpoints Yahoo publishes for its own website. No SLA, no support,
and adjusted prices are recomputed per request — so a historical bar can change
between downloads. The raw close is stored alongside `adj_close` so a change is at
least visible.

### 7. Instrument-specific caveats

- **LTM (LATAM Airlines)** — Chapter 11 completed 2022 with massive dilution. Pre- and
  post-2022 prices are not comparable.
- **ITAUCL** — repeated mergers (Corpbanca → Itaú Corpbanca → Itaú Chile).
- **MALLPLAZA** — history begins 2018-07-27, not at the 2016 IPO.
- **ENELCHILE** — history begins 2016-04-22, after the Enel Chile / Enel Americas split.
- **SQM-B** — Series B is the liquid local line; Series A trades separately.

### 8. Shallow intraday history (low)

60 days for 5/15-minute bars, 730 days for hourly. Not enough for a serious intraday
study. Daily is the priority everywhere in this project.

---

## Testing

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest                          # 328 tests (325 offline, 3 live)
python -m pytest -m network               # 3 live provider tests
python -m pytest --cov=backend/app        # with coverage
```

The suite is offline by default; provider behaviour is exercised through a test
double. Notable test groups:

- **`test_lookahead_bias.py`** — truncation invariance across every feature, an AST
  scanner that rejects `center=True`, `bfill` and negative shifts outside the
  quarantined module, plus two meta-tests proving the detectors can actually fail.
- **`test_stale_quotes.py`** — carried-forward quote detection, written after
  observing the real thing in the downloaded Chilean data.
- **`test_indicators.py`** — indicator values against hand-computed arithmetic, not
  against another library. RSI is checked against the Wilder definition
  (`100 − 300/14 = 78.5714…` for a known 14-period series).
- **`test_database.py`** — upsert idempotency, constraint enforcement, adjusted-price
  round trips, duplicate-order rejection.
- **`test_config.py`** — live-trading refusal, split-overlap refusal, non-zero costs.

---

## Roadmap

| Phase | Scope |
|---|---|
| **1** | **Data foundation, database, indicators — complete** |
| 2 | Strategy engine, scanner, backtester, metrics, HTML reports |
| 3 | Full dashboard |
| 4 | Optimisation, objective function, train/validation/test, walk-forward |
| 5 | Projection engine, historical analogues, robustness testing |
| 6 | Paper trading — Alpaca for US, internal broker for Chile |
| 7 | Telegram alerts |
| 8 | Production hardening |

Live trading is **not** on this roadmap as an enabled feature. A `BrokerAdapter`
interface exists so one can be added deliberately later.

---

## How this project talks about the future

It never says *"NVDA will rise 8%"*, *"this strategy is profitable"*, or *"score 0.75
means a 75% chance of a gain"*.

It says *"in 127 similar historical setups the median 20-day return was +3.4%"*,
*"over this sample, with these costs, the strategy returned X"*, *"score 0.75 means 3
of 4 conditions currently hold"*, and — when the evidence is thin — *"insufficient
historical evidence"*.

A score is an ordinal ranking of how many conditions hold. It is not a probability,
and the code is written so it cannot be presented as one.
