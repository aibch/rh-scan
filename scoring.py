"""Versioned health-score model, shared by the dashboard, pick log, and validator.

The score is a pure function of one snapshot row, so historical scores can be
recomputed from stored data — but published picks are also logged at scan time
(data/picks/) with SCORE_VERSION, so formula changes never rewrite the record
of what the model actually said.

Bump SCORE_VERSION whenever weights, subscores, or eligibility gates change.
"""

import math
from datetime import datetime, timezone

# v2: on-chain safety component + transfer-block hard gate
# v3: asset-side normalization (quote-side tokens scored), transfer gate only
#     on CONFIRMED blocks (decoded), unknown stays neutral
SCORE_VERSION = 3

SCORE_WEIGHTS = [
    ("Liquidity depth", 0.20),
    ("Trading activity", 0.15),
    ("Two-sided flow", 0.12),
    ("Trader breadth", 0.12),
    ("Survival age", 0.12),
    ("Price stability", 0.09),
    ("On-chain safety", 0.20),
]

QUOTE_LIKE = {"WETH", "USDG", "USDC", "USDT", "DAI", "WBTC"}

# canonical quote-currency ADDRESSES on Robinhood Chain — symbols are
# user-controlled and already being impersonated (a fake "USDG" exists), so
# side detection trusts addresses only
QUOTE_ADDRESSES = {
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0x0000000000000000000000000000000000000000",  # native placeholder
}


def horizon_tolerance_s(horizon_days):
    """How late an observation may be and still count for this horizon:
    25% of the horizon, clamped to [1h, 12h]. Precommitted so the measured
    rate can't drift with an arbitrary window (a 12h outcome observed at
    24h is a different quantity)."""
    return max(3600, min(12 * 3600, horizon_days * 86400 * 0.25))


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def median(vals):
    """True median: averages the two middle values for even-sized input."""
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return None
    m = n // 2
    return vals[m] if n % 2 else (vals[m - 1] + vals[m]) / 2


def field(r, key):
    """Row access tolerant of missing on-chain columns (older data paths)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def subscores(r, now):
    """Six 0..1 component scores for one snapshot row (dict-like access)."""
    liq = r["liquidity_usd"] or 0
    s_liq = clamp((math.log10(max(liq, 1)) - math.log10(5e3))
                  / (math.log10(5e5) - math.log10(5e3)))

    v = r["vol_liq_ratio"] or 0
    if v <= 0:
        s_act = 0.0
    elif v < 0.3:          # too quiet
        s_act = clamp(v / 0.3)
    elif v <= 5:           # healthy churn relative to depth
        s_act = 1.0
    else:                  # suspiciously hot: wash-trade / pump territory
        s_act = clamp(1 - (math.log10(v) - math.log10(5)) / 1.5)

    b, s = r["buys_h24"] or 0, r["sells_h24"] or 0
    s_bal = (min(b, s) / max(b, s)) if b and s else 0.0

    traders = (r["buyers_h24"] or 0) + (r["sellers_h24"] or 0)
    s_breadth = clamp((math.log10(max(traders, 1)) - 1) / (math.log10(500) - 1))

    if r["pool_created_at"]:
        age_d = (now - parse_ts(r["pool_created_at"])).total_seconds() / 86400
        s_age = clamp(age_d / 7)
    else:
        s_age = 0.0

    c = r["price_change_h24"]
    if c is None:
        s_mom = 0.5
    elif c <= -50:
        s_mom = 0.0
    elif c < 0:
        s_mom = 0.5 * (1 + c / 50)
    elif c <= 100:
        s_mom = 0.5 + 0.5 * (c / 100)
    else:                  # vertical candles usually round-trip
        s_mom = clamp(1 - (math.log10(c) - 2) / 1.5, 0.3, 1.0)

    # On-chain safety: holder concentration dominates, verification helps.
    # Unchecked tokens sit at a neutral-poor 0.4 so they can't outrank a
    # token that actually passed the checks.
    top10 = field(r, "top10_pct")
    if top10 is None:
        s_safe = 0.4
    else:
        # <=20% of supply in top-10 wallets is healthy; >=80% is a rug lever
        s_conc = clamp((80 - top10) / 60)
        s_safe = 0.75 * s_conc + 0.25 * (1.0 if field(r, "verified") else 0.0)

    return [s_liq, s_act, s_bal, s_breadth, s_age, s_mom, s_safe]


def total(subs):
    return sum(w * s for (_, w), s in zip(SCORE_WEIGHTS, subs)) * 100


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def asset_side(r):
    """Which side of the pair is the asset under evaluation? GeckoTerminal's
    base token is usually the new asset, but pairs like WETH/TOKEN exist —
    evaluating WETH there and ignoring TOKEN misses real assets.
    Detection is by canonical ADDRESS: a token merely named "WETH" is an
    asset (and a suspicious one), not a quote currency."""
    base_q = r["base_token"] in QUOTE_ADDRESSES
    quote_q = field(r, "quote_token") in QUOTE_ADDRESSES
    if not base_q:
        return "base"
    if not quote_q and field(r, "quote_token"):
        return "quote"
    return None  # both sides are quote currencies (WETH/USDG) — not a candidate


def market_eligible(r):
    return ((r["liquidity_usd"] or 0) >= 10_000
            and (r["vol_h24_usd"] or 0) >= 1_000
            and (r["buys_h24"] or 0) >= 5
            and (r["sells_h24"] or 0) >= 5)


def side_adjusted(r):
    """(side, row-as-seen-from-the-asset-side) or (None, None). Quote-side
    views null the base-token price metrics and take the quote token's
    on-chain fields — shared by scoring, display, and decay analysis."""
    side = asset_side(r)
    if side is None:
        return None, None
    r2 = dict(r)
    if side == "quote":
        # pool-level price metrics describe the base token — not the asset
        r2["price_change_h24"] = None
        for k in ("verified", "top10_pct", "transfer_ok", "holders"):
            r2[k] = r2.get("q_" + k)
        r2["asset_symbol"] = field(r, "quote_symbol")
    else:
        r2["asset_symbol"] = r["base_symbol"]
    return side, r2


def candidate(r, now):
    """Score one pool row from the perspective of its asset side.
    Returns None if ineligible, else {token, symbol, price_usd, subs, score, r}."""
    side, r2 = side_adjusted(r)
    if side is None or not market_eligible(r):
        return None
    if side == "quote":
        token, symbol = r["quote_token"], field(r, "quote_symbol")
        price = r2.get("quote_price_usd")
    else:
        token, symbol, price = r["base_token"], r["base_symbol"], r["price_usd"]
    # hard gate: a CONFIRMED failed transfer simulation means holders can't
    # move tokens — no score can compensate. None (unknown) passes.
    if r2.get("transfer_ok") == 0:
        return None
    subs = subscores(r2, now)
    return {"token": token, "symbol": symbol, "price_usd": price, "side": side,
            "verified": r2.get("verified"), "top10_pct": r2.get("top10_pct"),
            "transfer_ok": r2.get("transfer_ok"), "holders": r2.get("holders"),
            "price_change_h24": r2.get("price_change_h24"),
            "subs": subs, "score": total(subs), "r": r}


def eligible(r):
    """Back-compat: is this row a candidate from its asset side?"""
    return candidate(r, datetime.now(timezone.utc)) is not None


def ranked_candidates(rows, now):
    """Score eligible rows, keep the best pool per asset token, rank descending."""
    best = {}
    for r in rows:
        c = candidate(r, now)
        if c is None:
            continue
        key = c["token"]
        if key not in best or best[key]["score"] < c["score"]:
            best[key] = c
    return sorted(best.values(), key=lambda c: c["score"], reverse=True)
