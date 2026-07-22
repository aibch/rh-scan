"""Robinhood Chain token scanner (Stage 1).

Polls the free GeckoTerminal API (no key needed) for every pool it can see on
Robinhood Chain — both the newest pools and the top pools by volume — and logs
a time-stamped snapshot of each.

Two storage modes:
  default  -> SQLite (data/scanner.db) — use this on a VPS / always-on machine
  --jsonl  -> append-only data/snapshots/YYYY-MM-DD.jsonl — used by the GitHub
              Actions deployment, where the repo itself is the data store.
              Rebuild the SQLite DB from JSONL with: python3 build_db.py

Usage:
    python scanner.py --once            # single scan cycle into SQLite
    python scanner.py --once --jsonl    # single scan cycle into JSONL
    python scanner.py --loop 300        # scan every 300s forever
"""

import argparse
import concurrent.futures
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

NETWORK = "robinhood"
API_BASE = "https://api.geckoterminal.com/api/v2"
NEW_POOL_PAGES = 3    # 20 pools per page; ~10 launches/hour on this chain
TOP_POOL_PAGES = 6    # top 120 by volume; the re-poll sweep covers the rest
REQUEST_GAP_S = 2.5   # stay under the free tier's 30 calls/min

SNAPSHOT_DIR = os.path.join(db.DATA_DIR, "snapshots")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_next_send = 0.0
_pace_lock = threading.Lock()
FETCH_WORKERS = 4  # overlaps response latency only; the pacer sets the rate


def pace():
    """Space request STARTS at least REQUEST_GAP_S apart (24/min, under the
    free tier's 30/min), across all fetch threads. Pacing send times instead
    of sleeping after each response lets slow responses overlap the gap
    rather than add to it."""
    global _next_send
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_send)
        _next_send = slot + REQUEST_GAP_S
    if slot > now:
        time.sleep(slot - now)


def penalize(seconds):
    """A 429 means the shared budget is exhausted — delay ALL threads' next
    sends, not just the one that saw it."""
    global _next_send
    with _pace_lock:
        _next_send = max(_next_send, time.monotonic() + seconds)


def fetch_many(paths, stats):
    """api_get each path on a small thread pool; results come back in the
    given order, with failures as None. Request starts stay strictly paced."""
    stats["requests"] += len(paths)
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        for fut in [ex.submit(api_get, p) for p in paths]:
            try:
                out.append(fut.result())
            except Exception:
                out.append(None)
    return out


def api_get(path):
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "robinhood-tracker/0.1"})
    for attempt in range(3):
        pace()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited — back off and retry
                try:
                    retry_after = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                penalize(max(20 * (attempt + 1), retry_after))
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return None


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


MAX_REPOLL_BATCHES = 20  # 30 addresses per batch

# Re-poll cadence for pools that fell off the listings, tiered by their last
# known liquidity: dead pools still get tracked (forward returns and rug-rate
# need them) but not on every cycle, or the call count grows without bound.
REPOLL_TIERS = [
    (1_000, 0),           # liq >= $1k: every cycle
    (100, 4 * 3600),      # $100..$1k: every 4h
    (0, 12 * 3600),       # dust: every 12h
]
RETIRE_AFTER_S = 14 * 86400  # unseen this long -> stop re-polling entirely


def known_pools(jsonl):
    """pool -> (last_seen_epoch, last_liquidity) for every pool ever recorded,
    so dead pools keep being tracked after they fall off the listings
    (otherwise the dataset only contains survivors and validation is biased)."""
    known = {}
    if jsonl:
        import glob
        paths = glob.glob(os.path.join(SNAPSHOT_DIR, "*.jsonl")) + \
            glob.glob(os.path.join(SNAPSHOT_DIR, "*.jsonl.gz"))
        for path in sorted(paths):
            with open_snapshot(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        e = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") \
                            .replace(tzinfo=timezone.utc).timestamp()
                        known[r["pool"]] = (e, r.get("liquidity_usd") or 0)
                    except (ValueError, KeyError):
                        continue
    elif os.path.exists(db.DB_PATH):
        conn = db.connect()
        try:
            for addr, ts, liq in conn.execute(
                    "SELECT pool_address, MAX(ts), liquidity_usd "
                    "FROM snapshots GROUP BY pool_address"):
                e = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") \
                    .replace(tzinfo=timezone.utc).timestamp()
                known[addr] = (e, liq or 0)
        finally:
            conn.close()
    return known


def due_for_repoll(known, seen, now_epoch):
    due = []
    for pool, (last_e, liq) in known.items():
        if pool in seen:
            continue
        if now_epoch - last_e > RETIRE_AFTER_S:
            continue  # retired: permanently missing pools can't hog the cap
        # negative liquidity happens on broken pools — treat as dust tier
        min_gap = next((gap for floor, gap in REPOLL_TIERS if liq >= floor),
                       REPOLL_TIERS[-1][1])
        if now_epoch - last_e >= min_gap:
            due.append((last_e, pool))
    # stalest first: if the backlog exceeds the batch cap, truncation drops
    # the most recently seen pools, not a fixed alphabetical tail forever
    return [pool for _, pool in sorted(due)]


def iter_listing(data, seen):
    tokens = {t["id"]: t["attributes"] for t in data.get("included", [])
              if t.get("type") == "token"}
    for pool in data["data"]:
        addr = pool["attributes"]["address"].lower()
        if addr in seen:
            continue
        seen.add(addr)
        yield pool, tokens


def open_snapshot(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def fetch_pools(jsonl=False, stats=None):
    """Yield (pool_dict, tokens_by_id) for new pools, top pools, and every
    previously seen pool that no longer shows up in those listings.
    Failed requests are counted in stats — a scan with failures is PARTIAL,
    which downstream should know about rather than mistake for a quiet chain."""
    seen = set()
    stats = stats if stats is not None else {}
    stats.setdefault("requests", 0)
    stats.setdefault("failed", 0)
    endpoints = (
        [f"/networks/{NETWORK}/new_pools?page={p}&include=base_token,quote_token"
         for p in range(1, NEW_POOL_PAGES + 1)] +
        [f"/networks/{NETWORK}/pools?page={p}&include=base_token,quote_token"
         for p in range(1, TOP_POOL_PAGES + 1)]
    )
    for data in fetch_many(endpoints, stats):
        if data is None:
            stats["failed"] += 1
            continue
        if not data.get("data"):
            continue
        yield from iter_listing(data, seen)

    now_epoch = datetime.now(timezone.utc).timestamp()
    missing = due_for_repoll(known_pools(jsonl), seen, now_epoch)
    cap = MAX_REPOLL_BATCHES * 30
    if len(missing) > cap:
        # rotate the starting index each hour so a fixed oldest cohort can't
        # monopolize the cap while everything behind it starves
        offset = (int(now_epoch // 3600) * cap) % len(missing)
        missing = (missing[offset:] + missing[:offset])[:cap]
        print(f"re-poll backlog exceeds cap, rotating window "
              f"(offset {offset})", flush=True)
    batch_paths = [
        f"/networks/{NETWORK}/pools/multi/{','.join(missing[i:i + 30])}"
        f"?include=base_token,quote_token"
        for i in range(0, len(missing), 30)]
    for data in fetch_many(batch_paths, stats):
        if data is None:
            stats["failed"] += 1
            continue
        if data.get("data"):
            yield from iter_listing(data, seen)


def pool_row(pool, tokens, ts):
    """Flatten one API pool object into a plain snapshot row."""
    attrs = pool["attributes"]
    rel = pool["relationships"]
    base_id = rel["base_token"]["data"]["id"]
    quote_id = rel["quote_token"]["data"]["id"]
    base = tokens.get(base_id, {})
    quote = tokens.get(quote_id, {})
    tx = (attrs.get("transactions") or {}).get("h24") or {}
    return {
        "ts": ts,
        "pool": attrs["address"].lower(),
        "pool_name": attrs.get("name"),
        "dex": rel["dex"]["data"]["id"],
        "pool_created_at": attrs.get("pool_created_at"),
        "base_token": (base.get("address") or base_id.split("_", 1)[-1]).lower(),
        "base_symbol": base.get("symbol"),
        "base_name": base.get("name"),
        "quote_token": (quote.get("address") or quote_id.split("_", 1)[-1]).lower(),
        "quote_symbol": quote.get("symbol"),
        "price_usd": to_float(attrs.get("base_token_price_usd")),
        "quote_price_usd": to_float(attrs.get("quote_token_price_usd")),
        "liquidity_usd": to_float(attrs.get("reserve_in_usd")),
        "fdv_usd": to_float(attrs.get("fdv_usd")),
        "market_cap_usd": to_float(attrs.get("market_cap_usd")),
        "vol_h24_usd": to_float((attrs.get("volume_usd") or {}).get("h24")),
        "buys_h24": to_int(tx.get("buys")),
        "sells_h24": to_int(tx.get("sells")),
        "buyers_h24": to_int(tx.get("buyers")),
        "sellers_h24": to_int(tx.get("sellers")),
        "price_change_h24": to_float((attrs.get("price_change_percentage") or {}).get("h24")),
    }


def write_rows_db(conn, rows):
    for r in rows:
        conn.execute(
            "INSERT INTO tokens (address, symbol, name, first_seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET symbol=COALESCE(excluded.symbol, symbol), "
            "name=COALESCE(excluded.name, name)",
            (r["base_token"], r["base_symbol"], r["base_name"], r["ts"]))
        conn.execute(
            "INSERT INTO tokens (address, symbol, name, first_seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET symbol=COALESCE(excluded.symbol, symbol), "
            "name=COALESCE(excluded.name, name)",
            (r["quote_token"], r["quote_symbol"], None, r["ts"]))
        conn.execute(
            "INSERT INTO pools (address, base_token, quote_token, dex, name, pool_created_at, first_seen_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(address) DO NOTHING",
            (r["pool"], r["base_token"], r["quote_token"], r["dex"],
             r["pool_name"], r["pool_created_at"], r["ts"]))
        liq, vol = r["liquidity_usd"], r["vol_h24_usd"]
        conn.execute(
            "INSERT OR IGNORE INTO snapshots (ts, pool_address, price_usd, quote_price_usd, "
            "liquidity_usd, fdv_usd, market_cap_usd, vol_h24_usd, buys_h24, sells_h24, "
            "buyers_h24, sellers_h24, vol_liq_ratio, price_change_h24) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["ts"], r["pool"], r["price_usd"], r.get("quote_price_usd"),
             liq, r["fdv_usd"], r["market_cap_usd"],
             vol, r["buys_h24"], r["sells_h24"], r["buyers_h24"], r["sellers_h24"],
             (vol / liq) if (vol is not None and liq) else None, r["price_change_h24"]))


def write_rows_jsonl(rows, ts):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{ts[:10]}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return path


def scan_once(jsonl=False):
    ts = utcnow()
    stats = {}
    rows = [pool_row(pool, tokens, ts)
            for pool, tokens in fetch_pools(jsonl, stats)]
    partial = (f" — PARTIAL: {stats['failed']}/{stats['requests']} requests failed"
               if stats.get("failed") else "")
    if jsonl:
        path = write_rows_jsonl(rows, ts)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"_meta": "scan", "ts": ts,
                                "requests": stats.get("requests", 0),
                                "failed": stats.get("failed", 0)},
                               separators=(",", ":")) + "\n")
        print(f"[{ts}] scanned {len(rows)} pools -> {path}{partial}", flush=True)
    else:
        conn = db.connect()
        try:
            write_rows_db(conn, rows)
            conn.execute("INSERT OR REPLACE INTO scan_meta (ts, requests, failed) "
                         "VALUES (?,?,?)",
                         (ts, stats.get("requests", 0), stats.get("failed", 0)))
            conn.commit()
        finally:
            conn.close()
        print(f"[{ts}] scanned {len(rows)} pools -> {db.DB_PATH}{partial}", flush=True)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Robinhood Chain pool scanner")
    ap.add_argument("--once", action="store_true", help="run a single scan cycle")
    ap.add_argument("--jsonl", action="store_true",
                    help="append to data/snapshots/*.jsonl instead of SQLite")
    ap.add_argument("--loop", type=int, nargs="?", const=300, default=None,
                    metavar="SECONDS", help="scan repeatedly every SECONDS (default 300)")
    args = ap.parse_args()

    if args.once or args.loop is None:
        scan_once(jsonl=args.jsonl)
        return

    interval = max(60, args.loop)
    print(f"scanning every {interval}s — Ctrl-C to stop", flush=True)
    while True:
        started = time.time()
        try:
            scan_once(jsonl=args.jsonl)
        except Exception as e:  # keep the loop alive through transient failures
            print(f"[{utcnow()}] scan failed: {e}", file=sys.stderr, flush=True)
        time.sleep(max(5, interval - (time.time() - started)))


if __name__ == "__main__":
    main()
