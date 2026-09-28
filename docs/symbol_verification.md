# Symbol verification record

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
