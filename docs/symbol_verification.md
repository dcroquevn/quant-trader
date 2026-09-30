# Symbol verification record

> ## Update, 2026-09-30: the Chilean section is now a historical record
>
> All 18 Santiago mappings below resolved correctly and the record is kept because it is evidence.
> They are **no longer in the universe**: resolving was never the binding problem -- none of them
> could be purchased through the broker available here.
>
> The instruments now tracked are listed further down, all US listings resolving to their plain
> ticker: 6 for Chilean exposure and 21 for emerging Asia. See
> [chilean_data_sources.md](chilean_data_sources.md) for the full reasoning.

**Verification date: 2026-09-27** · Provider: `yfinance` 1.7.0 (free, no API key)

This file records what was **empirically probed**, not what was assumed. A free
provider returns an empty frame for an unknown ticker rather than an error, so the
only way to know a mapping works is to ask for data and see what comes back.

Re-run with `python -m app verify-symbols --force` when this goes stale. Free vendors
rename and delist without notice.

---

## Chilean equities — 18/18 resolved

Yahoo suffixes Bolsa de Comercio de Santiago listings with `.SN` applied to the local
*nemotécnico*. The primary candidate resolved in **every** case; every alternate
spelling tried failed.

| Canonical | Resolved ticker | Bars (2016→2026) | History begins |
|---|---|---|---|
| CHILE | `CHILE.SN` | 2,598 | 2016-01-04 |
| BSANTANDER | `BSANTANDER.SN` | 2,555 | 2016-01-05 |
| BCI | `BCI.SN` | 2,580 | 2016-01-04 |
| ITAUCL | `ITAUCL.SN` | 2,515 | 2016-01-04 |
| SQM-B | `SQM-B.SN` | 2,611 | 2016-01-04 |
| CENCOSUD | `CENCOSUD.SN` | 2,562 | 2016-01-04 |
| FALABELLA | `FALABELLA.SN` | 2,558 | 2016-01-04 |
| LTM | `LTM.SN` | 2,594 | 2016-01-04 |
| ENELCHILE | `ENELCHILE.SN` | 2,457 | **2016-04-22** |
| COLBUN | `COLBUN.SN` | 2,506 | 2016-01-04 |
| CAP | `CAP.SN` | 2,551 | 2016-01-05 |
| COPEC | `COPEC.SN` | 2,546 | 2016-01-05 |
| CMPC | `CMPC.SN` | 2,564 | 2016-01-04 |
| ENTEL | `ENTEL.SN` | 2,478 | 2016-01-04 |
| ANDINA-B | `ANDINA-B.SN` | 2,528 | 2016-01-05 |
| CCU | `CCU.SN` | 2,513 | 2016-01-05 |
| PARAUCO | `PARAUCO.SN` | 2,504 | 2016-01-05 |
| MALLPLAZA | `MALLPLAZA.SN` | 1,919 | **2018-07-27** |

### Alternate spellings probed and rejected

All returned zero rows. Recorded so nobody re-litigates them:

`BCHILE.SN`, `SANTANDER.SN`, `ITAUCORP.SN`, `CORPBANCA.SN`, `SQMB.SN`, `SQM_B.SN`,
`LATAM.SN`, `LTMAUY.SN`, `ENEL.SN`, `ANDINAB.SN`, `ANDINA_B.SN`, `PLAZA.SN`

### Shorter histories are real, not download failures

- **ENELCHILE** begins 2016-04-22, following the 2016 reorganisation that separated
  Enel Chile from Enel Americas.
- **MALLPLAZA** begins 2018-07-27 — roughly two years *after* its 2016 IPO. Yahoo
  simply does not carry the earlier period. Any backtest starting in 2016 silently
  runs this name on a shorter sample than the rest of the universe.

---

## US equities — 15/15 resolved

Plain tickers, no suffix. All returned 2,698 bars from 2016-01-04 to 2026-09-25.

`SPY` `QQQ` `DIA` `IWM` `AAPL` `MSFT` `NVDA` `AMZN` `META` `GOOGL` `TSLA` `AMD` `JPM` `V` `AVGO`

---

## Benchmarks

| Market | Wanted | Available | Used |
|---|---|---|---|
| USA | S&P 500 | Yes | `SPY` — 2,698 bars, total-return via adjusted close |
| CHILE | IPSA | **No** | `ECH` as a flagged proxy — 2,698 bars |

### The IPSA is not obtainable free of charge

Six spellings probed, all returning **zero rows**:

| Candidate | Result |
|---|---|
| `^IPSA` | 0 rows — "possibly delisted; no timezone found" |
| `IPSA.SN` | 0 rows |
| `^SPIPSA` | 0 rows |
| `^SPCLXIPSA` | 0 rows |
| `^CLX` | 0 rows |
| `IPSA` | 0 rows |

The index is licensed by S&P Dow Jones and its history is a paid product. `IPSA`
remains declared in the universe with **no provider mapping**, so reports can state
the gap explicitly instead of quietly substituting something else. A test asserts
that the Chile benchmark is never labelled "IPSA".

### Why ECH is a poor substitute

`ECH` (iShares MSCI Chile ETF) resolved with 2,698 bars, but:

1. **USD-denominated** while the strategy trades in CLP — the comparison silently
   includes CLP/USD currency moves.
2. **NYSE sessions and US holidays**, not Santiago's — daily returns are not aligned
   one-for-one.
3. **MSCI-eligible large caps only**, not the full local market.
4. **Charges a management fee**, already deducted from its price.

A synthetic equal-weight buy-and-hold of the Chilean universe is planned as a second,
also-imperfect comparison in Phase 2 — it answers "did the strategy beat simply
holding everything?", which is informative even though it shares the universe's
survivorship bias.

---

## Data-quality finding: carried-forward quotes

Measured on the downloaded data, same date. Yahoo repeats the last traded price for
Chilean instruments that have not printed, producing flat bars with zero volume that
are **dated to the current week**:

| Instrument | Trailing flat zero-volume bars |
|---|---|
| SQM-B | **49** |
| ANDINA-B | **49** |
| All 16 other Chilean names | 5 |
| All 15 US names | 0 |

Forty-nine bars is roughly ten weeks of data that looks current and contains no
trading. This is why `DataEngine.assert_fresh()` checks for a carried-forward tail in
addition to the age of the newest bar, and why `ChileDataProvider` flags illiquid bars
rather than dropping them — a dropped bar would close the gap and make the series
look continuous.

See `backend/tests/test_stale_quotes.py`.

---

## Missing weekdays

Reported as **candidates**, never confirmed gaps: this project has no free, reliable
exchange-holiday calendar, so a missing bar cannot be distinguished from a closed
session.

| Market | Missing weekdays over 10.7 years | Per year | Interpretation |
|---|---|---|---|
| USA | 102 | ~9.5 | Consistent with NYSE holidays |
| CHILE | 189–322 | ~18–30 | More public holidays, plus genuine no-print days |

Longest consecutive run: 1 session (USA), 3–6 sessions (Chile).

---

## Chilean exposure via US listings — verified 2026-09-27

Probed the same way: ask for data and see what comes back. All resolve to their plain ticker; no
suffix, no special namespace. Turnover measured separately over the three years to 2026-09-25.

| Symbol | Instrument | Bars (2016→2026) | History begins | Median turnover USD/day |
|---|---|---|---:|---:|
| `SQM` | Sociedad Quimica y Minera (ADR) | 2,700 | 2016-01-04 | 57,487,256 |
| `BSAC` | Banco Santander Chile (ADR) | 2,700 | 2016-01-04 | 6,959,128 |
| `BCH` | Banco de Chile (ADR) | 2,700 | 2016-01-04 | 6,362,856 |
| `ENIC` | Enel Chile (ADR) | 2,625 | 2016-04-21 | 1,786,491 |
| `CCU` | Compania Cervecerias Unidas (ADR) | 2,700 | 2016-01-04 | 1,680,206 |
| `ECH` | iShares MSCI Chile ETF | 2,700 | 2016-01-04 | 10,510,235 |

Zero carried-forward bars and zero zero-volume bars on all six — the two failures that
disqualified the `.SN` tickers. `ENIC`'s history begins at the 2016 reorganisation that separated
Enel Chile from Enel Americas, not earlier.

**Five of the six are below the 20M USD/day threshold** at which this project flags liquidity as
binding on position size. That was not the expected result and it is recorded here because a later
reader will otherwise assume a NYSE listing implies liquidity.

`ILF` (iShares Latin America 40) was probed and clean but not adopted: it is regional rather than
Chilean, so including it would have muddied what "Chile exposure" means.

---

## Emerging Asia via US listings — verified 2026-09-27

31 candidates probed, 30 clean. `GRAB` was rejected for having only 1,461 bars. 21 were adopted;
the remainder were dropped as redundant (`VWO` ≈ `EEM`, `FXI` ≈ `MCHI`) or too thin to be worth
the coverage (`EPHE` at 3.8M USD/day, plus `BIDU`, `UMC`, `ASX`, `KB`, `SKM`).

| Symbol | Instrument | Bars | Median turnover USD/day |
|---|---|---:|---:|
| `EEM` | iShares MSCI Emerging Markets ETF | 2,700 | 1,240,329,835 |
| `AAXJ` | iShares MSCI All Country Asia ex Japan ETF | 2,700 | 38,797,939 |
| `MCHI` | iShares MSCI China ETF | 2,700 | 136,631,462 |
| `ASHR` | Xtrackers Harvest CSI 300 China A-Shares ETF | 2,700 | 139,259,476 |
| `INDA` | iShares MSCI India ETF | 2,700 | 257,044,919 |
| `EWY` | iShares MSCI South Korea ETF | 2,700 | 255,048,505 |
| `EWT` | iShares MSCI Taiwan ETF | 2,700 | 184,992,866 |
| `EIDO` | iShares MSCI Indonesia ETF | 2,700 | 9,337,889 |
| `EWS` | iShares MSCI Singapore ETF | 2,700 | 11,910,076 |
| `EWM` | iShares MSCI Malaysia ETF | 2,700 | 6,563,187 |
| `THD` | iShares MSCI Thailand ETF | 2,700 | 2,982,196 |
| `VNM` | VanEck Vietnam ETF | 2,700 | 8,004,576 |
| `TSM` | Taiwan Semiconductor (ADR) | 2,700 | 2,746,970,359 |
| `BABA` | Alibaba Group (ADR) | 2,700 | 1,366,756,873 |
| `PDD` | PDD Holdings (ADR) | 2,055 | 819,614,369 |
| `JD` | JD.com (ADR) | 2,700 | 312,934,505 |
| `NTES` | NetEase (ADR) | 2,700 | 110,012,176 |
| `INFY` | Infosys (ADR) | 2,700 | 184,494,450 |
| `HDB` | HDFC Bank (ADR) | 2,700 | 153,504,009 |
| `IBN` | ICICI Bank (ADR) | 2,700 | 138,536,484 |
| `SE` | Sea Limited (ADR) | 2,246 | 405,497,448 |

`PDD` begins 2018-07-26 and `SE` 2017-10-20, so both run on a shorter sample than the rest.

**Country coverage was chosen over liquidity** where the two conflicted. Dropping Thailand,
Malaysia, Indonesia and Vietnam would leave emerging Asia as China, India, Korea and Taiwan — most
of the market capitalisation, but a much narrower question. The cost is that five of the 21 are
thin, and each one says so.

**Two overlaps** that the risk engine's sector cap will not catch, because the ETFs are classified
"Broad Market": `TSM` dominates `EWT` by weight, and `MCHI` and `ASHR` are both labelled "China"
while holding different markets (Hong Kong/US listings versus mainland A-shares). Holding either
pair is more correlated than diversified.
