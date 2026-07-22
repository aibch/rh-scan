"""Look-ahead-safe grid search for Stage 2 entry-filter rules.

The score validator answers whether score bands predict returns. This tool
answers the next question: which entry filters would have reduced rugs while
retaining a useful number of candidates?

Every rule is evaluated as it could have run live:

* on-chain values are joined as of the entry snapshot by ``validate.load``;
* each asset is entered once, at its first qualifying snapshot for that rule;
* forward observations use the validator's precommitted tolerance windows;
* drained pools are absorbing rugs, while genuinely missing outcomes remain
  censored and are reported as strict lower/upper rug-rate bounds; and
* unknown on-chain values fail any gate that explicitly requires them.

The search population is the current scoring model's market-eligible candidate
population. Confirmed transfer blocks are therefore excluded even when a rule
does not require a positive transfer check; this matches the production hard
gate in ``scoring.candidate``.

This is an exploratory search, not out-of-sample proof. Freeze a small set of
rules before judging them on fresh data (``--entry-from`` makes that later
prospective check reproducible).

Usage:
    python filter_backtest.py
    python filter_backtest.py --horizons 1 3 5 --top 30
    python filter_backtest.py --csv data/filter_backtest.csv
    python filter_backtest.py --entry-from 2026-08-01
"""

import argparse
import csv
import hashlib
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import product

import db
import scoring
import validate


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_LIQUIDITY_FLOORS = (10_000.0, 25_000.0, 50_000.0, 100_000.0)
DEFAULT_MIN_AGES = (0.0, 0.25, 1.0, 3.0, 7.0)
DEFAULT_SCORE_THRESHOLDS = (0.0, 50.0, 60.0, 70.0, 80.0)
DEFAULT_TOP10_CAPS = (30.0, 50.0)


@dataclass(frozen=True)
class Rule:
    liquidity_floor: float
    min_age_days: float
    score_threshold: float
    require_verified: bool
    top10_max: object
    require_transfer_ok: bool


@dataclass(frozen=True)
class Entry:
    epoch: float
    token: str
    pool: str
    key: tuple
    score: float
    liquidity_usd: float
    age_days: object
    verified: object
    top10_pct: object
    transfer_ok: object


def parse_boundary(value):
    """Parse YYYY-MM-DD or the repository's UTC timestamp format."""
    if value is None:
        return None
    if len(value) == 10:
        value += "T00:00:00Z"
    try:
        return scoring.parse_ts(value).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ") from exc


def build_entries(scored, entry_from=None, entry_before=None):
    """Normalize validator snapshots into entry facts, ordered so a same-scan
    multi-pool tie selects the highest-scoring qualifying pool."""
    entries = []
    for epoch, row, score, key in scored:
        if entry_from is not None and epoch < entry_from:
            continue
        if entry_before is not None and epoch >= entry_before:
            continue
        cand = scoring.candidate(row, scoring.parse_ts(row["ts"]))
        if cand is None or not cand["price_usd"]:
            continue
        if row["pool_created_at"]:
            age = (scoring.parse_ts(row["ts"])
                   - scoring.parse_ts(row["pool_created_at"])).total_seconds() / 86400
            age = max(0.0, age)
        else:
            age = None
        token = cand["token"].lower()
        entries.append(Entry(
            epoch=epoch,
            token=token,
            pool=row["address"],
            key=key,
            score=score,
            liquidity_usd=row["liquidity_usd"] or 0.0,
            age_days=age,
            verified=cand["verified"],
            top10_pct=cand["top10_pct"],
            transfer_ok=cand["transfer_ok"],
        ))
    return sorted(entries, key=lambda e: (e.epoch, -e.score, e.pool))


def passes_rule(entry, rule):
    if entry.liquidity_usd < rule.liquidity_floor:
        return False
    if entry.score < rule.score_threshold:
        return False
    if rule.min_age_days > 0 and (
            entry.age_days is None or entry.age_days < rule.min_age_days):
        return False
    if rule.require_verified and entry.verified != 1:
        return False
    if rule.top10_max is not None and (
            entry.top10_pct is None or entry.top10_pct > rule.top10_max):
        return False
    if rule.require_transfer_ok and entry.transfer_ok != 1:
        return False
    return True


def first_qualifying_entries(entries, rule):
    """One live-like entry per asset for this rule."""
    seen = set()
    selected = []
    for entry in entries:
        if entry.token in seen or not passes_rule(entry, rule):
            continue
        seen.add(entry.token)
        selected.append(entry)
    return selected


def rule_grid(liquidity_floors=DEFAULT_LIQUIDITY_FLOORS,
              min_ages=DEFAULT_MIN_AGES,
              score_thresholds=DEFAULT_SCORE_THRESHOLDS,
              top10_caps=DEFAULT_TOP10_CAPS):
    top10_options = (None,) + tuple(top10_caps)
    return [Rule(*values) for values in product(
        liquidity_floors,
        min_ages,
        score_thresholds,
        (False, True),
        top10_options,
        (False, True),
    )]


def percentile(values, pct):
    """Linear-interpolated percentile, with pct expressed from 0 to 100."""
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct / 100
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def cohort_id(entries):
    digest = hashlib.sha1()
    for entry in entries:
        digest.update(
            f"{entry.token}|{entry.pool}|{entry.epoch:.0f}\n".encode("ascii"))
    return digest.hexdigest()[:12]


def evaluate_entries(entries, prices, last_epoch, horizon_days, cache=None):
    """Evaluate one rule's selected entries at one horizon."""
    if cache is None:
        cache = {}
    horizon_s = horizon_days * 86400
    returns = []
    censored = 0
    pending = 0
    rugs = 0
    for entry in entries:
        if entry.epoch + horizon_s > last_epoch:
            pending += 1
            continue
        ck = (entry.key, entry.epoch, horizon_s)
        if ck not in cache:
            cache[ck] = validate.forward_return(
                prices, entry.key, entry.epoch, horizon_s)
        result = cache[ck]
        if result is None:
            censored += 1
            continue
        returns.append(result)
        if result <= -90:
            rugs += 1

    measured = len(returns)
    mature = measured + censored
    mean = sum(returns) / measured if measured else None
    lower = rugs / mature if mature else None
    upper = (rugs + censored) / mature if mature else None
    return {
        "entries": len(entries),
        "mature": mature,
        "measured": measured,
        "censored": censored,
        "pending": pending,
        "rugs": rugs,
        "rug_rate_measured": rugs / measured if measured else None,
        "rug_rate_lower": lower,
        "rug_rate_upper": upper,
        "return_min": min(returns) if returns else None,
        "return_p10": percentile(returns, 10),
        "return_p25": percentile(returns, 25),
        "return_median": percentile(returns, 50),
        "return_mean": mean,
        "return_p75": percentile(returns, 75),
        "return_p90": percentile(returns, 90),
        "return_max": max(returns) if returns else None,
        "positive_rate": (sum(v > 0 for v in returns) / measured
                          if measured else None),
    }


def result_row(rule, entries, prices, last_epoch, horizon_days, cache):
    return {
        "score_version": scoring.SCORE_VERSION,
        "horizon_days": horizon_days,
        "cohort_id": cohort_id(entries),
        **asdict(rule),
        **evaluate_entries(entries, prices, last_epoch, horizon_days, cache),
    }


def fmt_usd(value):
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}m"
    return f"${value / 1000:.0f}k"


def fmt_number(value):
    return "-" if value is None else f"{value:+.1f}"


def fmt_rate(value):
    return "-" if value is None else f"{100 * value:.0f}%"


def ranking_key(row, sort_by):
    rug = row["rug_rate_upper"] if row["rug_rate_upper"] is not None else 2.0
    med = row["return_median"] if row["return_median"] is not None else -math.inf
    mean = row["return_mean"] if row["return_mean"] is not None else -math.inf
    if sort_by == "median":
        return (-med, rug, -row["mature"])
    if sort_by == "mean":
        return (-mean, rug, -row["mature"])
    return (rug, -med, -row["mature"])


def print_row(row, prefix="  "):
    top10 = "-" if row["top10_max"] is None else f"{row['top10_max']:.0f}"
    bounds = f"{fmt_rate(row['rug_rate_lower'])}-{fmt_rate(row['rug_rate_upper'])}"
    print(
        f"{prefix}{fmt_usd(row['liquidity_floor']):>6} "
        f"{row['min_age_days']:>4g} {row['score_threshold']:>5g} "
        f"{'Y' if row['require_verified'] else '-':>3} {top10:>5} "
        f"{'Y' if row['require_transfer_ok'] else '-':>3} "
        f"{row['entries']:>5} {row['measured']:>4}/{row['censored']:<4} "
        f"{row['pending']:>4} {bounds:>9} "
        f"{fmt_number(row['return_p25']):>7} "
        f"{fmt_number(row['return_median']):>7} "
        f"{fmt_number(row['return_p75']):>7} "
        f"{fmt_number(row['return_mean']):>7}"
    )


def print_results(rows, horizons, baseline_rule, top, min_mature, sort_by):
    for horizon in horizons:
        hr = [r for r in rows if r["horizon_days"] == horizon]
        baseline = next(r for r in hr if all(
            r[name] == value for name, value in asdict(baseline_rule).items()))
        print(f"\n== {horizon:g}d horizon (+"
              f"{scoring.horizon_tolerance_s(horizon) / 3600:g}h window) ==")
        print("  liq>=  age score ver top10  tx entry  obs/cens pend rug bounds"
              "     p25  median     p75    mean")
        print("  candidate baseline")
        print_row(baseline)

        eligible = [r for r in hr if r["mature"] >= min_mature and r["measured"]]
        eligible.sort(key=lambda r: ranking_key(r, sort_by))
        unique = []
        seen_cohorts = set()
        for row in eligible:
            if row["cohort_id"] in seen_cohorts:
                continue
            seen_cohorts.add(row["cohort_id"])
            unique.append(row)
            if len(unique) >= top:
                break
        print(f"  top {len(unique)} distinct cohorts by {sort_by} "
              f"(minimum {min_mature} mature entries)")
        for row in unique:
            print_row(row)
        if not unique:
            print("  none meet the minimum mature-entry requirement")


def write_csv(path, rows):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(
        description="Grid-search look-ahead-safe Stage 2 entry filters")
    ap.add_argument("--horizons", type=float, nargs="+", default=[1, 3, 7],
                    metavar="DAYS")
    ap.add_argument("--liquidity-floors", type=float, nargs="+",
                    default=list(DEFAULT_LIQUIDITY_FLOORS), metavar="USD")
    ap.add_argument("--min-ages", type=float, nargs="+",
                    default=list(DEFAULT_MIN_AGES), metavar="DAYS")
    ap.add_argument("--score-thresholds", type=float, nargs="+",
                    default=list(DEFAULT_SCORE_THRESHOLDS), metavar="SCORE")
    ap.add_argument("--top10-caps", type=float, nargs="+",
                    default=list(DEFAULT_TOP10_CAPS), metavar="PCT",
                    help="caps to grid in addition to no concentration gate")
    ap.add_argument("--entry-from", type=parse_boundary,
                    help="start a fresh entry cohort at this UTC date/timestamp")
    ap.add_argument("--entry-before", type=parse_boundary,
                    help="exclusive UTC end of the entry cohort")
    ap.add_argument("--min-mature", type=int, default=20,
                    help="minimum measured+censored entries for console ranking")
    ap.add_argument("--top", type=int, default=20,
                    help="distinct rule cohorts shown per horizon")
    ap.add_argument("--sort", choices=("rug", "median", "mean"), default="rug")
    ap.add_argument("--csv", metavar="PATH",
                    help="write every rule/horizon result to CSV")
    args = ap.parse_args()

    if args.entry_from is not None and args.entry_before is not None \
            and args.entry_from >= args.entry_before:
        ap.error("--entry-from must be earlier than --entry-before")
    if any(h <= 0 for h in args.horizons):
        ap.error("horizons must be positive")
    if any(v < 10_000 for v in args.liquidity_floors):
        ap.error("liquidity floors must be >= $10,000 (the candidate-population gate)")
    if any(v < 0 for v in args.min_ages):
        ap.error("minimum ages cannot be negative")
    if any(v < 0 or v > 100 for v in args.score_thresholds):
        ap.error("score thresholds must be between 0 and 100")
    if any(v < 0 or v > 100 for v in args.top10_caps):
        ap.error("top-10 caps must be between 0 and 100")
    if args.min_mature < 1 or args.top < 1:
        ap.error("--min-mature and --top must be positive integers")

    conn = db.connect()
    try:
        prices, scored, _, _, last_epoch = validate.load(conn)
    finally:
        conn.close()
    entries = build_entries(scored, args.entry_from, args.entry_before)
    if not entries:
        print("no price-valid candidate snapshots in the requested entry window")
        return

    rules = rule_grid(args.liquidity_floors, args.min_ages,
                      args.score_thresholds, args.top10_caps)
    cache = {}
    rows = []
    for rule in rules:
        selected = first_qualifying_entries(entries, rule)
        for horizon in args.horizons:
            rows.append(result_row(
                rule, selected, prices, last_epoch, horizon, cache))

    first_ts = datetime.fromtimestamp(entries[0].epoch, timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    last_ts = datetime.fromtimestamp(entries[-1].epoch, timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"== Filter-rule backtest (score v{scoring.SCORE_VERSION}) ==")
    print(f"entry snapshots: {len(entries):,} across "
          f"{len({e.token for e in entries}):,} assets; {first_ts} -> {last_ts}")
    print(f"grid: {len(rules):,} rules x {len(args.horizons)} horizons; "
          "one first qualifying entry per asset per rule")
    print("rug bounds treat every censored mature outcome as survivor (lower) "
          "or rug (upper)")
    print("WARNING: exploratory multiple-rule search; freeze rules before "
          "prospective validation on fresh data.")

    baseline_rule = Rule(
        min(args.liquidity_floors),
        min(args.min_ages),
        min(args.score_thresholds),
        False,
        None,
        False,
    )
    print_results(rows, args.horizons, baseline_rule, args.top,
                  args.min_mature, args.sort)
    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nwrote {len(rows):,} result rows to {args.csv}")


if __name__ == "__main__":
    main()
