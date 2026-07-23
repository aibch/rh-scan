"""Log the model's current top-scored picks to data/picks/YYYY-MM-DD.jsonl.

Run after each scan (the workflow does this). The pick log is the immutable
record of what the score said at the time — validate.py measures these
against what prices did afterwards, and because each entry carries
SCORE_VERSION, later formula changes can never quietly rewrite history.
"""

import json
import math
import os
from datetime import datetime, timezone

import db
import scoring
import auto_paper

PICKS_DIR = os.path.join(db.DATA_DIR, "picks")
TOP_N = auto_paper.TOP_N
ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC_MARKER = os.path.join(ROOT, ".public")


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


def complete_strategy_scan(conn, scan_ts, candidate_count):
    """Only complete, full Top-10 scans can become prospective entries.

    The immutable pick log still records partial/small cohorts for diagnosis,
    but those rows remain unstamped historical observations. This prevents a
    transient API outage from silently changing the strategy's entry universe.
    """
    return strategy_scan_gate(conn, scan_ts, candidate_count)["eligible"]


def strategy_scan_gate(conn, scan_ts, candidate_count):
    """Return the auditable reason a public scan did or did not create a cohort."""
    meta = conn.execute(
        "SELECT requests, failed FROM scan_meta WHERE ts = ?", (scan_ts,)
    ).fetchone()
    requests = int(meta["requests"] or 0) if meta is not None else 0
    failed = int(meta["failed"] or 0) if meta is not None else 0
    complete = meta is not None and requests > 0 and failed == 0
    candidate_count = int(candidate_count)
    eligible = complete and candidate_count == TOP_N
    if eligible:
        reason = "stamped"
    elif meta is None or requests <= 0:
        reason = "missing_scan_metadata"
    elif failed:
        reason = "partial_scan"
    else:
        reason = "fewer_than_10_priceable_candidates"
    return {
        "eligible": eligible,
        "complete_scan": complete,
        "requests": requests,
        "failed_requests": failed,
        "reason": reason,
    }


def make_strategy_scan_manifest(
    scan_ts,
    *,
    ranked_count,
    tradeable_count,
    eligible_cohort_size,
    stamped_entry_count,
    gate,
    recorded_at,
):
    """Identity-free, immutable denominator for hourly strategy capture."""
    return {
        "_meta": auto_paper.SCAN_MANIFEST_TYPE,
        "scan_ts": scan_ts,
        "recorded_at": auto_paper._ts(recorded_at),
        "score_version": scoring.SCORE_VERSION,
        "strategy_id": auto_paper.strategy_id(scoring.SCORE_VERSION),
        "candidate_count": int(ranked_count),
        "tradeable_candidate_count": int(tradeable_count),
        "eligible_cohort_size": int(eligible_cohort_size),
        "stamped_entry_count": int(stamped_entry_count),
        "requests": int(gate["requests"]),
        "failed_requests": int(gate["failed_requests"]),
        "complete_scan": bool(gate["complete_scan"]),
        "reason": str(gate["reason"]),
    }


def valid_candidate_price(candidate):
    """A paper entry requires a finite, positive price on its asset side."""
    try:
        price = float(candidate.get("price_usd"))
    except (AttributeError, TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0


def ranked_tradeable_candidates(rows, now):
    """Rank normally, then remove unpriceable sides before assigning ranks."""
    ranked = scoring.ranked_candidates(rows, now)
    return [candidate for candidate in ranked if valid_candidate_price(candidate)]


def make_pick_record(c, rank, scan_ts, *, public=False, logged_at=None):
    """Build one immutable pick record.

    Public collection runs stamp prospective-strategy metadata. Private runs
    retain the historical signal format so a local rebuild cannot fabricate a
    prospective cohort after seeing its outcome.
    """
    r = c["r"]
    record = {
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
    }
    # Defensive even for direct callers: an invalid quote-side price can be
    # logged diagnostically but can never be stamped as a prospective entry.
    if public and valid_candidate_price(c):
        logged_at = logged_at or datetime.now(timezone.utc)
        record.update(auto_paper.signal_metadata(
            score_version=scoring.SCORE_VERSION,
            scan_ts=scan_ts,
            rank=rank,
            token=c["token"],
            pool=r["address"],
            side=c["side"],
            logged_at=logged_at,
        ))
    return record


def main():
    conn = db.connect()
    try:
        rows = db.latest_rows(conn)
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
        ranked = scoring.ranked_candidates(rows, now)
        tradeable = [c for c in ranked if valid_candidate_price(c)]
        cands = tradeable[:TOP_N]
        skipped_unpriced = len(ranked) - len(tradeable)
        public = os.path.exists(PUBLIC_MARKER)
        gate = strategy_scan_gate(conn, scan_ts, len(cands))
        prospective = public and gate["eligible"]
    finally:
        conn.close()

    os.makedirs(PICKS_DIR, exist_ok=True)
    path = os.path.join(PICKS_DIR, f"{scan_ts[:10]}.jsonl")
    if already_logged(path, scan_ts, scoring.SCORE_VERSION):
        print(f"picks for scan {scan_ts} (v{scoring.SCORE_VERSION}) already logged — skipping")
        return
    lines = []
    if skipped_unpriced:
        print(
            f"skipped {skipped_unpriced} candidate(s) without a finite positive "
            "asset-side price"
        )
    if public and not prospective:
        print(
            f"scan {scan_ts} is partial or has fewer than {TOP_N} candidates "
            "— logging picks without prospective strategy entries"
        )
    # One timestamp for the whole atomic batch; per-member wall-clock drift
    # would make the same Top-10 cohort look as if it were selected piecemeal.
    batch_time = datetime.now(timezone.utc) if public else None
    if public:
        lines.append(json.dumps(
            make_strategy_scan_manifest(
                scan_ts,
                ranked_count=len(ranked),
                tradeable_count=len(tradeable),
                eligible_cohort_size=len(cands),
                stamped_entry_count=len(cands) if prospective else 0,
                gate=gate,
                recorded_at=batch_time,
            ),
            separators=(",", ":"),
        ))
    for rank, c in enumerate(cands, 1):
        lines.append(json.dumps(
            make_pick_record(
                c, rank, scan_ts, public=prospective, logged_at=batch_time
            ),
            separators=(",", ":"),
        ))
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
