"""Volume-surge detection over recorded snapshots (read-only, self-contained).

A surge is a violent burst of traded volume between two consecutive scans of
the same pool: at least MIN_DVOL_USD in the gap AND at least LIQ_RATIO times
the pool's liquidity. This module only READS the snapshot store — it changes
nothing about scanning, scoring, or validation.

Context from this dataset (see the spike study): entries at first detection
of such surges have shown deeply negative median forward returns and roughly
triple the baseline rug rate. Surges are a watchlist signal, not a buy signal.
"""

from datetime import datetime, timezone

MIN_DVOL_USD = 25_000   # minimum volume traded within one scan gap
LIQ_RATIO = 2.0         # ...and at least this multiple of pool liquidity
MIN_LIQ_USD = 1_000     # ignore dust pools (their "volume" is theater)
MIN_GAP_S = 30 * 60     # gaps shorter than this can't be measured reliably
MAX_GAP_S = 4 * 3600    # gaps longer than this dilute the "sudden" signal


def is_surge(dvol, liq):
    """The surge predicate, shared by the report view and any watcher."""
    return (dvol >= MIN_DVOL_USD
            and liq >= MIN_LIQ_USD
            and dvol >= LIQ_RATIO * liq)


def _epoch(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=timezone.utc).timestamp()


def recent_surges(conn, hours=24, limit=15):
    """Largest surges detected in the last `hours`, newest data first by
    surge size. Returns plain dicts ready for rendering."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    rows = conn.execute("""
        SELECT s.ts, s.pool_address, s.price_usd, s.liquidity_usd,
               s.vol_h24_usd, p.name
        FROM snapshots s JOIN pools p ON p.address = s.pool_address
        ORDER BY s.pool_address, s.ts
    """).fetchall()

    out = []
    prev = None
    for r in rows:
        cur = (r["pool_address"], _epoch(r["ts"]), r["price_usd"],
               r["liquidity_usd"] or 0, r["vol_h24_usd"] or 0, r["name"])
        if prev and prev[0] == cur[0]:
            gap = cur[1] - prev[1]
            if MIN_GAP_S <= gap <= MAX_GAP_S and cur[1] >= cutoff:
                dvol = cur[4] - prev[4]
                liq0 = prev[3]
                if is_surge(dvol, liq0):
                    move = ((cur[2] - prev[2]) / prev[2] * 100
                            if prev[2] and cur[2] else None)
                    out.append({
                        "ts": r["ts"], "pool": cur[0], "name": cur[5],
                        "dvol": dvol, "liq_before": liq0, "liq_now": cur[3],
                        "ratio": dvol / liq0 if liq0 else None,
                        "price_move_pct": move,
                        "gap_minutes": gap / 60,
                    })
        prev = cur
    out.sort(key=lambda s: s["dvol"], reverse=True)
    return out[:limit]
