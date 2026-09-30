# Free Chilean data sources: what exists, what does not

> ## Update, 2026-09-30: the Santiago tickers were removed
>
> Everything below still holds as research. What changed is what was done about it.
>
> The 18 Bolsa de Santiago instruments are **no longer in the universe**, for a reason this
> document did not consider: **they could not be bought.** The broker available here lists US
> instruments only, so every finding below was about data feeding analysis nobody could act on.
> The data quality problems documented here were the second reason, not the first.
>
> Chilean exposure is now taken through US-listed instruments that *can* be bought: the NYSE ADRs
> `SQM`, `BSAC`, `BCH`, `ENIC` and `CCU`, plus the country ETF `ECH`. All six have full history
> since 2016, zero carried-forward bars and zero zero-volume bars -- clean exactly where the local
> tickers were broken.
>
> **What this did not fix.** Measured over the three years to 2026-09-25, five of the six trade
> under 20M USD a day. `CCU` (1.7M) and `ENIC` (1.8M) are the least liquid instruments anywhere in
> this project. "NYSE-listed" suggested liquidity and the measurement said otherwise. The `.SN`
> tickers were illiquid too, so this is not a regression -- but it is not the improvement the swap
> appeared to be either.
>
> **The Banco Central finding is unaffected**, and still the only free route to the IPSA. The
> `.SN` provider machinery is also still present and still tested, so someone with a Santiago
> broker could put those instruments back.

**Research date: 2026-09-27.** Endpoints were probed directly rather than taken from
documentation, because several of these services describe capabilities their public
interfaces do not actually expose.

Two gaps prompted this search:

1. **No IPSA.** Yahoo serves nothing for the Chilean index under any spelling.
2. **A carried-forward tail on every Chilean ticker**, which makes the most recent
   data fabricated.

---

## Verdict

| Source | Free? | Usable? | What it gives |
|---|---|---|---|
| **Banco Central de Chile — API BDE** | Yes, free registration | **Yes** | **The IPSA**, FX, rates. No equities. |
| Bolsa de Santiago — public site | Yes | **No** | Blocked by Imperva bot protection |
| Bolsa de Santiago — "API Brain Data" | No | **No** | Commercial product with a trial |
| Stooq | Yes | **No** | Proof-of-work challenge on every request |
| CMF Chile | Yes, free key | **No** | UF, exchange rates, interest rates — no equities |

**Conclusion: there is no free source for per-stock Chilean daily prices other than
Yahoo.** The Banco Central closes the IPSA gap. The carried-forward tail has to be
handled by detection and trimming, which is what the code now does.

---

## Banco Central de Chile — API BDE ✅

Endpoint: `https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx`
Implementation: `backend/app/data/bcentral_provider.py`

**Cost:** free. Registration (email + password) is required and is also free — no
card, no tier. Register at
<https://si3.bcentral.cl/estadisticas/Principal1/Web_Services/>, then activate the
credentials for API access; registering alone is not sufficient.

**Verified live, without credentials:**

```
GET .../SieteRestWS.ashx?user=x&pass=x&function=GetSeries&timeseries=F013.IPSA.X
→ HTTP 200
  {"Codigo": -5, "Descripcion": "Invalid username or password",
   "Series": {"descripEsp": null, ..., "Obs": null}, "SeriesInfos": []}
```

So the transport, the response schema and the error contract are confirmed. A
`network`-marked test (`TestLiveErrorContract`) re-checks this, so if the service is
retired or moved behind a captcha the build says so.

**Not verified: the IPSA series identifier.** The API authenticates *before*
validating the series argument, so no probing without credentials can confirm a code.
The provider therefore **discovers** the identifier through `SearchSeries` and matches
it against the catalogue description, rather than shipping a guess as though it were
a fact. Same principle as the Chilean equity ticker resolution.

**Limits and terms.** 5 series per second per account, regardless of source IP. The
Bank's terms permit consulting, reproducing, disseminating, publishing and adapting
BDE content provided the Bank is credited as the owner of the information; the
provider carries that attribution string for reports.

**What it does not give.** No individual equity prices. The BDE is a macro-financial
database: indices, exchange rates, interest rates, monetary aggregates.

**Index bars are flat by nature.** The BDE returns a level per date, so synthesised
bars have `open = high = low = close` and `volume = 0`. That is the truth for an
index, but it collides with the carried-forward detector, which treats exactly that
shape as fabricated. Index assets are therefore exempt from the check — keyed on
`asset_class == "index"`, not on the data looking flat, so a *traded* instrument with
flat volumeless bars is still caught.

---

## Bolsa de Santiago — public site ❌

```
GET https://www.bolsadesantiago.com/                                  → 200 (SPA shell)
GET https://www.bolsadesantiago.com/api/RV_ResumenMercado/precios_indice → 302
    <html><head><title>302 Found</title><script>var __uzdbm_1 = ...
```

The `__uzdbm_*` cookie is Imperva bot detection. The JSON endpoints behind the SPA
are protected, so reaching them means defeating that protection — against the site's
terms and fragile besides. Ruled out.

## Bolsa de Santiago — "API Brain Data" ❌

A commercial product at `api-braindata.bolsadesantiago.com`, with a *free trial* tier
rather than a permanent free tier. A trial cannot be a dependency of a project whose
first rule is zero cost. Ruled out unless you decide to buy it, in which case it
would slot in behind the existing `DataProvider` interface.

## Stooq ❌

```
GET https://stooq.com/q/d/l/?s=sqm-b.sn&i=d   → 200, but:
  "This site requires JavaScript to verify your browser."
  + a SHA-256 proof-of-work challenge posted to /__verify
```

The same challenge is returned for `aapl.us`, so this is not about Chilean coverage —
the CSV interface is gated for every symbol. Ruled out.

## CMF Chile ❌

`api.cmfchile.cl` requires a free API key and publishes the UF, UTM, exchange rates
and interest rates. It is a financial-regulator indicator service, not a market-data
feed, and carries no equity prices. Useful later if the project ever needs the UF for
inflation-linked accounting; useless for this gap.

---

## The carried-forward tail, explained precisely

Measured on the downloaded data, 2026-09-27:

| Instrument | Last real print | Fabricated bars after it |
|---|---|---|
| SQM-B | 2026-07-17 | 49 |
| ANDINA-B | 2026-07-15 | 49 |
| The other 16 Chilean names | **2026-09-17** | **5** |
| All 15 US names | current | 0 |

Sixteen instruments stopping on the *same* date is not sixteen coincidences. The
calendar explains it exactly:

- **Thu 2026-09-17** — last real session.
- **Fri 2026-09-18** — Fiestas Patrias. Market closed.
- **Sat 19 / Sun 20** — weekend.
- **Mon 21 → Fri 25** — five sessions that did trade, for which Yahoo returned the
  17th's close with zero volume.

So Yahoo's `.SN` feed stalled at the holiday and did not resume. The five fabricated
bars are every session since. SQM-B and ANDINA-B stalled separately in mid-July.

### Consequence

With a 5-day staleness tolerance and a last real print on 2026-09-17, **all 18
Chilean instruments are currently blocked for live signal generation** — correctly.
The data is ten days old; the system says so instead of trading a fabricated price.

Historical backtesting over 2016–2026 is unaffected: the fabricated bars are trimmed
and the ~2,500 real bars per instrument remain.

### What to do about it

Options, in order of how much they cost you:

1. **Accept the lag.** Chilean signals are generated from the last real print and are
   refused once it ages past tolerance. Free, honest, already implemented.
2. **Raise `STALE_DATA_MAX_AGE_DAYS`.** Makes the refusal go away without making the
   data newer. Only defensible for a strategy that holds for months.
3. **Re-download periodically and hope the feed resumes.** Costs nothing; the
   incremental download will pick up real bars as soon as Yahoo serves them.
4. **Buy a feed.** The Bolsa's own commercial API would slot in behind
   `DataProvider`. Outside this project's rules unless you decide otherwise.

Nothing here can be fixed by writing better code against Yahoo. The data is not
there.
