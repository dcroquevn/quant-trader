# quant-trader

Research, backtesting and paper-trading platform for **US** and **Chilean** equities.
Runs entirely on your machine, on free data sources, with SQLite. Total cost: **$0**.

> **Read this first.** This system measures what prices *did*. It does not forecast.
> Nothing in it establishes that a strategy is profitable, and it is deliberately
> built to make a weak strategy look weak: transaction costs are mandatory, the
> optimiser is walled off from test data, and every indicator is checked for
> look-ahead bias. Live order routing is **not implemented** — `LIVE_TRADING=true`
> is rejected at startup rather than ignored.

**Status: Phases 1–4 of 8 complete** — data foundation, indicators, strategy engine, scanner, backtester, metrics, HTML reports, dashboard, parameter optimisation, walk-forward analysis and robustness testing.

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
| Strategy engine with configurable trend/momentum/volume/volatility components | Done |
| Universe scanner that refuses to rank on broken data | Done |
| Event-driven backtester: costs, slippage, stops, targets, trailing, sizing | Done |
| Full metric set, benchmark comparison, standalone HTML reports | Done |
| Train/validation/test split guard (TEST refused unless finalising) | Done |
| 693 tests, including look-ahead, leakage and stale-quote detection | Done |
| Dark-mode dashboard: overview, scanner, backtest, asset and data pages | Done |
| Validated colour palette (CVD-checked) and a table view on every chart | Done |
| API contract tests covering every field the dashboard reads | Done |
| Parameter search with a configurable objective (not return alone) | Done |
| Train/validation/test separation, enforced in four independent places | Done |
| Walk-forward analysis: optimise, freeze, trade the next window, repeat | Done |
| Six robustness checks, including fragility and cost-breakeven | Done |
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
| `python -m app strategies` | Registered strategies and their parameters |
| `python -m app scan` | Rank the universe by the strategy's current reading |
| `python -m app backtest` | Backtest with costs, slippage and a benchmark |
| `python -m app report` | Backtest plus a standalone HTML report |
| `python -m app optimize` | Search TRAIN, select on VALIDATION |
| `python -m app walk-forward` | Rolling out-of-sample analysis |
| `python -m app robustness` | Stress-test one configuration |
| `python -m app runs` | List stored searches and studies |
| `python -m app serve` | Run the API |

Useful flags: `--market USA|CHILE`, `--symbols AAPL,SQM-B`, `--timeframe 1D|1H|15m|5m`,
`--start`/`--end`, `--full`.

**Registered but refusing to run:** `project` (Phase 5), `paper` (Phase 6).

Backtest flags: `--split full|train|validation|test`, `--strategy`, `--symbols`,
`--start`/`--end`, `--capital`, `--finalising`.

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
│   │   ├── strategies/
│   │   │   ├── base.py         Strategy ABC, Decision, leakage guard
│   │   │   ├── trend_momentum.py   The default strategy
│   │   │   ├── scanner.py      Universe ranking with data-quality gates
│   │   │   └── registry.py     Lookup and parameter validation
│   │   ├── backtesting/
│   │   │   ├── costs.py        Commission, spread, slippage, per market
│   │   │   ├── portfolio.py    Cash and position accounting
│   │   │   ├── engine.py       Event-driven loop, stops, targets, sizing
│   │   │   ├── metrics.py      Performance metrics and benchmark comparison
│   │   │   ├── runner.py       bars → features → backtest → metrics
│   │   │   └── report.py       Standalone HTML reports
│   │   ├── optimization/
│   │   │   ├── space.py        Parameter axes, grids, reproducible sampling
│   │   │   ├── objective.py    Weighted objective + fragility penalty
│   │   │   ├── search.py       TRAIN-only search, VALIDATION selection
│   │   │   ├── walkforward.py  Rolling windows with frozen parameters
│   │   │   ├── robustness.py   Six stress tests
│   │   │   └── store.py        Persisting runs for the dashboard to read
│   │   ├── projections|portfolio|
│   │   │   execution|risk/     (Phase 5+)
│   │   ├── api/main.py         FastAPI endpoints
│   │   └── __main__.py         CLI
│   └── tests/                  693 tests
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

**A signal on bar `t` fills at bar `t+1`'s open.** A decision made from a bar's close
cannot be executed at that same close — the close has already happened by the time you
know it. This makes every backtest here worse than a naive one, and it is the difference
between a measurement and a fantasy. A test builds a one-bar price spike and asserts the
engine *cannot* capture it.

**Intrabar ambiguity is resolved against the strategy.** When a bar's range contains both
the stop and the take-profit, daily data cannot say which came first, so the stop is
assumed to fill. A gap through the stop fills at the open, not the stop price — you
cannot be filled where the market never traded.

**The TEST split is refused by default.** `resolve_window("test")` raises
`DataLeakageError` unless `finalising=True` is passed explicitly, and the HTTP endpoint
returns 409 with no override at all. Reading the test partition during a search turns it
into a second validation set and leaves no out-of-sample estimate.

**The search cannot be pointed at the wrong data.** `run_search()` has no `split`
argument — the guard is the absence of the parameter, because an argument that can defeat
a guard eventually will. Four independent enforcements: no parameter, a hard-coded
`resolve_window("train")`, a `CHECK (split = 'train')` constraint on the table, and a
refusal in the persistence layer. A test asserts the parameter stays absent.

**The objective is not return.** Optimising for return alone reliably produces a strategy
that concentrates everything into a few lucky trades. The score weights return, Sharpe,
Sortino, drawdown, turnover, volatility and sample size, normalised with `tanh` so one
spectacular outlier cannot swamp every other term. Weights are configurable and five
presets ship.

**A spike is penalised.** After scoring, the top candidates have their immediate
*neighbours* evaluated, and a configuration that scores well only at its own exact
settings is marked down. Without that, a search reports the highest peak it found — and
the highest peak in a noisy landscape is noise.

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

---

## What the default strategy actually did

Measured, not claimed. `trend_momentum` on default parameters, with the configured cost
assumptions, over the real downloaded data. **These are not recommendations, and the
train figures carry no out-of-sample information at all.**

| | USA train<br>2016–2021 | USA validation<br>2022–2023 | Chile train<br>2016–2021 |
|---|---|---|---|
| Total return | +76.9% | +12.8% | **−19.9%** |
| Benchmark | +163.7% (SPY) | +2.7% (SPY) | −15.3% (ECH proxy) |
| CAGR | 10.0% | 6.3% | −3.6% |
| Volatility | 8.8% | 7.7% | 5.8% |
| Sharpe | 1.12 | 0.83 | **−0.61** |
| Max drawdown | −12.7% | −9.1% | −28.0% |
| Trades | 377 | 95 | 267 |
| Win rate | 39.5% | — | 24.3% |
| Profit factor | 1.74 | — | **0.71** |
| Exposure | 40.7% | 24.5% | 27.4% |
| Turnover (annualised) | 1,274% | 978% | 895% |

Three things worth reading carefully:

**It badly underperformed buy-and-hold in the US on total return** (+77% against SPY's
+164%) while taking far less risk — lower volatility, a third of the drawdown, and a
slightly better Sharpe. Whether that trade is worth making is a question about your
objectives, not about the numbers.

**The same strategy loses money in Chile.** Sharpe −0.61, profit factor 0.71, win rate
24%. Chilean round-trip costs are assumed at 1.0% against the US 0.08%, and at ~900%
annual turnover that alone consumes roughly 9% a year. This is the concrete answer to the
brief's instruction not to assume the optimal parameters are the same in both markets —
here the *unoptimised* ones are not even viable.

**Turnover is the headline risk to all of it.** Around 1,000% a year means friction
assumptions dominate the result. The US figures rest on an assumed 0.08% round trip; at a
realistic retail cost the edge largely disappears. Replace the placeholders in `.env` with
your broker's actual schedule before drawing any conclusion.

### What optimising it revealed

A 30-trial search on the USA TRAIN split, with the balanced objective, then the top five
re-run on VALIDATION:

| Rank on train | Train objective | Validation objective | Degradation |
|---|---|---|---|
| 1 | 2.235 | 1.097 | +1.14 |
| 2 | 2.214 | 0.505 | +1.71 |
| 3 | 2.171 | **−2.011** | +4.18 |
| 4 | 2.141 | 0.350 | +1.79 |
| 5 | 2.060 | **−1.953** | +4.01 |

**Every candidate degraded, and two went from a positive train score to a negative
validation score** — they lost money out of sample. The best surviving configuration
returned 4.3% on validation at Sharpe 0.73, against 2.2 on train.

Parameter stability was worse than the headline suggests: among the best trials
`rsi_min` and `max_holding_bars` each took three different values with only 33%
concentration. Either they do not matter or the search is fitting noise in them, and in
both cases the value it picked carries little information.

That is the system working as intended. It is not evidence the strategy is good; it is
evidence that a train-set winner is the most overfitted candidate available, which is why
the validation split exists and why `walk-forward` is the command to trust.

### What walk-forward found — the number to actually read

Seven rolling windows, 4 years train / 1 year test, re-optimising and freezing parameters
before each test year. Every figure below is out-of-sample with respect to the parameters
that produced it.

| Test year | OOS return | Sharpe | Max DD | Trades |
|---|---|---|---|---|
| 2020 | **+25.0%** | 2.96 | −7.1% | 51 |
| 2021 | +5.6% | 0.81 | −4.4% | 45 |
| 2022 | −0.4% | −0.67 | −0.7% | **2** |
| 2023 | +1.7% | 0.49 | −4.7% | 25 |
| 2024 | +4.3% | 0.82 | −5.9% | 22 |
| 2025 | +7.5% | 1.05 | −5.1% | 62 |
| 2026 | +0.3% | 0.11 | −3.6% | 24 |

**Stitched out-of-sample: +50.4% total, 6.25% CAGR, Sharpe 1.10, Sortino 1.65, max
drawdown −7.1%, volatility 5.7%, 231 trades.**

A Sharpe near 1.1 with a 7% worst drawdown, entirely out-of-sample, is the most defensible
result this project has produced. Three things qualify it, all of which the tool reported
on its own:

**2020 carries half of it.** Excluding that one window leaves +20.3% over six years — a
3.1% CAGR rather than 6.3%. 2020 was an exceptional year for trend following, and a
strategy whose out-of-sample record depends on one such year has been tested against one
regime, not several.

**The 2022 window took two trades.** Its frozen parameters (`rsi_min=45`,
`min_relative_volume=2.0`) were restrictive enough that it barely traded. A two-trade year
is not a measurement, and its −0.4% should be read as "no information" rather than as a
small loss.

**Four of six parameters changed in more than half the windows** — consistency 29% to 43%.
The procedure is re-fitting each period rather than converging on a stable setting, which
means the parameters it would choose for the next year carry little information. That is
the most important finding here, and it is not visible in the headline at all.

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
python -m pytest                          # 693 tests (690 offline, 3 live)
python -m pytest -m 'not slow'            # skip the minutes-long integration tests
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
- **`test_backtest_engine.py`** — the spike test described above, the equity/cash
  accounting identity after every fill, intrabar tie-breaks, gap fills, and sizing limits.
- **`test_costs.py`** — that friction always hurts. A round trip at an unchanged price
  must lose money, and slippage must not be charged twice.
- **`test_runner.py`** — the split guard, including that a split cannot be widened past
  its own bounds.
- **`test_metrics.py`** — metric values against hand arithmetic, and that undefined
  metrics return null rather than zero or infinity.
- **`test_optimization.py`** — that `run_search` has no `split` parameter, that the
  objective's components stay bounded, and that changing the weights actually changes the
  ranking.
- **`test_walkforward_robustness.py`** — that every test window begins strictly after its
  own training window ends, and that the concentration checks detect a result carried by
  one trade.
- **`test_strategies.py`** — that the engine's fast path agrees with the reference
  implementation bar by bar. It is 22x faster, and a fast path that disagreed would be
  worse than a slow one.

---

## Roadmap

| Phase | Scope |
|---|---|
| **1** | **Data foundation, database, indicators — complete** |
| **2** | **Strategy engine, scanner, backtester, metrics, HTML reports — complete** |
| **3** | **Dashboard — complete** |
| **4** | **Optimisation, objective function, walk-forward, robustness — complete** |
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
