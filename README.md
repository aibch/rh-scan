# RobinhoodTracker

Minimal working version of the [Robinhood Chain token strategy plan](docs/strategy-plan.md) — covers **Stage 0 (setup)**, **Stage 1 (scanner)**, and the reporting groundwork for **Stage 2 (analyze)**.

## What you need

**Nothing.** Python 3.8+ standard library only — no API keys, no wallet, no RPC endpoint. Market data comes from the free [GeckoTerminal API](https://api.geckoterminal.com), which already indexes Robinhood Chain (network id `robinhood`, Uniswap v3).

## Quick start

```bash
# one scan cycle (writes data/scanner.db)
python3 scanner.py --once

# run forever, scanning every 5 minutes (Stage 1)
python3 scanner.py --loop 300

# keep it running unattended
nohup python3 scanner.py --loop 300 >> data/scanner.log 2>&1 &

# the Stage 2 report (rug rate, survivor profile, current top pools)
python3 report.py               # judges pools 7+ days old
python3 report.py --age-days 1  # useful in the first week
```

## What the scanner captures (per pool, every cycle)

- price (USD), liquidity (USD), FDV, market cap
- 24h volume, volume ÷ liquidity ratio
- 24h buys / sells / unique buyers / sellers
- 24h price change
- pool creation time and when *we* first saw it (token age)

It sweeps both the **newest pools** and the **top pools by volume**, so new launches and established pairs are all logged into SQLite (`data/scanner.db`). Everything is time-series: each cycle appends a snapshot, which is what makes the Stage 2 rug-rate analysis possible.

## On-chain safety checks (Phase 2 — implemented)

`onchain.py` enriches tokens via the Robinhood Chain Blockscout explorer
(no key needed) and Alchemy RPC:

| Metric | Status |
|---|---|
| Top-10 holder concentration | ✅ scored (20% weight, with verification) |
| Contract verified | ✅ scored |
| Transfer-block check (`eth_call` as a top holder — NOT a full sell simulation; sell taxes/AMM-recipient blocks pass it) | ✅ hard eligibility gate on confirmed blocks |
| Deployer wallet recorded (rug history accumulates in our own data) | ✅ stored |
| LP locked / burned check | ⬜ future (Uniswap v3 position analysis) |

The transfer simulation needs an `ALCHEMY_API_KEY`. For GitHub Actions, add it
under **repo Settings → Secrets and variables → Actions → New repository
secret** named `ALCHEMY_API_KEY`. Locally: `ALCHEMY_API_KEY=... python3
onchain.py`. Without it, that one check is skipped gracefully.

The hourly workflow keeps a hard 40-token request budget but spends it by
freshness priority. Any due current top-10 candidates take first claim so they
cannot be starved; remaining capacity goes to failed and new checks, other
recently active candidates daily, and the liquid-token long tail every three
days. This keeps the data affecting picks fresh without attempting an
API-heavy full-universe refresh. Every successful check still appends to
`data/onchain_history.jsonl`, preserving look-ahead-safe historical validation.

Blockscout 503-blocks anonymous requests from GitHub Actions runner IPs. To
let the explorer checks run in CI, create a free account at
[robinhoodchain.blockscout.com](https://robinhoodchain.blockscout.com), generate
an API key (Account → API keys), and add it as a repository secret named
`BLOCKSCOUT_API_KEY`. Until then, the workflow probes the explorer and skips
the checks cheaply when blocked; holder/verification data still refreshes
whenever the checks run from an unblocked network.

## Volume surges & real-time spike alerts (segregated from the pipeline)

- The dashboard's **Volume surges (24h)** panel (via `surges.py`) shows bursts
  of ≥ $25k traded within one scan gap at ≥ 2× pool liquidity — read-only over
  existing snapshots, granularity limited by scan cadence.
- `spike_watch.py` is a standalone minute-cadence watcher for an always-on
  machine: it polls the newest/hottest pools, pushes alerts via ntfy.sh
  (`NTFY_TOPIC`) or Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`), and
  logs every event to `data/spike_events.jsonl` for the ongoing study.

Both are watchlist tools. The spike study on this dataset: entries at spike
detection ran a **median −37% at +6h and 61% rugged by +24h** — most surges
here are pump-and-dump ignitions, not news.

## Validating the score (`validate.py`)

Scores are logged at every scan (`data/picks/`, stamped with SCORE_VERSION) and
are recomputable for every historical snapshot. `python3 validate.py` measures
whether high scores actually predicted better outcomes: median forward return,
rug rate, and untracked share per score band at 1/3/7-day horizons, the
score→return rank correlation, and per-component post-mortems of the biggest
score collapses. If the correlation isn't consistently positive, re-weight the
model, bump `SCORE_VERSION`, and re-validate on a fresh period.

## Backtesting Stage 2 filter rules (`filter_backtest.py`)

`python3 filter_backtest.py` grid-searches liquidity floors, minimum pool age,
score thresholds, verification, top-10 concentration, and confirmed-transfer
gates. Each asset is entered once per rule at its first qualifying snapshot.
Historical on-chain checks are joined as of that snapshot, and outcomes reuse
the validator's fixed horizon windows and absorbing-rug rule. The console shows
the strongest distinct cohorts; `--csv data/filter_backtest.csv` writes every
rule/horizon result, including return percentiles and strict rug-rate bounds for
censored pools.

The grid is exploratory and tests many rules. Freeze a small set before judging
it on fresh data; later, use `--entry-from YYYY-MM-DD` to start that prospective
cohort without rewriting the original search period.

## Deployment

Two supported ways to keep the scanner running unattended:

### 1. GitHub Actions (zero infrastructure — already wired up)

`.github/workflows/scan.yml` runs the scanner **hourly** on GitHub's runners
and commits each scan as JSONL to `data/snapshots/` (completed days are
gzipped) — the repo itself is the data store. Nothing to host. To analyze the collected data on any
machine:

```bash
git pull
python3 build_db.py   # rebuilds data/scanner.db from the JSONL logs
python3 report.py
```

Caveats: scheduled runs only fire on the default branch, timing jitters by a
few minutes, and a private repo has 2000 free Actions-minutes/month — at real
run times, hourly overshoots that mid-month (runs then pause until the cycle
resets). Making the repo public removes the minutes cap entirely.

#### Going public with a password-gated dashboard

Making the repo public removes the Actions minutes cap. To keep the rendered
dashboard behind a password (note: the raw `data/` files stay world-readable
in a public repo — this gates the view, not the research):

1. Add a repository secret `DASHBOARD_PASSWORD`.
2. Flip the repo to public (Settings → General → Danger Zone).
3. Enable GitHub Pages: Settings → Pages → deploy from branch, select the
   default branch and the `/docs` folder.

Each scan then commits only `docs/index.html` — an AES-256-GCM-encrypted page
that prompts for the password and decrypts in the browser (WebCrypto, PBKDF2
300k iterations). The plaintext `report.html` is no longer committed; generate
it locally anytime with `python3 build_db.py && python3 report_html.py`.

### 2. systemd on an always-on box (best for the real 5-min cadence)

See `deploy/robinhood-scanner.service` — instructions are in the file header.
Any $4–5/month VPS (Hetzner, DigitalOcean) or an always-on home machine works.
This mode writes straight to SQLite; no build step needed.

Run both in parallel if you like — they don't conflict (Actions writes JSONL,
systemd writes SQLite).

## Files

- `scanner.py` — Stage 1 scanner (`--once` or `--loop SECONDS`; `--jsonl` for append-only logs)
- `report.py` — Stage 2 report: baseline rug rate, survivor vs rug launch profiles, current movers
- `report_html.py` — generates `report.html`, a self-contained visual dashboard (auto-refreshed by the workflow every scan; pull and open in a browser)
- `filter_backtest.py` — Stage 2 look-ahead-safe entry-filter grid search
- `build_db.py` — rebuild `data/scanner.db` from `data/snapshots/*.jsonl`
- `db.py` — shared SQLite schema (`data/scanner.db`)
- `.github/workflows/scan.yml` — scheduled scanning on GitHub Actions
- `deploy/robinhood-scanner.service` — systemd unit for VPS deployment
- `docs/strategy-plan.md` — the staged strategy plan this implements

## Following the plan

You are now in **Stage 2**: keep collecting until there are at least 3 weeks of
data, run `report.py` for the rug-rate baseline, and use `filter_backtest.py` to
write down a small set of candidate rules. Do not trade yet; prospective
validation comes before Stage 3.
