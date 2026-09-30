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
- ✅ Readable on a phone (GitHub app → Actions → the latest run).
- ❌ No Pages site.
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
| `daily-watch.yml` | 21:30 UTC, Mon–Fri | Downloads bars, checks your positions, sends Telegram alerts, writes a job summary | Telegram secrets; a private repo |
| `publish-digest.yml` | 22:00 UTC, Mon–Fri | Downloads bars, builds the static digest, deploys to Pages | Pages enabled; a public repo on the free plan |

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
