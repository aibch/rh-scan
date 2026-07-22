"""Log the model's current top-scored picks to data/picks/YYYY-MM-DD.jsonl.

Run after each scan (the workflow does this). The pick log is the immutable
record of what the score said at the time — validate.py measures these
against what prices did afterwards, and because each entry carries
SCORE_VERSION, later formula changes can never quietly rewrite history.
"""

import json
import os
from datetime import datetime, timezone

import db
import scoring

PICKS_DIR = os.path.join(db.DATA_DIR, "picks")
TOP_N = 10


def already_logged(path, scan_ts, version):
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("scan_ts") == scan_ts and rec.get("score_version") == version:
                return True
    return False


def main():
    conn = db.connect()
    try:
        rows = db.latest_rows(conn)
    finally:
        conn.close()
    if not rows:
        print("no data to score")
        return
    scan_ts = max(r["ts"] for r in rows)
    # only pools actually observed in the labeled scan — tiered re-polling
    # means "latest per pool" can be hours stale, and a pick with no snapshot
    # at its own label timestamp can't be validated later
    rows = [r for r in rows if r["ts"] == scan_ts]
    # score AS OF the scan being labeled, not wall-clock time — the log must
    # be reproducible from the data it claims to describe
    now = scoring.parse_ts(scan_ts)
    cands = scoring.ranked_candidates(rows, now)[:TOP_N]

    os.makedirs(PICKS_DIR, exist_ok=True)
    path = os.path.join(PICKS_DIR, f"{scan_ts[:10]}.jsonl")
    if already_logged(path, scan_ts, scoring.SCORE_VERSION):
        print(f"picks for scan {scan_ts} (v{scoring.SCORE_VERSION}) already logged — skipping")
        return
    lines = []
    for rank, c in enumerate(cands, 1):
        r = c["r"]
        lines.append(json.dumps({
            "scan_ts": scan_ts,
            "score_version": scoring.SCORE_VERSION,
            "rank": rank,
            "score": round(c["score"], 1),
            "subscores": {name: round(s, 3) for (name, _), s
                          in zip(scoring.SCORE_WEIGHTS, c["subs"])},
            "pool": r["address"],
            "token": c["token"],
            "symbol": c["symbol"],
            "price_usd": c["price_usd"],
            "liquidity_usd": r["liquidity_usd"],
            "vol_h24_usd": r["vol_h24_usd"],
        }, separators=(",", ":")))
    # atomic append: read-modify-replace, so a crash can never leave a
    # partial batch that a rerun would then skip as "already logged"
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(existing + "".join(line + "\n" for line in lines))
    os.replace(tmp, path)
    print(f"logged {len(cands)} picks for scan {scan_ts} -> {path}")


if __name__ == "__main__":
    main()
