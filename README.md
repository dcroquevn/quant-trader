# quant-trader

Research, backtesting and position-tracking platform for **US, Chilean and emerging-Asian**
equity exposure. Runs on your machine or on GitHub's, on free data sources, with SQLite.
Total cost: **$0**.

Every instrument is **US-listed and priced in USD**, including the Chilean and Asian ones —
those are ADRs and country ETFs, not local shares. Their returns carry the currency move as
well as the underlying move, and nothing here separates the two. See
[Why there are no Santiago tickers](#why-there-are-no-santiago-tickers).

> **Read this first.** This system measures what prices *did*. It does not forecast.
> Nothing in it establishes that a strategy is profitable, and it is deliberately
> built to make a weak strategy look weak: transaction costs are mandatory, the
> optimiser is walled off from test data, and every indicator is checked for
> look-ahead bias. Live order routing is **not implemented** — `LIVE_TRADING=true`
> is rejected at startup rather than ignored.

**Status: Phases 1–6 of 8 complete** — data foundation, indicators, strategy engine, scanner,
backtester, metrics, HTML reports, dashboard, parameter optimisation, walk-forward analysis,
robustness testing, historical analogues, statistical scenarios, real-position tracking and
Telegram alerts.

Phase 6 is built for a **manual workflow**: you buy through your own broker's app, record what
you bought, and the daily check tells you when the strategy's exit rule fires on it. This
software has no broker connection and places no orders. See
[Tracking real positions](#tracking-real-positions).

It can run daily on GitHub Actions so your computer does not have to be on —
[docs/deployment.md](docs/deployment.md), including the trade-off that decides whether you get a
public dashboard or private alerts, and
[what you actually open](docs/deployment.md#what-you-actually-open) (there is no dashboard file in
the repository; GitHub renders an HTML file as source code, not as a page).

Free Chilean data sources were surveyed separately; see
[docs/chilean_data_sources.md](docs/chilean_data_sources.md) for what exists and
what turned out not to.

---

## Table of contents

- [What works today](#what-works-today)
- [What the digest looks like](#what-the-digest-looks-like)
- [What horizon this operates on](#what-horizon-this-operates-on)
- [Why there are no Santiago tickers](#why-there-are-no-santiago-tickers)
- [Quick start](#quick-start)
- [Tracking real positions](#tracking-real-positions)
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
| Free data providers behind one abstraction | Done |
| Symbol mapping with empirical resolution, verified against live responses | Done |
| 42 instruments across three exposure regions, all purchasable | Done |
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
| 898 tests, including look-ahead, leakage and stale-quote detection | Done |
| Dark-mode dashboard: overview, scanner, backtest, asset and data pages | Done |
| Validated colour palette (CVD-checked) and a table view on every chart | Done |
| API contract tests covering every field the dashboard reads | Done |
| Parameter search with a configurable objective (not return alone) | Done |
| Train/validation/test separation, enforced in four independent places | Done |
| Walk-forward analysis: optimise, freeze, trade the next window, repeat | Done |
| Six robustness checks, including fragility and cost-breakeven | Done |
| Historical analogues with overlap correction and a base-rate comparison | Done |
| Percentile scenarios that refuse to be forecasts | Done |
| Recording real positions bought through your own broker | Done |
| Daily exit-rule watch, replaying the backtester's own logic | Done |
| Telegram alerts, with delivery recorded separately from the trigger | Done |
| Real P&L on closed positions, reported with how little it establishes | Done |
| Self-contained HTML digest, publishable to GitHub Pages | Done |
| Scheduled daily run on GitHub Actions | Done |
| Broker order routing | **Not implemented, not planned** |

Commands belonging to later phases are registered and **refuse to run**, naming
the phase they belong to. Nothing prints a fabricated result.

---

## What the digest looks like

One self-contained HTML file — no server, no build step, no network — built by
`python -m app digest` and attached to every scheduled run.

| Section | Form | Why that form |
|---|---|---|
| Today's signals | Cards with the conditions that fired | The only part that might prompt an action, so it goes first and shows its reasoning. A signal you cannot interrogate is one you either obey blindly or ignore |
| 20-session return | Diverging bars, one chart per region | Polarity. Split by region because colour already encodes gain/loss and cannot also carry region |
| Regional benchmarks | Lines rebased to 100 | Change over time across instruments trading at \$38, \$187 and \$764. Rebasing is what lets them share one axis; a second y-scale would let the crossover be placed anywhere by choosing the scales |
| Traded value | Log-scale bars, threshold drawn | Magnitude over four orders of magnitude. On a linear axis everything but SPY is an invisible sliver |
| Per-region tables | Sparkline per row | Small multiples: "what has this been doing" without a second page |

Charts are inline SVG with no script, because the page has to open from a `file://` URL and from a
phone with no network. The palette is the project's, re-validated against the dark panel surface:
lightness band, chroma floor, all-pairs CVD separation (worst ΔE 9.4 deutan), normal-vision floor
(20.9) and 3:1 contrast all pass.

Two things found only by rendering it and looking: at a 680-unit viewBox the instrument labels
came out around 7px on a phone, and the benchmark end labels read "Emerging Asia (AAXJ) 134",
three times the right margin, so all three were clipped mid-word. Both fixed; the lesson is that
the validator checks colour, not layout.

---

## What horizon this operates on

Measured, not intended. 915 trades on TRAIN across all three regions:

| | days held |
|---|---|
| 10th percentile | 2 |
| 25th percentile | 5 |
| **median** | **16** |
| 75th percentile | 34 |
| 90th percentile | 54 |

Only 33% of positions close within a week; 71% close within a month.

**Where the money came from, by how long the position was held:**

| Held | Trades | Win rate | Median | Share of total P&L |
|---|---:|---:|---:|---:|
| 1–7 days | 301 | 13% | −2.36% | **−66%** |
| 8–14 days | 132 | 14% | −2.81% | −19% |
| 15–30 days | 212 | 40% | −1.10% | +43% |
| 31–60 days | 203 | 65% | +3.13% | **+90%** |
| 60+ days | 67 | 96% | +8.15% | **+52%** |

Positions that end within a week are where the losses are, and the reason is visible in why they
close: the median stop-out lasts 8 days and loses 3.67%, while the median target hit takes 36 days
and gains 10.26%.

**The holding period is an outcome, not a setting.** Nobody chooses to take only the 40-day trades —
you choose to enter, and the market decides whether you are stopped out in five days or left to run
for forty. The table describes what happened; it is not a knob.

**The number that governs everything else:** the best 5% of trades produced **102% of net P&L**.
Without those ~45 trades out of 915, the sample loses money (−2,971 against +136,400). With a 36.7%
win rate, two of every three positions lose, and what carried the sample was a handful of winners
allowed to run.

So this suits a horizon of **weeks to months, across many simultaneous positions**. It does not suit
buying one instrument and selling it a week later: that is the bucket where this design loses, and a
handful of trades a year cannot reach the average that made the sample work.

All of the above is TRAIN (2016–2021), the partition where parameters are fitted. It is the most
favourable view this system can produce and **does not establish future profitability**.

---

## Why there are no Santiago tickers

This project used to track 18 Bolsa de Santiago instruments — `SQM-B.SN`, `FALABELLA.SN` and so
on. All 18 resolved correctly on Yahoo and all 18 were removed. Two reasons, in order of
importance:

**They could not be bought.** The broker available here lists US instruments only, so every
scan, backtest and signal on those names produced analysis nobody could act on.

**Their data was the worst in the project.** Measured 2026-09-27: all 18 ended in flat
zero-volume bars the vendor had carried forward, several reported no volume on 85% of recent
sessions, and the strategy lost money on them (−19.9%, Sharpe −0.61).

What replaced them is Chilean *exposure* that can actually be bought: the NYSE ADRs `SQM`,
`BSAC`, `BCH`, `ENIC` and `CCU`, plus the country ETF `ECH`. All six have full history since
2016, zero carried-forward bars and zero zero-volume bars — clean exactly where the local
tickers were broken.

**The honest costs of the swap**, all three:

1. **USD denomination.** A return on `SQM` is the Chilean move *and* the CLP/USD move. Nothing
   here separates them.
2. **Five companies, not eighteen.** A much narrower slice of the Chilean market.
3. **They are thin.** This one was a surprise and it is the serious one. Measured over the three
   years to 2026-09-25, five of the six trade under 20M USD a day, and `CCU` (1.7M) and `ENIC`
   (1.8M) are the *least liquid instruments anywhere in this project* — below every Asian country
   ETF except Thailand. Only `SQM` (57M) is comfortable. "NYSE-listed" suggested liquidity and
   the measurement said otherwise.

The `.SN` provider machinery is still present and still tested, so someone with a Santiago broker
could put them back. A test fails if anyone does it by accident.

### Emerging Asia

21 instruments, screened on the same criteria: regional ETFs (`AAXJ`, `EEM`), country ETFs
(`MCHI`, `ASHR`, `INDA`, `EWY`, `EWT`, `EIDO`, `EWS`, `EWM`, `THD`, `VNM`) and large ADRs (`TSM`,
`BABA`, `PDD`, `JD`, `NTES`, `INFY`, `HDB`, `IBN`, `SE`). 30 of 31 candidates passed the data
checks; `GRAB` was rejected for having only 1,461 bars.

Country coverage was chosen over liquidity where the two conflicted. Dropping Thailand, Malaysia,
Indonesia and Vietnam would leave "emerging Asia" as China, India, Korea and Taiwan — most of the
market capitalisation, but a much narrower question. The cost is that five of the 21 are thin, and
each one says so.

Two overlaps worth knowing: `TSM` dominates `EWT` by weight, and `MCHI` and `ASHR` are both
"China" but hold different markets. Holding either pair is more correlated than diversified, and
the risk engine's sector cap will not catch it because the ETFs are classified "Broad Market".

Chinese ADRs also carry a risk the others do not: they are claims on offshore holding structures
rather than direct equity, and their listing status has been politically contingent more than
once. That is not modellable, so it is in no number here.

### Market versus region

Because every instrument now trades in New York, `market` stopped distinguishing anything useful
and a second field was added:

- **`market`** is *where it trades* — currency, session hours, holiday calendar, cost model.
- **`region`** is *what it is exposed to* — portfolio grouping, and **which benchmark it is
  measured against**.

That last part is why the distinction is load-bearing. A benchmark keyed on market would have
compared an emerging-Asia strategy against the S&P 500 and reported the difference as skill. Each
region has its own: SPY for the US, ECH for Chile, AAXJ for Asia — and all three are also in the
tradable universe, so a strategy can hold its own yardstick. That caveat travels with every
number derived from them.

The `CHILE` market definition survives with an empty universe. Its calendar, currency and cost
model are correct and verified, and deleting them would lose the work.

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
| `python -m app project SYMBOL` | What followed similar historical situations |
| `python -m app check SYMBOL --price P` | What paying P does to the signalled setup. Not advice |
| `python -m app buy SYMBOL --amount N` | **Record** a purchase by the cash spent. Orders nothing |
| `python -m app fix-entry SYMBOL --price P` | Replace an assumed entry with the real fill |
| `python -m app sell ID` | **Record** a sale you already made, and see the real P&L |
| `python -m app holdings` | Positions you have recorded, marked at the latest close |
| `python -m app watch` | Check every open position's exit rule and alert on what fired |
| `python -m app alerts` | Every notification attempted, and whether it was delivered |
| `python -m app digest --send` | Build the digest and send it to Telegram |
| `python -m app positions-export` | Write holdings to `data/positions.json` so they survive |
| `python -m app positions-import` | Restore holdings from that file. Idempotent |
| `python -m app prune-data` | Delete stored bars for instruments no longer in the universe |
| `python -m app serve` | Run the API |

Useful flags: `--market USA`, `--symbols AAPL,SQM,TSM`, `--timeframe 1D|1H|15m|5m`,
`--start`/`--end`, `--full`.

**Registered but refusing to run:** `paper` (simulated order routing — Phase 7). The manual
workflow in `buy`/`sell`/`watch` is what Phase 6 delivered instead, because it matches how you
actually trade.

Backtest flags: `--split full|train|validation|test`, `--strategy`, `--symbols`,
`--start`/`--end`, `--capital`, `--finalising`.

---

## Tracking real positions

The workflow is manual on both ends, because that is how you actually trade: you buy through your
broker's app, and this software watches what you bought.

```bash
# 1. The scanner says the entry conditions hold on SQM.
python -m app scan --action BUY

# 2. You buy it yourself, in your broker's app. This software does not and cannot.

# 3. Record it. Stop and target default to the strategy's own levels as of that date,
#    so the watch checks the rule the backtest actually measured.
python -m app buy SQM --qty 50 --price 47.30 --on 2026-09-15 --broker fintual

# 4. Every day (or let GitHub Actions do it):
python -m app watch

# 5. When the rule fires you get a Telegram message. If you decide to sell, record that:
python -m app sell 1 --price 51.20 --fees 1.20
```

**What the watch actually does.** It rebuilds the `Position` the backtester would be holding,
replays every bar since your purchase through the same `update_marks`, `advance_trailing_stop` and
`_resolve_price_exit`, and **stops at the first bar that triggers**. Reusing the backtester's own
code rather than reimplementing it is deliberate: an alert firing on slightly different logic than
the backtest measured is an alert with no evidence behind it.

It reports *when* the rule fired, not just whether it fires today. An early version checked only
the latest bar, and a position whose target was crossed months earlier was reported as triggering
now — a position a backtest would have closed a year before. The alert says "fired on 2026-07-13,
55 sessions ago" and states outright that a backtest considers it already exited.

**What it refuses to do.** If the newest stored bar is more than five days old, the watch will not
report "no exit signal". Silence and ignorance look identical on a dashboard, and that is the most
dangerous output this feature could produce. It says the data is stale and names the position as
unwatched.

**What an alert means.** That a rule this project backtested has triggered. Not that the price
will fall, not that selling is correct, not that the strategy is right. The message says this in
those words, because a notification is read in three seconds and whatever it implies is what gets
acted on.

**What the P&L means.** The figures on a closed position are the only ones in this project that
are not modelled — they come from the prices you report. They are also reported with a statement
of how little they establish: four real trades are a smaller sample than any backtest here, and
real money makes a result *feel* like proof in a way a backtest does not.

Alerts go through Telegram (free, a bot token from @BotFather) or print to the console. Delivery
is recorded separately from the trigger, because they fail independently — `python -m app alerts`
shows which messages actually arrived.

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
│   │   ├── projections/
│   │   │   ├── analogues.py    Similarity search + overlap correction
│   │   │   ├── scenarios.py    Percentile cases, never forecasts
│   │   │   └── runner.py       Wires it to stored data
│   │   ├── portfolio|execution|risk/     (Phase 6+)
│   │   ├── api/main.py         FastAPI endpoints
│   │   └── __main__.py         CLI
│   └── tests/                  760 tests
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

**Overlapping observations are not counted separately.** Take every bar as an observation
and measure the next 20 days from each: two observations one day apart share 19 of those
20 days. "127 historical observations" may contain six independent ones. Analogue matches
are therefore thinned so no two retained observations share any forward window or any
date, and every judgement uses the thinned count. On real data this is the difference
between claiming 921 observations and reporting 194.

**Every analogue distribution is compared against the base rate.** A conditional
distribution that reproduces the unconditional one carries the authority of a finding
without the content, and the two are indistinguishable on screen. So the base rate is
computed over the same bars and reported alongside, with an `adds_information` flag that
is false when they barely differ.

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

## The projection engine, and what calibrating it revealed

`python -m app project NVDA` answers "when conditions looked like this before, what
happened next?" — never "what will happen next". It finds past bars whose conditions
resembled the current setup and reports the distribution of what followed, as three
percentiles:

```
NVDA (USA) -- 20-bar outcomes after 194 similar historical situations
  61% of those situations were followed by a gain.

  BEAR  p10    -9.95%  ->  202.67 USD
  BASE  p50    +2.07%  ->  229.72 USD
  BULL  p90   +12.97%  ->  254.26 USD

  base rate (all bars): median +2.12%  p10 -7.78%  p90 +12.34%
```

Two things in that output exist because the first version was wrong.

**921 matches became 194 observations.** Consecutive daily bars share almost all of their
forward window, so counting them separately overstates the evidence by roughly the horizon
length. Matches are thinned until no two share a forward window or a date — instruments in
one market move together, so fifteen of them on one day are nearer to one observation than
to fifteen.

**The base rate is on the same line as the result.** The original default similarity
ceiling matched 54% of all candidate bars, and the resulting "conditional" distribution
reproduced the unconditional one to within a tenth of a point:

| | median | p10 | p90 |
|---|---|---|---|
| matched, ceiling 1.0 | +2.05% | −8.27% | +11.78% |
| **unconditional base rate** | **+2.12%** | **−7.78%** | **+12.34%** |
| matched, ceiling 0.3 | +2.07% | **−9.95%** | +12.97% |

At the loose ceiling the projection was telling us nothing beyond "these instruments rose
on average over this period", while looking exactly like a conditional finding. The default
moved to 0.3, which matches about 2% of candidates and produces a materially deeper
downside tail — and the base rate is now reported with every result, with an
`adds_information` flag that is false when the matching has isolated nothing.

A thin sample produces the phrase **"Insufficient historical evidence"** and no numbers at
all. At the default settings, 3 of 15 US instruments fall into that category. There is no
fallback to a looser search or a shorter horizon: relaxing the criteria until a number
appears is how insufficient evidence becomes a median, and that median would be
indistinguishable from a good one.

## Limitations you must know about

Run `python -m app limitations`, or open the Limitations page in the dashboard.
The most important ones:

### 1. Survivorship bias (high)

The universe lists companies that exist **today**. Firms that were delisted, went
bankrupt or were acquired are absent, so any backtest over this universe is measured
only on survivors and is **optimistic by an unknown amount**. Free data cannot fix
this. It is recorded on every backtest row and printed in every report.

### 2. Liquidity is not modelled at all (high)

Ten of the 42 instruments trade under 20M USD a day, including five of the six Chilean ones:
`CCU` 1.7M, `ENIC` 1.8M, `THD` 3.0M, `BCH` 6.4M, `EWM` 6.6M, `BSAC` 7.0M, `VNM` 8.0M, `EIDO`
9.3M, `ECH` 10.5M, `EWS` 11.9M (medians, three years to 2026-09-25).

The backtester models **no market impact at any position size**. A position large enough to matter
in `CCU` would move the price, and no figure in this project captures by how much. Every modelled
fill on those ten is optimistic by an unmeasured amount, and the backtester's risk-based position
sizing — calibrated on frictionless fills — will happily size a position that could not be filled.

Each affected instrument generates its own caveat from the measured figure (`AssetSpec.liquidity_caveat`),
which appears in exit alerts, the digest, the CLI and the API. A median is also a poor guide: it
says nothing about depth, spread, or how fast liquidity evaporates in a selloff.

### 3. The IPSA needs a free Banco Central account (medium)

Yahoo serves nothing for the Chilean index: six spellings were probed on 2026-09-27
(`^IPSA`, `IPSA.SN`, `^SPIPSA`, `^SPCLXIPSA`, `^CLX`, `IPSA`) and **all returned zero
rows**.

The **Banco Central de Chile API BDE** does carry it, free of charge, after a free
registration (email + password — no card, no tier). Set `BCCH_USER` and
`BCCH_PASSWORD` in `.env`; see [docs/chilean_data_sources.md](docs/chilean_data_sources.md).

The Chilean *region* is benchmarked against **`ECH`** instead. Since the Chilean instruments are
now USD-denominated ADRs, that comparison is at least currency-consistent — but both sides carry
the CLP/USD move and neither isolates the Chilean equity move from it. ECH is also tradable here,
so a strategy can hold its own benchmark. The code refuses to call ECH "IPSA", and a test enforces
that.

### 4. Yahoo's Chilean feed stalled at Fiestas Patrias (high — historical)

This is why the Santiago tickers are gone rather than a live problem. **Measured 2026-09-27: all 18
ended in flat zero-volume bars.** All 15 US instruments were clean.

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

Shorter history than the rest of the universe, so a backtest starting in 2016 runs these on a
smaller sample than everything beside them:

- **ENIC** — begins 2016-04-21, after the Enel Chile / Enel Americas split.
- **SE** — begins 2017-10-20 (NYSE listing).
- **PDD** — begins 2018-07-26.

Correlation the sector cap will not catch, because the ETFs are classified "Broad Market":

- **TSM and EWT** — TSM dominates EWT by weight. Holding both is one bet, not two.
- **MCHI and ASHR** — both labelled "China", holding different markets (Hong Kong/US listings
  versus mainland A-shares).

Structural risk that is in no number here:

- **Chinese ADRs** (BABA, PDD, JD, NTES) — claims on offshore holding structures rather than
  direct equity, with a listing status that has been politically contingent more than once.

### 8. Shallow intraday history (low)

60 days for 5/15-minute bars, 730 days for hourly. Not enough for a serious intraday
study. Daily is the priority everywhere in this project.

---

## Testing

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest                          # 898 tests (895 offline, 3 live)
python -m pytest -m 'not slow'            # skip the minutes-long integration tests
python -m pytest -m network               # 3 live provider tests
python -m pytest --cov=backend/app        # with coverage
python scripts/check_file_integrity.py    # source files zeroed by an interrupted write
```

`check_file_integrity.py` exists because it happened: two source files were once found to be
entirely NUL bytes, the signature of a write interrupted before the filesystem flushed. This
repository lives on a OneDrive-synced path, which makes that more likely rather than less. The
check is worth a script because every tool points away from the cause -- Python reports a
`SyntaxError` at line 1, `git diff` says "Binary files differ" and shows nothing, and `grep`
silently matches nothing. Recovery is `git checkout --` for anything committed, so committing
often is the real mitigation.

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
- **`test_projections.py`** — that twenty consecutive bars collapse to one observation,
  that a thin sample yields the phrase and no numbers, and that a distribution matching the
  base rate is flagged as uninformative.

---

## Roadmap

| Phase | Scope |
|---|---|
| **1** | **Data foundation, database, indicators — complete** |
| **2** | **Strategy engine, scanner, backtester, metrics, HTML reports — complete** |
| **3** | **Dashboard — complete** |
| **4** | **Optimisation, objective function, walk-forward, robustness — complete** |
| **5** | **Projection engine and historical analogues — complete** |
| **6** | **Real-position tracking, exit watch, Telegram alerts, scheduled runs — complete** |
| 7 | Simulated order routing (`paper`), portfolio-level allocation across regions |
| 8 | Production hardening |

Phase 6 was delivered as **manual position tracking rather than broker-connected paper trading**,
which is a deliberate change from the original plan. The reason is that the original plan solved a
problem that did not exist: orders are placed by hand through a broker's app, so a simulated order
router would have produced a second set of fictional positions alongside the real ones. What was
needed was a record of the real ones and an alert when the exit rule fires — which is what was
built.

Live trading is **not** on this roadmap as an enabled feature. A `BrokerAdapter` interface exists
in `app/execution` with nothing behind it, so one can be added deliberately later.

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
