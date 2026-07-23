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

## Paper trade tracker

Paper entries live in an append-only private ledger at
`data/paper_trades.jsonl`. Each buy is a separate lot, so buying the same token
on different days preserves each entry price, timestamp, quantity, and result.

```bash
# prompted entry (recommended)
python3 paper_trades.py add

# equivalent non-interactive entry
python3 paper_trades.py add \
  --token 0x0123456789abcdef0123456789abcdef01234567 \
  --symbol TOKEN --price 0.0000125 --at 2026-07-22T18:30:00Z \
  --usd 50 --note "candidate rule A"

python3 paper_trades.py list

# closes the entire selected lot; repeat buys remain separate
python3 paper_trades.py close LOT_ID \
  --price 0.0000152 --at 2026-07-24T09:00:00Z --note "take profit"

# audit-preserving correction for an erroneous open entry
python3 paper_trades.py void LOT_ID --reason "wrong token address"

# rebuild the visual dashboard
python3 build_db.py
python3 report_html.py
```

Token addresses must be full 40-hex EVM addresses. Entry and exit timestamps
must include `Z` or an explicit timezone offset and cannot be in the future.
`close` is full-lot only; use separate entry lots when you want independently
managed position slices. The ledger separately records when the command was
run; entries recorded more than 15 minutes after their claimed entry time are
visibly flagged as backfilled to protect prospective paper-trading evidence.

The dashboard shows cumulative deployed capital, open marked value, realized
and unrealized P&L, a 6-hour portfolio P&L trend, price coverage/staleness, and
one outcome row per lot. “Current” means the latest price recorded by this
scanner (normally refreshed hourly), not a streaming or executable quote. If
an open token has no scanner price at or after entry, total portfolio P&L and
return remain unavailable rather than treating it as break-even.

Results are gross of gas, fees, and slippage. For pessimistic Stage 3
simulation, enter the fill price after your assumed slippage and account for
gas separately in the note until explicit cost fields are added. Building the
tracker early does not advance the project out of Stage 2 or authorize live
trading.

### Automatic hourly Top 10 research cohort

The automatic strategy is deliberately separate from manual paper trades. On
each complete public hourly scan, the ten highest-ranked priceable assets create
independent **$1 virtual signals**. Each signal fills at the first valid recorded
pool-side price within two hours of ranking or expires as a missed fill. No
automatic event is written to
`data/paper_trades.jsonl`; the cohort is derived from the existing immutable,
AES-encrypted `data/picks/` records.

Each prospective signal is deterministic and idempotent, keyed by its scan,
token, rank, and score version, so a workflow retry cannot create a duplicate.
The record preserves the rank, total score, `score_version`, scan quote, and
the later `logged_at` time when that ranking actually became available. Its
official strategy stamp is created only by the public collector; a
private/local scan cannot manufacture prospective entries. All stamped score
versions remain in the combined live book after future model-version bumps.

The strategy targets an exit at **+24h from the actual fill**. It uses the first
recorded quote from that target through +6h; without one, the outcome is
censored rather than silently carried forward. Returns at **+1h, +6h, +72h,
and +168h** use their precommitted observation windows and remain diagnostics;
the 72h and 168h marks do not extend exposure beyond the 24h target. Tooltips
show the actual observation timestamp and delay. The dashboard keeps three
populations distinct:

- live prospective automatic signals, fills, and missed fills after activation;
- a clearly labelled historical preview replayed from earlier immutable picks;
- manually entered paper trades from the private ledger.

At a full ten filled entries per hour, the strategy deploys $10 of research
notional per hour. After the first 24 hours, the exit target produces an
expected rolling open exposure of about **$240** (`10 × $1 × 24`), while
cumulative deployed notional continues to grow by up to $240 per day. Fewer
eligible picks, missing prices, or missed scans reduce those figures.

The $1 unit is for comparable research returns, not cash and not a suggested
position size. Results are gross: they do not model gas, fees, taxes, slippage,
liquidity limits, failed execution, or sell restrictions. The earlier scan
quote is provenance only. A prospective signal remains `awaiting_fill` until
the first valid recorded price for the selected pool side in
`[logged_at, logged_at + 2h]`; that observation supplies both entry price and
liquidity, and the 24-hour clock starts at its timestamp. If no such quote
arrives, the signal becomes a terminal `missed_fill` and is never deployed
later. Candidates without a finite positive asset-side price are removed before
prospective ranking/stamping.

Every public run that reaches `log_picks.py` also appends an identity-free scan
manifest recording whether the prospective cohort was accepted or gated
(partial scan, missing scan metadata, or fewer than ten priceable candidates).
The dashboard calls this **logged-attempt acceptance**: it does not include
scheduler misses, failures before the pick-log step, or pushes that never land,
which remain visible only in GitHub Actions/workflow monitoring.

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

Making the deployment twin public removes the Actions minutes cap. The public
tree carries market data and the paper ledger only as AES-encrypted files under
`dataenc/`; plaintext `data/` and `report.html` are excluded. The rendered
dashboard is separately password-gated:

1. Add a repository secret `DASHBOARD_PASSWORD`.
2. Flip the repo to public (Settings → General → Danger Zone).
3. Enable GitHub Pages: Settings → Pages → deploy from branch, select the
   default branch and the `/docs` folder.

Each scan then commits only `docs/index.html` — an AES-256-GCM-encrypted page
that prompts for the password and decrypts in the browser (WebCrypto, PBKDF2
300k iterations). The plaintext `report.html` is no longer committed; generate
it locally anytime with `python3 build_db.py && python3 report_html.py`.

The paper ledger is private-authoritative. After adding or closing a trade:

1. Commit and push `data/paper_trades.jsonl` to the private repository.
2. Run the private repository's `pack-data` workflow (or run
   `DASHBOARD_PASSWORD=... python3 crypt_data.py pack` locally), then pull its
   ciphertext commit.
3. Run the sanitized public-export script. Public Actions decrypt the ledger
   only inside the job to render the dashboard and commit only its encrypted
   mirror.

The automatic Top 10 cohort does not change that ownership flow. It derives
signals and fills from the public collector's encrypted immutable `data/picks/`, stores
no automatic trades in the manual ledger, and requires no private-to-public
paper-ledger merge. Its strategy stamp contains no user-entered amount, note,
wallet, or identity; the official prospective stamp is generated only on the
public deployment. Automatic, historical-preview, and manual results remain
separate in the password-gated report.

Public history can reveal that the encrypted ledger changed and its approximate
size, but not its token addresses, prices, timestamps, amounts, or notes.

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
- `paper_trades.py` — append-only paper-lot entry/close/void CLI plus portfolio valuation and P&L trend engine
- `auto_paper.py` — derives versioned live/historical automatic books, bounded fills, outcomes, summaries, and chart payloads
- `log_picks.py` — immutable ranked picks plus public-only automatic strategy stamps and scan-acceptance manifests
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
validation comes before Stage 3. The paper tracker is available early for
workflow testing, and the automatic Top 10 cohort is an additional Stage 2
research feed. Neither starts the formal 30-trade Stage 3 cohort, which begins
only after the Stage 2 entry, exit, and risk rules are frozen.
