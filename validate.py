"""Score validation: did high scores actually predict better outcomes?

Because the score is a pure function of a snapshot, it can be recomputed for
EVERY historical snapshot, not just the moments we published picks. This tool:

  1. Scores every eligible pool at every scan timestamp.
  2. Looks up the same pool's price N days later (forward return).
  3. Reports, per score band: median forward return, rug rate (<= -90%),
     and how many disappeared from tracking (usually death, not success).
  4. Computes the rank correlation (Spearman) between score and forward
     return — the single number that says "the score has predictive power"
     (positive, consistently) or "it doesn't" (≈ 0 or negative).
  5. Shows the biggest score collapses and which component broke first —
     the "it was 94, now it's 20, why?" post-mortem.

Usage:
    python3 validate.py                # 1d/3d/7d horizons
    python3 validate.py --horizons 0.25 1 3   # early on, use shorter windows

Run build_db.py first if your data lives in JSONL.
"""

import argparse
import glob
import sys
import json
import os
from bisect import bisect_left
from collections import defaultdict

import db
import scoring
from scoring import parse_ts

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BANDS = [(80, 101, "80-100"), (60, 80, "60-79"), (40, 60, "40-59"), (0, 40, "0-39")]
# forward observation must land within scoring.horizon_tolerance_s of target


def fmt_pct(v):
    return f"{v:+.1f}%" if v is not None else "–"


def load_onchain_history():
    """token -> sorted [(checked_epoch, record)] from the append-only log.
    The token_onchain table keeps only the LATEST record; joining that onto
    historical snapshots leaks future knowledge into past cohorts (a token
    discovered to be blocked later would be retroactively excluded)."""
    path = os.path.join(db.DATA_DIR, "onchain_history.jsonl")
    hist = defaultdict(list)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("transfer_version", 0) < 2:
                        # pre-decode-fix results conflated RPC errors with
                        # blocks and never decoded ABI returns — untrusted
                        rec = {**rec, "transfer_ok": None}
                    e = parse_ts(rec["checked_at"]).timestamp()
                    hist[rec["token"]].append((e, rec))
                except (ValueError, KeyError):
                    continue
        for v in hist.values():
            v.sort(key=lambda x: x[0])
    return hist


def as_of(hist, token, epoch):
    """Latest on-chain record for token checked AT OR BEFORE epoch, else None."""
    entries = hist.get(token)
    if not entries:
        return None
    rec = None
    for e, r in entries:
        if e <= epoch:
            rec = r
        else:
            break
    return rec


def load(conn):
    rows = conn.execute("""
        SELECT s.ts, s.pool_address AS address, p.name, p.pool_created_at,
               p.base_token, t.symbol AS base_symbol,
               p.quote_token, tq.symbol AS quote_symbol,
               s.price_usd, s.quote_price_usd, s.liquidity_usd, s.vol_h24_usd,
               s.vol_liq_ratio, s.buys_h24, s.sells_h24, s.buyers_h24,
               s.sellers_h24, s.price_change_h24
        FROM snapshots s
        JOIN pools p ON p.address = s.pool_address
        LEFT JOIN tokens t ON t.address = p.base_token
        LEFT JOIN tokens tq ON tq.address = p.quote_token
        ORDER BY s.ts
    """).fetchall()
    hist = load_onchain_history()
    prices = defaultdict(list)   # (pool, side) -> [(epoch, price, liquidity)]
    scored = []                  # (epoch, row, score, key)
    last_rows = {}               # pool -> most recent row (no eligibility gate)
    base_of = {}                 # pool -> base token (for pick-side resolution)
    last_epoch = 0
    for r in rows:
        e = parse_ts(r["ts"]).timestamp()
        last_epoch = max(last_epoch, e)
        liq = r["liquidity_usd"] or 0
        base_of[r["address"]] = r["base_token"]
        if r["price_usd"]:
            prices[(r["address"], "base")].append((e, r["price_usd"], liq))
        if r["quote_price_usd"]:
            prices[(r["address"], "quote")].append((e, r["quote_price_usd"], liq))
        # as-of join: only on-chain knowledge that existed at snapshot time
        r2 = dict(r)
        for tok, prefix in ((r["base_token"], ""), (r["quote_token"], "q_")):
            rec = as_of(hist, tok, e) or {}
            for k in ("verified", "top10_pct", "transfer_ok"):
                r2[prefix + k] = rec.get(k)
        last_rows[r["address"]] = r2
        c = scoring.candidate(r2, parse_ts(r["ts"]))
        # a candidate with no recorded price for ITS OWN side cannot be
        # validated — using the other side's return would misstate outcomes
        if c is not None and c["price_usd"]:
            scored.append((e, r2, c["score"], (r["address"], c["side"])))
    return prices, scored, last_rows, base_of, last_epoch


def forward_return(prices, key, epoch, horizon_s):
    """%change to the first snapshot >= target (within the horizon's
    precommitted tolerance), else None. A drained pool counts as ~total loss —
    its price is meaningless and the position couldn't be exited anyway."""
    series = prices.get(key)
    if not series:
        return None
    target = epoch + horizon_s
    tol = scoring.horizon_tolerance_s(horizon_s / 86400)
    i = bisect_left(series, (target, float("-inf")))
    if i >= len(series) or series[i][0] - target > tol:
        # no observation in the window — but if the pool was already drained
        # at its last observation before the target, that's an absorbing
        # state with a known outcome, not a censored one
        entry0 = next(((p, lq) for e, p, lq in series if abs(e - epoch) < 1), None)
        prior = next((series[j] for j in range(min(i, len(series)) - 1, -1, -1)
                      if series[j][0] <= target), None)
        if entry0 and prior and (prior[2] < 100
                                 or prior[2] <= 0.02 * max(entry0[1], 1)):
            return -99.9
        return None
    entry = next(((p, lq) for e, p, lq in series if abs(e - epoch) < 1), None)
    if not entry:
        return None
    p0, liq0 = entry
    # drained or 98%+ collapsed liquidity = ~total loss; the price print in
    # what's left of the pool is meaningless and the position can't be exited
    if series[i][2] < 100 or series[i][2] <= 0.02 * max(liq0, 1):
        return -99.9
    return (series[i][1] - p0) / p0 * 100


def spearman(pairs):
    """Spearman rank correlation for [(score, fwd_return)]."""
    n = len(pairs)
    if n < 5:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else None


def band_table(prices, scored, last_epoch, horizon_d):
    h = horizon_d * 86400
    usable = [t for t in scored if t[0] + h <= last_epoch]
    if not usable:
        print(f"  {horizon_d}d: no scored snapshots are {horizon_d}+ days old yet")
        return
    stats = {label: {"rets": [], "gone": 0} for _, _, label in BANDS}
    by_scan = defaultdict(list)
    n_pools = len({r["address"] for _, r, _, _ in usable})
    for e, r, sc, key in usable:
        label = next(lb for lo, hi, lb in BANDS if lo <= sc < hi)
        fr = forward_return(prices, key, e, h)
        if fr is None:
            stats[label]["gone"] += 1
        else:
            stats[label]["rets"].append(fr)
            by_scan[e].append((sc, fr))
    # mean per-scan rank IC: snapshots of the same pool across scans are NOT
    # independent observations, so correlate within each scan cohort and
    # average — the standard information-coefficient approach
    ics = [ic for ic in (spearman(pairs) for pairs in by_scan.values())
           if ic is not None]
    ic_str = f"{sum(ics)/len(ics):+.2f} (mean of {len(ics)} scan cohorts)" \
        if ics else "n/a"
    tol_h = scoring.horizon_tolerance_s(horizon_d) / 3600
    print(f"  horizon {horizon_d}d (+{tol_h:.0f}h window) — {len(usable)} "
          f"snapshots of {n_pools} pools, rank IC score→return: {ic_str}")
    print(f"  {'band':>7} {'n':>6} {'median fwd':>11} {'rug rate':>9} {'untracked':>10}")
    for lo, hi, label in BANDS:
        s = stats[label]
        n = len(s["rets"]) + s["gone"]
        if not n:
            continue
        rets = s["rets"]
        med = scoring.median(rets)
        rugs = sum(1 for x in rets if x <= -90)
        rug_rate = f"{100 * rugs / len(rets):.0f}%" if rets else "–"
        print(f"  {label:>7} {n:>6} {fmt_pct(med):>11} {rug_rate:>9} "
              f"{100 * s['gone'] / n:>9.0f}%")
    print()


def picks_report(prices, base_of, last_epoch, horizons, top_rank=3):
    """Validate the PUBLISHED picks from data/picks/ — the record of what the
    model actually said at each scan, keyed by scan timestamp and score
    version. This is the out-of-sample test the pick log exists for."""
    picks = []
    for path in sorted(glob.glob(os.path.join(db.DATA_DIR, "picks", "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("rank", 99) <= top_rank:
                    picks.append(rec)
    if not picks:
        print("  no pick log found (data/picks/)")
        return
    by_version = defaultdict(list)
    for p in picks:
        by_version[p.get("score_version", "?")].append(p)
    for version, vp in sorted(by_version.items(), key=lambda x: str(x[0])):
        scans = len({p["scan_ts"] for p in vp})
        print(f"  score v{version}: top-{top_rank} picks from {scans} scans")
        for hd in horizons:
            h = hd * 86400
            rets, gone, pending = [], 0, 0
            for p in vp:
                e = parse_ts(p["scan_ts"]).timestamp()
                if e + h > last_epoch:
                    pending += 1
                    continue
                tok = p.get("token") or p.get("base_token")
                side = "base" if tok is None or tok == base_of.get(p["pool"]) \
                    else "quote"
                fr = forward_return(prices, (p["pool"], side), e, h)
                if fr is None:
                    gone += 1
                else:
                    rets.append(fr)
            if not rets and not gone:
                print(f"    {hd}d: no picks old enough yet ({pending} pending)")
                continue
            med = scoring.median(rets)
            rugs = sum(1 for x in rets if x <= -90)
            print(f"    {hd}d: n={len(rets)} median {fmt_pct(med)} "
                  f"rugged {rugs} untracked {gone} pending {pending}")
    print()


def decay_report(scored, last_rows, top=8):
    """Biggest score collapses and which component broke. The endpoint is the
    pool's LATEST snapshot scored without eligibility gates — a pool that
    collapsed below the gates is exactly the collapse we want to see."""
    by_pool = defaultdict(list)
    for e, r, sc, _ in scored:
        by_pool[r["address"]].append((e, r, sc))
    drops = []
    for pool, entries in by_pool.items():
        entries.sort(key=lambda t: t[0])
        e0, r0, s0 = entries[0]
        # both endpoints viewed from the ASSET side — a quote-side pool's
        # decay must not be computed from the base token's fields
        _, r0a = scoring.side_adjusted(r0)
        _, r1a = scoring.side_adjusted(last_rows[pool])
        if r0a is None or r1a is None:
            continue
        e1 = parse_ts(r1a["ts"]).timestamp()
        s1 = scoring.total(scoring.subscores(r1a, parse_ts(r1a["ts"])))
        if s0 - s1 >= 20 and e1 > e0:
            sub0 = scoring.subscores(r0a, parse_ts(r0a["ts"]))
            sub1 = scoring.subscores(r1a, parse_ts(r1a["ts"]))
            deltas = [(name, (a - b) * w * 100) for (name, w), a, b
                      in zip(scoring.SCORE_WEIGHTS, sub0, sub1)]
            worst = max(deltas, key=lambda d: d[1])
            drops.append((s0 - s1, r0a, s0, s1, worst))
    if not drops:
        print("  none yet (needs pools whose score fell 20+ points)")
        return
    drops.sort(reverse=True, key=lambda d: d[0])
    for drop, r, s0, s1, (comp, pts) in drops[:top]:
        print(f"  {(r.get('asset_symbol') or '?')[:12]:12} {s0:5.0f} → {s1:5.0f}  "
              f"biggest hit: {comp} (-{pts:.0f} pts)")
    print()


def main():
    ap = argparse.ArgumentParser(description="Validate health scores against outcomes")
    ap.add_argument("--horizons", type=float, nargs="+", default=[1, 3, 7],
                    metavar="DAYS", help="forward-return windows in days")
    args = ap.parse_args()

    conn = db.connect()
    try:
        prices, scored, last_rows, base_of, last_epoch = load(conn)
    finally:
        conn.close()
    if not scored:
        print("no eligible scored snapshots yet — let the scanner run")
        return

    print(f"== Score validation (v{scoring.SCORE_VERSION}) ==")
    print(f"{len(scored)} scored snapshots across "
          f"{len({r['address'] for _, r, _, _ in scored})} pools\n")
    print("Forward returns by score band")
    print("('untracked' = no price data at the horizon — usually a dead pool;")
    print(" treat a high untracked share in high bands as a red flag)\n")
    for hd in args.horizons:
        band_table(prices, scored, last_epoch, hd)
    print("Published-pick performance (from data/picks/, as logged at scan time)")
    picks_report(prices, base_of, last_epoch, args.horizons)
    print("Score collapses (first eligible vs latest ungated score per pool)")
    decay_report(scored, last_rows)
    print("Read: the score works if high bands beat low bands consistently AND\n"
          "rank correlation stays positive across horizons. If not, re-weight\n"
          "(bump SCORE_VERSION) and re-validate — never against the same period\n"
          "you tuned on.")


if __name__ == "__main__":
    main()
