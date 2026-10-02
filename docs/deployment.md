Written for: the person running this project on their own GitHub account.

# The app

`https://dcroquevn.github.io/quant-trader/` — one page, updated every weekday after the US
close. Open it on a phone and use **Add to Home Screen**; it then behaves like an app, because
it is a single page with no server behind it.

## Where your positions live

**In your browser, on your device. Nowhere else.**

The repository is public, which is what makes Pages work on the free plan — and it means
anything the site carries is world-readable. So the site carries prices, and you type your
positions into it. The page computes HOLD or SELL locally, from public market data and private
holdings, and only your device ever sees the two together.

Three consequences worth knowing before you rely on it:

* **Clearing site data loses them.** So does a private window, and so does a different phone.
  Use the **Export** button and keep the file.
* **No sync.** Your laptop and your phone each have their own copy.
* **Nothing can read them back.** Not this project, not GitHub, not me. If you want the CLI to
  know about a position too, record it there separately with `python -m app buy`.

This is enforced, not promised. `scripts/check_payload_is_public.py` runs in the publish job and
fails the build if `market.json` grows any field outside an allowlist, and
`tests/test_webapp.py` asserts the payload is byte-identical whether or not a position exists.
Two checks of one rule, because it is the rule that must not fail quietly.

## Using it

**Recording a purchase.** Pick the instrument, enter the cash you spent and **the price Fintual
showed you**. Not the price on the page — that is the previous close, and Fintual fills during
the session. On 2026-10-01 the gap was 2.1% on GOOGL, which took the setup from 3.0x
reward-to-risk to 1.8x. The share count is worked out from the two.

The stop and target are fixed when you record, from the bar the strategy measured, and stored
with the position. They do not move afterwards: a stop that drifts as new bars arrive is not a
stop.

**Reading the verdict.** A green **HOLD** means none of the four exit rules has been met. A red
**SELL** means one has, and the line beside it says which and when. Neither is advice.

## The one workflow

| | What it does | When |
|---|---|---|
| **Publish the app to Pages** | Downloads bars, builds the page and its data, checks the data carries nothing personal, deploys | 22:00 UTC weekdays, and on any push that touches the app |

That is all there is now. The Telegram jobs, the trade-recording form and the Telegram test were
removed when the app replaced them: the first two read and wrote a positions file that a public
repository cannot hold, and the third tested a channel no longer in use.

## Running it locally

```bash
python -m app app --output site
```

Then open `site/index.html`. No server, no network. Same page, same storage rules — a different
browser profile, so a different set of positions.

## If the Actions tab shows no workflows

This happened once on this repository and the symptom points nowhere near the cause:

* **Actions** loads, says "0 workflow runs", and the sidebar lists nothing.
* `/actions/workflows/<file>.yml` returns "This workflow does not exist".
* **Settings → Actions → General** already allows all actions.

GitHub registers a workflow from the push event that carries the file, and sometimes does not.
A fresh push touching it is the remedy:

```bash
git commit --allow-empty -m "reindex" && git push
```

## What this does not do

No part of it places an order, and live trading is not implemented. `PAPER_TRADING=true` and
`LIVE_TRADING=false` are set explicitly in the workflow rather than left to defaults, because it
runs unattended. There is no broker integration to enable — `app/execution` holds an interface
and nothing behind it.
