Written for: the person running this project on their own GitHub account.

# Running the daily check without your computer on

The dashboard needs a FastAPI process, which needs your machine awake. The alerts do not. This
document is about moving the parts that matter onto GitHub's machines, and about the one real
trade-off involved: **GitHub's free tier will let you have private alerts or a public dashboard,
probably not both.**

## Things I could not verify

I could not check GitHub's current pricing and quotas while writing this, so three claims below
are from memory and need confirming on your own account before you rely on them:

1. **GitHub Pages on a free plan requires a public repository.** Publishing Pages from a private
   repository is, as far as I know, a paid feature. Check **Settings → Pages** in your repository;
   if it refuses to let you enable Pages, this is why.
2. **Actions minutes are free on public repositories and metered on private ones**, with a monthly
   allowance on the Free plan. Check **Settings → Billing and plans → Plans and usage**.
3. **Cache entries are evicted after about a week without a read**, with a per-repository size cap.

The first one is what decides your setup, so check it first. Everything else here works either
way.

## Two options, and which I would pick

### Option A — private repository (recommended)

Alerts reach your phone through Telegram. The daily run also writes a job summary, which the
GitHub mobile app renders as a formatted page — so you get a readable dashboard on your phone
without publishing anything.

- ✅ Positions, sizes and entry prices stay private.
- ✅ Telegram alerts work.
- ✅ Readable on a phone (GitHub app → Actions → the latest run), covering both your positions
  and the market overview.
- ✅ The full digest downloadable as an artifact from each run.
- ❌ No Pages site, so no permanent URL you can bookmark or share.
- ⚠️ Actions minutes are metered. A run takes two to four minutes, so a weekday schedule is
  roughly 60–100 minutes a month.

Use `daily-watch.yml`. Skip `publish-digest.yml`.

### Option B — public repository

You get a real web page at `https://<your-username>.github.io/<repo>/`, updated every weekday, and
free unlimited Actions minutes. The page contains market analysis only.

- ✅ A proper dashboard, openable by anyone with the link.
- ✅ Free Actions minutes.
- ❌ Anything in the repository is world-readable, including a job summary. So the watch's output
  must not run here, or must run with alerts going only to Telegram and the summary disabled.
- ❌ Your positions cannot live in this repository at all.

Use `publish-digest.yml`. If you want both, put the watch in a second, private repository.

**My recommendation: Option A.** The digest is pleasant to look at, but the alerts are the point,
and the GitHub app's job summary covers "read it on my phone" without making anything public.

## Setting it up

### 1. Push the repository

```bash
cd quant-trader
git remote add origin https://github.com/<you>/quant-trader.git
git push -u origin main
```

Choose **private** when you create it, unless you decided on Option B.

`.gitignore` already excludes `.env`, so your Telegram token does not go up with it. It does *not*
exclude `data/quant_trader.db`. On a private repository, committing that file is a reasonable way
to make your positions survive a cache eviction; on a public one it must not be committed.

### 2. Get a Telegram bot (free, five minutes)

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, pick a name, and it replies with a token.
3. Send your new bot any message — it cannot message you until you do.
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find
   `result[0].message.chat.id`.

Test it locally first:

```bash
# in .env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=true
```

```bash
python -m app watch --channel telegram
```

If nothing arrives, `python -m app alerts` shows the reason. A failed delivery is recorded, not
swallowed.

### 3. Add the secrets to GitHub

**Settings → Secrets and variables → Actions → New repository secret:**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat id |

Secrets are encrypted and masked in logs. Never put them in a workflow file.

### 4. Seed the database

The first scheduled run starts from an empty cache and downloads everything, which takes a few
minutes. That is fine. What it *cannot* rebuild is your positions — no provider knows what you
bought.

So after the first run succeeds, record your positions:

```bash
python -m app buy SQM --qty 50 --price 47.30 --on 2026-09-15 --broker fintual
```

Do this against your **local** database, then either commit `data/quant_trader.db` (private repo
only) or re-enter them if the cache is ever evicted. See "The cache is not storage" below.

### 5. Enable the schedule

Actions are disabled by default on a fresh clone. **Actions → I understand my workflows, enable
them.** Then trigger a manual run: **Actions → Daily watch → Run workflow**. Use the `dry_run`
input the first time so nothing is sent while you check the output.

For Option B, also set **Settings → Pages → Source → GitHub Actions**.

## Recording a trade from your phone

`python -m app buy` needs a terminal, and the moment you most want to record a trade is the moment
you just made one — in a broker's app, nowhere near a computer. An unrecorded position is an
unwatched position, so that friction is not cosmetic.

**GitHub mobile app → your repo → Actions → Record a trade → Run workflow.** It is a form:

| Field | Buy | Sell |
|---|---|---|
| action | `check`, `buy` or `sell` | |
| symbol | the ticker, e.g. `SQM` | the **holding id**, e.g. `3` |
| quantity | shares bought | ignored |
| price | what you paid per share | what you received |
| on_date | blank for today | blank for today |
| fees | commission and taxes | commission and taxes |
| note | why | why |

**`check` records nothing.** It answers "I am about to pay this — what does it do to the setup?",
which is the question that arrives at the same moment and in the same place as the trade. It shows
the risk, the reward and the ratio at your price against the signal's, plus how 915 backtest trades
actually ended. It does not say whether to buy, because nothing in this project establishes that
the strategy is profitable at any entry price.

A `buy` or `sell` prints your holdings in the summary and commits the updated positions file. It
places no order and cannot: there is no broker connection anywhere in this project.

The holding id for a sell comes from the daily watch summary, from the Telegram alert (every exit
alert ends with the exact `app sell <id>` command), or from `python -m app holdings`.

## Where positions actually live

In **`data/positions.json`**, committed to the repository.

Not in the Actions cache. The cache is evicted after about a week of disuse, and while ten years
of price history re-downloads in a few minutes, *nothing* can rebuild what you bought — no
provider knows. So the holdings are also written to a few hundred bytes of JSON that lives in git:

* `buy` and `sell` rewrite it automatically, locally and in the workflow.
* Every scheduled run imports it before checking anything, so an eviction costs nothing.
* The import is idempotent — matched on symbol and entry date — because it runs daily and
  duplicating a position would corrupt every P&L figure derived from it, silently.
* It is JSON rather than the SQLite file so it diffs in a pull request and a human can read it.

`python -m app positions-export` and `positions-import` do it by hand if you need to.

## If the Actions tab shows no workflows

This happened on the first push of this repository, so it is worth writing down. The symptom:

* **Actions** tab loads, says "0 workflow runs", and the left sidebar lists no workflow names.
* `/actions/workflows/daily-watch.yml` returns **"This workflow does not exist"**.
* **Settings → Actions → General** already shows "Allow all actions and reusable workflows".

Everything verifiable was fine: both files present on the default branch, valid against GitHub's
own published schema, no BOM, LF endings, correct `name` and triggers. GitHub simply had not
indexed them.

GitHub registers a workflow from the push event that carries the file. The remedy is a fresh push
that touches it:

```bash
git commit --allow-empty -m "reindex" && git push    # if nothing needs changing
```

Both workflows now carry a `push` trigger scoped to their own path, so editing one re-registers it
and runs it immediately. That doubles as a smoke test — a job that fires once a day unattended is
one whose mistakes stay hidden until the night they matter.

## Why publish-digest is manual only

Its deploy step needs GitHub Pages, and Pages on the free plan serves from public repositories. On
a private repository that step cannot succeed, so a daily schedule would produce a red X every
weekday for a reason that is not a fault — and a job that always fails teaches you to ignore
failures, which is expensive the day a real one appears.

The schedule is commented out in the file rather than deleted. Uncomment it when Pages actually
works for the repository: either it is public, or the plan serves Pages from private ones.

Until then `daily-watch` is the daily job, and it needs no Pages: the job summary renders in the
GitHub mobile app and Telegram carries the alerts.

## What you actually open

There is **no file in the repository to open**, and this is the part most likely to send you
looking for something that does not exist. Two reasons:

* GitHub shows an HTML file in a repository as *source code*, not as a rendered page.
* `reports/` is gitignored, so the digest is not in the repository at all.

What exists instead, in descending order of how often you will use it:

### 1. Telegram — the thing that matters

Two messages a day, and nothing to open:

* **The digest summary** — what fired, and where each region sits. Readable in the notification
  itself.
* **The digest page attached** — the charts and every instrument, one tap away when the summary
  raises a question.

Plus an alert, separately, whenever an exit rule fires on a position you recorded. That is the
entire point of the daily run; everything else is context.

The summary goes first on purpose: a document notification shows only a filename, so sending just
the attachment would make you open a file every day to learn that nothing happened.

### 2. The Actions job summary — your phone dashboard

GitHub mobile app → your repo → **Actions** tab → the most recent **Daily watch** run → scroll to
the summary. It renders as a formatted page with two sections:

* **Watch** — your positions, unrealised P&L, which stop is binding, and whether any rule fired.
  When you hold nothing it says so explicitly, so you can tell "ran, nothing to check" from "never
  ran".
* **Market overview** — instrument counts and signals per region, and the benchmark each region is
  measured against. Only instruments where something fired are listed; a HOLD row is not news.

Most days the overview will say nothing fired. That is the normal case — the backtest made about 13
entries a month across all 42 instruments — and the page says so rather than looking broken.

### 3. The digest artifact — the full picture

Same run page, **Artifacts** section at the bottom, `digest-<number>`. Download and open
`digest.html` in any browser. It has every instrument, its liquidity, each region's benchmark and
all its caveats. Awkward on a phone, useful on a laptop.

### 4. The React dashboard — needs your computer running

The richest surface, and the only one that cannot be scheduled. Two terminals:

```bash
python -m app serve            # terminal 1
cd frontend && npm run dev     # terminal 2
```

Then `http://localhost:5173`. Interactive charts, the optimiser, walk-forward results, per-instrument
projections — none of which fits in a job summary.

### Locally, without GitHub at all

```bash
python -m app digest
```

Writes `reports/digest.html`. Double-click it. No server, no build step, works offline.

## The cache is not storage

This is the part most likely to bite you.

The scheduled run keeps the SQLite database in the Actions cache between runs. Caches are evicted
after about a week without a read, and there is a size cap per repository. When the cache goes:

- **Bars re-download in a few minutes.** No loss.
- **Your recorded positions are gone.** Nothing can rebuild them.

Three mitigations, in order of how much I would trust them:

1. **Keep your local database as the record.** `data/quant_trader.db` on your machine is the truth;
   the cache is a convenience. Re-run the `buy` commands after an eviction.
2. **Commit the database** to a private repository. Crude — a binary file in git history — but it
   survives everything. Add a step to the workflow that commits it back after each run if you want
   this automated.
3. **Download the artifact.** Every run uploads `quant_trader.db` with 14-day retention. Drop it
   into your local `data/` directory to recover.

A weekday schedule reads the cache five times a week, so eviction should not normally happen. It
still will eventually.

## What runs when

| Workflow | Schedule | What it does | Needs |
|---|---|---|---|
| `daily-watch.yml` | 21:30 UTC, Mon–Fri | Downloads bars, checks your positions, sends Telegram alerts, writes a job summary with both your positions and a market overview, attaches the digest as an artifact | Telegram secrets; a private repo |
| `publish-digest.yml` | **manual only** | Downloads bars, builds the static digest, deploys to Pages | Pages enabled; a public repo on the free plan |
| `record-trade.yml` | **manual only** | Records a buy or a sell from a form, commits `data/positions.json` | nothing; this is the phone entry point |

21:30 UTC is after the US close with enough margin for the provider to settle the final bar.
Actions cron is not punctual — a job can start 10–30 minutes late under load, and occasionally not
at all. Treat a missing day as normal rather than as a fault.

## The privacy boundary, concretely

`python -m app digest` excludes positions unless you pass `--include-holdings`. The publish
workflow does not pass it, and it greps the output for the warning string that a personal digest
carries — failing the build rather than publishing it. That guard is tested
(`tests/test_digest.py::TestPrivacy`), so if someone changes the wording the test fails before the
workflow silently stops protecting anything.

If you want a private page with your positions on it, generate it locally and keep it local:

```bash
python -m app digest --include-holdings --output reports/mine.html
```

## Running it locally instead

If none of this appeals, the whole thing works from a scheduled task on your own machine. On
Windows:

```powershell
schtasks /create /tn "quant-trader watch" /tr `
  "C:\path\to\quant-trader\.venv\Scripts\python.exe -m app watch" `
  /sc daily /st 18:30
```

The computer has to be on at that time, which is the problem this document exists to solve — but
it needs no GitHub account, no secrets, and nothing leaves your machine except the Telegram
message.

## What this does not do

No part of this places an order. `PAPER_TRADING=true` and `LIVE_TRADING=false` are set explicitly
in both workflows rather than left to the defaults, because these run unattended and that is the
setting that must never drift. There is no broker integration to enable — `app/execution` holds an
interface and nothing behind it.
