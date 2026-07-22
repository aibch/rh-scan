"""Phase 2 on-chain safety checks (Blockscout + Alchemy RPC).

Per token:
  - top-10 holder concentration (% of supply, excluding pools & burn addresses)
  - contract verified on the explorer?
  - deployer address (rug history accumulates in our own data over time)
  - transfer simulation: can a real top holder actually move tokens?
    (eth_call with a spoofed `from` — catches blacklist/pause honeypots)

Results are cached in data/onchain.json (recheck every RECHECK_DAYS) and
upserted into the token_onchain table for scoring/reporting.

The transfer check needs ALCHEMY_API_KEY in the environment; without it the
check is skipped and stored as null. Blockscout needs no key.

Usage:
    ALCHEMY_API_KEY=... python3 onchain.py [--max 30]
"""

import argparse
import concurrent.futures
import json
import re
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api/v2"
ALCHEMY_URL = "https://robinhood-mainnet.g.alchemy.com/v2/{key}"
CACHE_PATH = os.path.join(db.DATA_DIR, "onchain.json")
RECHECK_DAYS = 3
# keyed access allows 5 req/s (100k per 16h); anonymous access IP-blocks
# quickly, so pace politely without a key
REQUEST_GAP_S = 0.3 if os.environ.get("BLOCKSCOUT_API_KEY", "").strip() else 1.2
# tokens are checked concurrently, but the pacer below keeps the AGGREGATE
# Blockscout rate at 1/REQUEST_GAP_S regardless of worker count
WORKERS = 4 if os.environ.get("BLOCKSCOUT_API_KEY", "").strip() else 2
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

# protocol singletons that custody user liquidity — excluding them from the
# holder list matters especially for Uniswap v4, whose pools are bytes32 ids
# (never matching a holder address) with all tokens held by the PoolManager
# (addresses from Uniswap's Robinhood Chain deployment table)
PROTOCOL_HOLDERS = {
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # Uniswap v4 PoolManager
    "0x58daec3116aae6d93017baaea7749052e8a04fa7",  # Uniswap v4 PositionManager
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
}

HISTORY_PATH = os.path.join(db.DATA_DIR, "onchain_history.jsonl")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) RobinhoodTracker/0.1")


def redact(url):
    """Strip query params and mask path-embedded keys before a URL can end
    up in an exception message or log line."""
    url = url.split("?")[0]
    return re.sub(r"(/v2/)[A-Za-z0-9_-]+", r"\1***", url)


def http_json(url, payload=None):
    body = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                 **({"Content-Type": "application/json"} if body else {})})
    server_errors = 0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (502, 503):
                # an IP-level block never recovers within a run — fail fast
                server_errors += 1
                if server_errors >= 2:
                    raise RuntimeError(f"HTTP {e.code} (blocked?): {redact(url)}")
                time.sleep(5)
                continue
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if attempt == 3:
                raise
            time.sleep(3)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise RuntimeError(f"network failure: {redact(url)}")
            time.sleep(3)
    raise RuntimeError(f"gave up after repeated errors: {redact(url)}")


class Pacer:
    """Thread-safe rate limiter: hands out send slots spaced GAP apart, so
    N workers together never exceed 1/GAP requests per second."""

    def __init__(self, gap):
        self.gap = gap
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_t)
            self.next_t = slot + self.gap
        if slot > now:
            time.sleep(slot - now)


BS_PACER = Pacer(REQUEST_GAP_S)


def bs_get(path):
    # optional API key (free Blockscout account) lifts rate limits and IP
    # blocks — needed for GitHub Actions runners, which Blockscout 503s
    key = os.environ.get("BLOCKSCOUT_API_KEY", "").strip()
    sep = "&" if "?" in path else "?"
    url = f"{BLOCKSCOUT}{path}{sep}apikey={key}" if key else f"{BLOCKSCOUT}{path}"
    BS_PACER.wait()
    return http_json(url)


def explorer_reachable():
    try:
        return bs_get("/stats") is not None
    except Exception:
        return False


def simulate_transfer(alchemy_key, token, holder):
    """eth_call transfer(dead, 1) with from=holder.

    Returns False only on a CONFIRMED block (execution revert, or the token
    returning ABI-encoded false), True on a decoded success, None when the
    result is unknown (RPC/rate-limit errors) — unknown must never be
    conflated with blocked, since False is a hard eligibility gate.

    NOTE: this is a transfer-level check, not a full sell simulation — a
    token can allow transfers while blocking or taxing AMM sells. A real
    quoted swap via the Uniswap Quoter is the planned upgrade.
    """
    dead = "000000000000000000000000000000000000dEaD".lower()
    data = ("0xa9059cbb" + dead.rjust(64, "0")
            + hex(1)[2:].rjust(64, "0"))
    try:
        resp = http_json(ALCHEMY_URL.format(key=alchemy_key), {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"from": holder, "to": token, "data": data}, "latest"]})
    except Exception:
        return None
    if resp is None:
        return None
    err = resp.get("error")
    if err:
        msg = str(err.get("message", "")).lower()
        if err.get("code") == 3 or "revert" in msg:
            return False       # the transfer itself reverted
        return None            # node/rate-limit error — result unknown
    result = (resp.get("result") or "0x").lower()
    if result in ("0x", "0x0"):
        return True            # non-standard ERC-20 with no return value
    try:
        return bool(int(result, 16))   # ABI-encoded bool: 0 = returned false
    except ValueError:
        return None


def check_token(addr, pool_addrs, alchemy_key):
    # transfer_version 2 = decoded results + unknown-on-RPC-error semantics
    rec = {"checked_at": utcnow(), "verified": None, "creator": None,
           "top10_pct": None, "transfer_ok": None, "transfer_version": 2,
           "holders": None, "transfers": None}

    tok = bs_get(f"/tokens/{addr}")
    holders = bs_get(f"/tokens/{addr}/holders")
    counters = bs_get(f"/tokens/{addr}/counters")
    if counters:
        try:
            rec["holders"] = int(counters.get("token_holders_count") or 0) or None
            rec["transfers"] = int(counters.get("transfers_count") or 0) or None
        except (TypeError, ValueError):
            pass
    supply = float(tok["total_supply"]) if tok and tok.get("total_supply") else None
    real_holders = []
    if holders and holders.get("items"):
        for h in holders["items"]:
            haddr = h["address"]["hash"].lower()
            if (haddr in BURN_ADDRESSES or haddr in pool_addrs
                    or haddr in PROTOCOL_HOLDERS):
                continue
            real_holders.append((haddr, float(h["value"])))
    if supply and real_holders:
        rec["top10_pct"] = round(
            sum(v for _, v in real_holders[:10]) / supply * 100, 2)

    sc = bs_get(f"/smart-contracts/{addr}")
    rec["verified"] = bool(sc and sc.get("is_verified"))

    info = bs_get(f"/addresses/{addr}")
    if info and info.get("creator_address_hash"):
        rec["creator"] = info["creator_address_hash"].lower()

    if tok is None and holders is None and sc is None and info is None:
        raise RuntimeError("all explorer lookups failed — not caching")

    rec["had_key"] = bool(alchemy_key)
    if alchemy_key and real_holders:
        rec["transfer_ok"] = simulate_transfer(alchemy_key, addr, real_holders[0][0])
        # a sim that had key + holders but returned unknown is an RPC failure,
        # not a result — flag it retryable instead of fresh for RECHECK_DAYS
        rec["sim_incomplete"] = rec["transfer_ok"] is None

    return rec


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(db.DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def upsert_db(conn, cache):
    for addr, rec in cache.items():
        conn.execute(
            "INSERT INTO token_onchain (address, checked_at, verified, creator, "
            "top10_pct, transfer_ok, holders) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET checked_at=excluded.checked_at, "
            "verified=excluded.verified, creator=excluded.creator, "
            "top10_pct=excluded.top10_pct, transfer_ok=excluded.transfer_ok, "
            "holders=excluded.holders",
            (addr, rec["checked_at"],
             None if rec["verified"] is None else int(rec["verified"]),
             rec["creator"], rec["top10_pct"],
             None if rec["transfer_ok"] is None else int(rec["transfer_ok"]),
             rec.get("holders")))
    conn.commit()


QUOTE_LIKE = {"WETH", "USDG", "USDC", "USDT", "DAI", "WBTC"}
ZERO = "0x0000000000000000000000000000000000000000"


def targets(conn, cache, limit):
    """Asset tokens worth checking, deepest liquidity first — from BOTH sides
    of each pair (the asset is sometimes GeckoTerminal's quote token).
    Skips wrapped/stable quote currencies and the native-ETH placeholder."""
    rows = conn.execute("""
        SELECT tok, sym, MAX(liq) AS liq FROM (
            SELECT p.base_token AS tok, t.symbol AS sym, s.liquidity_usd AS liq
            FROM pools p LEFT JOIN tokens t ON t.address = p.base_token
            JOIN snapshots s ON s.id = (SELECT id FROM snapshots
                WHERE pool_address = p.address ORDER BY ts DESC LIMIT 1)
            UNION ALL
            SELECT p.quote_token, tq.symbol, s.liquidity_usd
            FROM pools p LEFT JOIN tokens tq ON tq.address = p.quote_token
            JOIN snapshots s ON s.id = (SELECT id FROM snapshots
                WHERE pool_address = p.address ORDER BY ts DESC LIMIT 1)
        ) GROUP BY tok HAVING liq >= 5000 ORDER BY liq DESC
    """).fetchall()
    cutoff = datetime.now(timezone.utc).timestamp() - RECHECK_DAYS * 86400
    due = []
    for r in rows:
        if (r["sym"] or "").upper() in QUOTE_LIKE or r["tok"] == ZERO:
            continue
        rec = cache.get(r["tok"])
        if rec:
            checked = datetime.strptime(rec["checked_at"], "%Y-%m-%dT%H:%M:%SZ")
            fresh = checked.replace(tzinfo=timezone.utc).timestamp() > cutoff
            needs_reverify = (
                # pre-decode-fix results are untrusted in BOTH directions:
                # old False could be a rate limit, old True an ABI false
                rec.get("transfer_version", 0) < 2
                # confirmed blocks re-verify each window (tokens un-pause)
                or rec.get("transfer_ok") is False
                # sim recorded without a key: backfill now that one exists
                or (rec.get("transfer_ok") is None and not rec.get("had_key")
                    and bool(os.environ.get("ALCHEMY_API_KEY", "").strip()))
                # RPC failure during the sim: retry next run
                or rec.get("sim_incomplete") is True)
            if fresh and not needs_reverify:
                continue
        due.append(r["tok"])
        if len(due) >= limit:
            break
    return due


def main():
    ap = argparse.ArgumentParser(description="On-chain safety checks")
    ap.add_argument("--max", type=int, default=30, help="max tokens per run")
    ap.add_argument("--token", help="check one specific token address")
    args = ap.parse_args()

    alchemy_key = os.environ.get("ALCHEMY_API_KEY", "").strip()
    if not alchemy_key:
        print("ALCHEMY_API_KEY not set — transfer simulation will be skipped")

    if not explorer_reachable():
        print("explorer unreachable from this network (likely IP-blocked) — "
              "skipping on-chain checks this run")
        return

    conn = db.connect()
    cache = load_cache()
    pool_addrs = {r[0] for r in conn.execute("SELECT address FROM pools")}
    todo = [args.token.lower()] if args.token else targets(conn, cache, args.max)
    print(f"checking {len(todo)} tokens "
          f"({len(cache)} cached, recheck window {RECHECK_DAYS}d)")

    # tokens are checked concurrently (the Pacer keeps the aggregate
    # Blockscout rate unchanged); results are written back here in the main
    # thread so the cache and history file need no locking
    consecutive_failures = 0
    history = open(HISTORY_PATH, "a", encoding="utf-8")
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures = {ex.submit(check_token, addr, pool_addrs, alchemy_key): addr
                   for addr in todo}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            addr = futures[fut]
            try:
                r = cache[addr] = fut.result()
                # append-only history enables as-of joins in validate.py — the
                # latest-only cache would otherwise leak future knowledge into
                # historical cohorts
                history.write(json.dumps({"token": addr, **r},
                                         separators=(",", ":")) + "\n")
                history.flush()
                consecutive_failures = 0
                print(f"  [{i}/{len(todo)}] {addr[:10]} verified={r['verified']} "
                      f"top10={r['top10_pct']}% transfer_ok={r['transfer_ok']}",
                      flush=True)
            except Exception as e:
                consecutive_failures += 1
                print(f"  [{i}/{len(todo)}] {addr[:10]} FAILED: {e}", flush=True)
                if consecutive_failures >= 3:
                    print("explorer unreachable from this network — "
                          "stopping early, will retry next run")
                    break
            if i % 5 == 0:
                save_cache(cache)
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
        history.close()
    save_cache(cache)
    upsert_db(conn, cache)
    conn.close()
    print(f"cache: {len(cache)} tokens -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
