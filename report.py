"""Stage 2 report over scanner data.

Answers, from whatever data the scanner has collected so far:
  - How many tokens/pools have we tracked, over what time span?
  - Of pools first seen >= N days ago: how many rugged (down 90%+ from the
    price at first sighting)?  -> your baseline rug rate
  - What did survivors vs rugs look like at first sighting (liquidity,
    volume/liquidity)?  -> raw material for filter rules
  - Current top pools by 24h volume.

Usage:
    python report.py                 # full report, rug window = 7 days
    python report.py --age-days 3    # use a shorter maturity window early on
"""

import argparse
import sys
import scoring
from datetime import datetime, timedelta, timezone

import db

RUG_DROP_PCT = -90.0

# Windows consoles default to cp1252 — force utf-8 so the report prints
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fmt_usd(v):
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.2f}"


def overview(conn):
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM tokens) AS tokens, "
        "(SELECT COUNT(*) FROM pools) AS pools, "
        "(SELECT COUNT(*) FROM snapshots) AS snaps, "
        "(SELECT MIN(ts) FROM snapshots) AS first, "
        "(SELECT MAX(ts) FROM snapshots) AS last").fetchone()
    print("== Overview ==")
    print(f"tokens: {row['tokens']}   pools: {row['pools']}   snapshots: {row['snaps']}")
    print(f"data span: {row['first']}  ->  {row['last']}")
    meta = conn.execute(
        "SELECT COUNT(*), SUM(failed > 0) FROM scan_meta "
        "WHERE ts >= datetime('now', '-1 day')").fetchone()
    if meta and meta[0]:
        print(f"coverage last 24h: {meta[0]} scans recorded, "
              f"{meta[1] or 0} partial")
    print()


def pool_journeys(conn, horizon_days):
    """Outcome per pool at a FIXED horizon after first sighting, measured on
    the pool's ASSET side (usually the base token, sometimes the quote).
    Returns (journeys, censored, pending): censored pools reached the horizon
    but have no observation within the 12h tolerance window (usually retired
    dead pools); pending pools simply aren't old enough yet."""
    rows = conn.execute("""
        SELECT p.address, p.name, p.base_token, p.quote_token,
               t.symbol AS base_symbol, tq.symbol AS quote_symbol,
               f.ts AS ts0, f.price_usd AS p0b, f.quote_price_usd AS p0q,
               f.liquidity_usd AS liq0
        FROM pools p
        LEFT JOIN tokens t ON t.address = p.base_token
        LEFT JOIN tokens tq ON tq.address = p.quote_token
        JOIN snapshots f ON f.id = (SELECT id FROM snapshots WHERE pool_address = p.address
                                    ORDER BY ts ASC LIMIT 1)
    """).fetchall()
    now = datetime.now(timezone.utc)
    tol = timedelta(seconds=scoring.horizon_tolerance_s(horizon_days))
    out, censored, pending, unpriced = [], 0, 0, 0
    for r in rows:
        side = scoring.asset_side(r)
        if side is None or (r["liq0"] or 0) < 100:  # not a judgeable asset pool
            continue
        p0 = r["p0b"] if side == "base" else r["p0q"]
        if not p0:
            unpriced += 1  # asset-side price wasn't recorded at first sighting
            continue
        t0 = datetime.strptime(r["ts0"], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
        target = t0 + timedelta(days=horizon_days)
        if now < target:
            pending += 1
            continue
        col = "price_usd" if side == "base" else "quote_price_usd"
        h = conn.execute(
            f"SELECT {col} AS p1, liquidity_usd AS liq1 FROM snapshots "
            "WHERE pool_address = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts ASC LIMIT 1",
            (r["address"], target.strftime("%Y-%m-%dT%H:%M:%SZ"),
             (target + tol).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
        if h is None or h["p1"] is None:
            # no observation in the window — but a pool whose LAST observation
            # before the target was already drained is in an absorbing state:
            # its outcome at the horizon is known, not censored
            last = conn.execute(
                "SELECT liquidity_usd FROM snapshots WHERE pool_address = ? "
                "AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (r["address"], target.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
            lliq = (last[0] or 0) if last else 0
            if lliq < 100 or lliq <= 0.02 * r["liq0"]:
                out.append({**dict(r), "liq1": lliq, "change_pct": -100.0})
            else:
                censored += 1
            continue
        # a drained pool is a rug no matter what its price says: prices in
        # dust-liquidity pools are meaningless (division by near-zero reserves)
        if (h["liq1"] or 0) < 100 or (h["liq1"] or 0) <= 0.02 * r["liq0"]:
            change = -100.0
        else:
            change = (h["p1"] - p0) / p0 * 100
        out.append({**dict(r), "liq1": h["liq1"], "change_pct": change})
    return out, censored, pending, unpriced


def rug_report(conn, min_age_days):
    journeys, censored, pending, unpriced = pool_journeys(conn, min_age_days)
    tol_h = scoring.horizon_tolerance_s(min_age_days) / 3600
    print(f"== Rug analysis: outcomes at {min_age_days}d after first sighting "
          f"(observed within +{tol_h:.0f}h of target) ==")
    if not journeys:
        print(f"No pools observed for {min_age_days}+ days yet. Let the scanner run, "
              f"or try --age-days 1.\n")
        return
    rugs = [j for j in journeys if j["change_pct"] <= RUG_DROP_PCT]
    up = [j for j in journeys if j["change_pct"] > 0]
    n, c = len(journeys), censored
    lo, hi = 100 * len(rugs) / (n + c), 100 * (len(rugs) + c) / (n + c)
    print(f"measured: {len(journeys)}   censored (no observation in window, "
          f"likely dead): {censored}   unpriced (excluded): {unpriced}   "
          f"not yet mature: {pending}")
    print(f"rugged (<= {RUG_DROP_PCT:.0f}% or drained at horizon): {len(rugs)} "
          f"({100*len(rugs)/len(journeys):.1f}% of measured; strict bounds "
          f"{lo:.1f}%-{hi:.1f}% counting censored)   up: {len(up)}")

    def profile(group, label):
        if not group:
            return
        liqs = [j["liq0"] for j in group if j["liq0"] is not None]
        if liqs:
            print(f"  {label}: n={len(group)}, median first-sighting liquidity "
                  f"{fmt_usd(scoring.median(liqs))}")

    profile(rugs, "rugs      ")
    profile([j for j in journeys if j["change_pct"] > RUG_DROP_PCT], "survivors ")

    survivors = sorted((j for j in journeys if j["change_pct"] > RUG_DROP_PCT),
                       key=lambda j: j["change_pct"], reverse=True)[:10]
    if survivors:
        print("\ntop survivors at horizon:")
        for j in survivors:
            print(f"  {j['name'][:32]:32} {j['change_pct']:+9.1f}%  "
                  f"liq {fmt_usd(j['liq0'])} -> {fmt_usd(j['liq1'])}")
    print()


def current_top(conn, n=15):
    rows = conn.execute("""
        SELECT p.name, s.price_usd, s.liquidity_usd, s.vol_h24_usd, s.vol_liq_ratio,
               s.buys_h24, s.sells_h24, s.price_change_h24
        FROM pools p
        JOIN snapshots s ON s.id = (SELECT id FROM snapshots WHERE pool_address = p.address
                                    ORDER BY ts DESC LIMIT 1)
        WHERE s.vol_h24_usd IS NOT NULL AND s.liquidity_usd >= 1000
        ORDER BY s.vol_h24_usd DESC LIMIT ?
    """, (n,)).fetchall()
    print(f"== Current top {n} pools by 24h volume (liquidity >= $1k) ==")
    print(f"{'pool':32} {'liq':>9} {'vol24h':>9} {'vol/liq':>8} {'24h%':>8} {'buys':>5} {'sells':>5}")
    for r in rows:
        print(f"{(r['name'] or '?')[:32]:32} {fmt_usd(r['liquidity_usd']):>9} "
              f"{fmt_usd(r['vol_h24_usd']):>9} "
              f"{(f'{r[4]:.2f}' if r['vol_liq_ratio'] is not None else '-'):>8} "
              f"{(f'{r[7]:+.1f}' if r['price_change_h24'] is not None else '-'):>8} "
              f"{r['buys_h24'] or 0:>5} {r['sells_h24'] or 0:>5}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Report over scanner data")
    ap.add_argument("--age-days", type=float, default=7,
                    help="min pool age (days since first sighting) for rug analysis")
    args = ap.parse_args()

    conn = db.connect()
    try:
        overview(conn)
        rug_report(conn, args.age_days)
        current_top(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
