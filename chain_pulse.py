"""Chain-level activity pulse (read-only, self-contained).

Aggregates the whole chain's vital signs from existing snapshots into
6-hour buckets and a composite 0-100 pulse score:

  - traders:  sum of unique 24h buyers+sellers across real pools — "active
              trader slots". NOT unique people (wallets are cheap), but it
              rises and falls with genuine participation.
  - volume:   24h traded volume in pools with real liquidity (dust excluded)
  - tvl:      total liquidity locked across real pools
  - active:   pools with >= $1k of 24h volume ("markets with a heartbeat")
  - launches: pools first sighted per bucket (shown, not scored — more
              launches also means more scam supply)

The pulse compares now vs ~24h ago on a log scale: 50 = flat, 100 = doubled,
0 = halved. It measures ACTIVITY MOMENTUM, not price direction and not
social sentiment.

Usage: python3 chain_pulse.py   (or rendered as a dashboard panel)
"""

import sys
from datetime import datetime, timezone

import db
import scoring

BUCKET_S = 6 * 3600
MIN_LIQ = 100          # dust pools fake volume and traders; exclude
PULSE_WEIGHTS = [      # (metric key, weight) — must sum to 1
    ("traders", 0.35),
    ("volume", 0.25),
    ("tvl", 0.25),
    ("active", 0.15),
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _epoch(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=timezone.utc).timestamp()


def series(conn):
    """Per 6h bucket: each pool's LAST snapshot in the bucket, aggregated.
    24h-rolling fields summed at a time point = 'chain activity as of then'."""
    rows = conn.execute("""
        SELECT s.ts, s.pool_address, s.liquidity_usd, s.vol_h24_usd,
               s.buyers_h24, s.sellers_h24
        FROM snapshots s ORDER BY s.ts
    """).fetchall()
    first_seen = {r[0]: _epoch(r[1]) for r in conn.execute(
        "SELECT address, first_seen_at FROM pools")}

    buckets = {}
    raw_counts = {}
    for r in rows:
        b = int(_epoch(r["ts"]) // BUCKET_S)
        buckets.setdefault(b, {})[r["pool_address"]] = r
        raw_counts[b] = raw_counts.get(b, 0) + 1

    out = []
    for b in sorted(buckets):
        pools = buckets[b].values()
        tvl = vol = traders = active = 0
        for r in pools:
            liq = r["liquidity_usd"] or 0
            if liq < MIN_LIQ:
                continue
            tvl += liq
            v = r["vol_h24_usd"] or 0
            vol += v
            traders += (r["buyers_h24"] or 0) + (r["sellers_h24"] or 0)
            if v >= 1000:
                active += 1
        launches = sum(1 for e in first_seen.values()
                       if b * BUCKET_S <= e < (b + 1) * BUCKET_S)
        out.append({"epoch": b * BUCKET_S, "tvl": tvl, "volume": vol,
                    "traders": traders, "active": active, "launches": launches,
                    "snapshots": raw_counts.get(b, 0)})
    return out


def _log2_score(ratio):
    """1.0 -> 50 (flat), 2.0 -> 100 (doubled), 0.5 -> 0 (halved)."""
    import math
    if ratio <= 0:
        return 0.0
    return scoring.clamp(50 + 50 * math.log2(ratio), 0, 100)


def pulse(conn):
    """Composite pulse: latest bucket vs ~24h earlier. None until 2 days."""
    s = series(conn)
    if len(s) < 6:
        return None
    latest = s[-1]
    # compare against the bucket ~24h before the latest
    target = latest["epoch"] - 24 * 3600
    prev = min(s[:-1], key=lambda x: abs(x["epoch"] - target))
    if abs(prev["epoch"] - target) > 12 * 3600:
        return None
    components = {}
    total = 0.0
    for key, w in PULSE_WEIGHTS:
        ratio = latest[key] / prev[key] if prev[key] else (1.0 if not latest[key] else 2.0)
        sc = _log2_score(ratio)
        components[key] = {"now": latest[key], "prev": prev[key],
                           "change_pct": (ratio - 1) * 100, "score": sc}
        total += w * sc
    return {"score": total, "components": components,
            "latest": latest, "series": s}


def main():
    conn = db.connect()
    try:
        p = pulse(conn)
    finally:
        conn.close()
    if p is None:
        print("not enough bucketed history yet (needs ~2 days)")
        return
    print(f"== Chain pulse: {p['score']:.0f}/100 "
          f"(50 = flat vs 24h ago, >50 growing, <50 shrinking) ==")
    for key, w in PULSE_WEIGHTS:
        c = p["components"][key]
        print(f"  {key:8} now {c['now']:>14,.0f}   vs 24h {c['change_pct']:+7.1f}%   "
              f"score {c['score']:5.1f}  (weight {w:.0%})")
    print(f"  launches (per 6h, informational): {p['latest']['launches']}")


if __name__ == "__main__":
    main()
