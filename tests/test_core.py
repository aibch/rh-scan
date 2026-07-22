"""Unit tests for the pure logic: scoring, transfer-sim decoding, re-poll
tiers, rug classification, and pick-log idempotency.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_picks
import onchain
import scanner
import scoring
import filter_backtest as fb

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"   # canonical addresses
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def row(**kw):
    base = {
        "base_token": "0xaaa", "base_symbol": "TOK",
        "quote_token": WETH, "quote_symbol": "WETH",
        "price_usd": 1.0, "quote_price_usd": 1800.0,
        "liquidity_usd": 50_000, "vol_h24_usd": 25_000, "vol_liq_ratio": 0.5,
        "buys_h24": 100, "sells_h24": 80, "buyers_h24": 60, "sellers_h24": 50,
        "price_change_h24": 5.0, "pool_created_at": "2026-07-01T00:00:00Z",
        "verified": 1, "top10_pct": 15.0, "transfer_ok": 1,
        "q_verified": None, "q_top10_pct": None, "q_transfer_ok": None,
    }
    base.update(kw)
    return base


class TestAssetSide(unittest.TestCase):
    def test_base_is_asset(self):
        self.assertEqual(scoring.asset_side(row()), "base")

    def test_quote_is_asset_when_base_is_weth(self):
        r = row(base_token=WETH, base_symbol="WETH",
                quote_token="0xccc", quote_symbol="TOK")
        self.assertEqual(scoring.asset_side(r), "quote")

    def test_both_quote_like_is_no_candidate(self):
        r = row(base_token=WETH, base_symbol="WETH",
                quote_token=USDG, quote_symbol="USDG")
        self.assertIsNone(scoring.asset_side(r))

    def test_zero_address_base_is_not_asset(self):
        r = row(base_token=scoring.ZERO_ADDRESS, base_symbol="TOK",
                quote_token="0xccc", quote_symbol="XYZ")
        self.assertEqual(scoring.asset_side(r), "quote")

    def test_symbol_impersonation_does_not_fool_detection(self):
        # a token merely NAMED "WETH"/"USDG" at a non-canonical address is
        # an asset (symbols are user-controlled and already impersonated)
        r = row(base_token="0xfakeweth", base_symbol="WETH")
        self.assertEqual(scoring.asset_side(r), "base")


class TestCandidate(unittest.TestCase):
    def test_scores_healthy_base_token(self):
        c = scoring.candidate(row(), NOW)
        self.assertIsNotNone(c)
        self.assertEqual(c["token"], "0xaaa")
        self.assertGreater(c["score"], 50)

    def test_quote_side_uses_quote_fields(self):
        r = row(base_token=WETH, base_symbol="WETH",
                quote_token="0xccc", quote_symbol="TOK",
                q_verified=1, q_top10_pct=10.0, q_transfer_ok=1)
        c = scoring.candidate(r, NOW)
        self.assertEqual(c["token"], "0xccc")
        self.assertEqual(c["price_usd"], 1800.0)
        self.assertEqual(c["top10_pct"], 10.0)
        self.assertIsNone(c["price_change_h24"])  # base-side metric, not the asset's

    def test_confirmed_transfer_block_is_hard_gate(self):
        self.assertIsNone(scoring.candidate(row(transfer_ok=0), NOW))

    def test_unknown_transfer_result_passes_gate(self):
        self.assertIsNotNone(scoring.candidate(row(transfer_ok=None), NOW))

    def test_market_gates(self):
        self.assertIsNone(scoring.candidate(row(liquidity_usd=500), NOW))
        self.assertIsNone(scoring.candidate(row(sells_h24=0), NOW))

    def test_high_concentration_scores_below_low(self):
        hi = scoring.candidate(row(top10_pct=90.0), NOW)["score"]
        lo = scoring.candidate(row(top10_pct=10.0), NOW)["score"]
        self.assertLess(hi, lo)


class TestSimulateTransfer(unittest.TestCase):
    def _sim(self, resp):
        with mock.patch.object(onchain, "http_json", return_value=resp):
            return onchain.simulate_transfer("key", "0xt", "0xh")

    def test_revert_is_blocked(self):
        self.assertIs(self._sim({"error": {"code": 3, "message": "execution reverted"}}), False)
        self.assertIs(self._sim({"error": {"code": -32000, "message": "execution reverted: paused"}}), False)

    def test_rate_limit_error_is_unknown_not_blocked(self):
        self.assertIsNone(self._sim({"error": {"code": -32005, "message": "rate limit exceeded"}}))

    def test_abi_false_is_blocked(self):
        self.assertIs(self._sim({"result": "0x" + "0" * 64}), False)

    def test_abi_true_is_ok(self):
        self.assertIs(self._sim({"result": "0x" + "0" * 63 + "1"}), True)

    def test_no_return_value_is_ok(self):
        self.assertIs(self._sim({"result": "0x"}), True)

    def test_transport_failure_is_unknown(self):
        self.assertIsNone(self._sim(None))


class TestOnchainRefreshTiers(unittest.TestCase):
    NOW_EPOCH = datetime(2026, 7, 22, 12, tzinfo=timezone.utc).timestamp()

    def rec(self, age_hours, **kw):
        checked = datetime.fromtimestamp(
            self.NOW_EPOCH - age_hours * 3600, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        values = {
            "checked_at": checked, "transfer_version": 2,
            "transfer_ok": True, "had_key": True,
            "sim_incomplete": False,
        }
        values.update(kw)
        return values

    def tier(self, rec, top=False, active=False, has_key=True):
        return onchain.refresh_tier(
            rec, top, active, self.NOW_EPOCH, has_key)

    def test_new_and_incomplete_results_are_immediate(self):
        self.assertEqual(self.tier(None), "new")
        self.assertEqual(self.tier(
            self.rec(0.1, transfer_version=1)), "retry")
        self.assertEqual(self.tier(
            self.rec(0.1, transfer_ok=False)), "retry")
        self.assertEqual(self.tier(
            self.rec(0.1, transfer_ok=None, sim_incomplete=True)), "retry")
        self.assertEqual(self.tier(
            self.rec(0.1, transfer_ok=None, had_key=False), has_key=True),
            "retry")

    def test_top_active_and_longtail_freshness(self):
        self.assertIsNone(self.tier(self.rec(0.5), top=True, active=True))
        self.assertEqual(self.tier(self.rec(1), top=True, active=True),
                         "top-hourly")
        self.assertIsNone(self.tier(self.rec(20), active=True))
        self.assertEqual(self.tier(self.rec(24), active=True), "active-daily")
        self.assertIsNone(self.tier(self.rec(48)))
        self.assertEqual(self.tier(self.rec(73)), "longtail-3d")

    def test_no_key_does_not_hot_loop_an_unavailable_simulation(self):
        rec = self.rec(1, transfer_ok=None, had_key=False)
        self.assertIsNone(self.tier(rec, has_key=False))

    def test_top_candidates_reserve_budget_ahead_of_backlog(self):
        self.assertLess(
            onchain.selection_priority("top-hourly", True),
            onchain.selection_priority("new", False))
        self.assertLess(
            onchain.selection_priority("retry", False),
            onchain.selection_priority("new", False))

    def test_target_plan_prioritizes_top_then_new_and_skips_quote_address(self):
        top, new, active = "0xtop", "0xnew", "0xactive"

        class FakeCursor(list):
            def fetchall(self):
                return self

        class FakeConn:
            def execute(self, _query):
                return FakeCursor([
                    {"tok": new, "liq": 100_000},
                    {"tok": active, "liq": 80_000},
                    {"tok": top, "liq": 50_000},
                    {"tok": WETH, "liq": 1_000_000},
                ])

        cache = {top: self.rec(1), active: self.rec(24)}
        tiers = ({top}, {top, active}, {top: 0, active: 1})
        with mock.patch.object(onchain, "candidate_tiers", return_value=tiers):
            plan = onchain.target_plan(
                FakeConn(), cache, 3, self.NOW_EPOCH, has_alchemy=True)
        self.assertEqual(plan, [
            (top, "top-hourly"),
            (new, "new"),
            (active, "active-daily"),
        ])


class TestRedact(unittest.TestCase):
    def test_strips_query_and_masks_path_key(self):
        out = onchain.redact("https://x.alchemy.com/v2/SECRET123?a=b")
        self.assertNotIn("SECRET123", out)
        self.assertNotIn("a=b", out)


class TestRepoll(unittest.TestCase):
    def test_tiers_and_stalest_first(self):
        now = 1_000_000
        known = {
            "live": (now - 60, 5_000),          # liq>=1k: due immediately
            "mid_fresh": (now - 3600, 500),     # 4h tier, only 1h stale: not due
            "mid_stale": (now - 5 * 3600, 500),  # 4h tier, 5h stale: due
            "dust_stale": (now - 13 * 3600, 5),  # 12h tier, 13h stale: due
            "negative": (now - 13 * 3600, -50),  # broken pool -> dust tier
        }
        due = scanner.due_for_repoll(known, set(), now)
        self.assertNotIn("mid_fresh", due)
        self.assertEqual(set(due), {"live", "mid_stale", "dust_stale", "negative"})
        # stalest first, so a truncated cap rotates instead of starving
        self.assertEqual(due[0], "dust_stale")
        self.assertEqual(due[-1], "live")


class TestPickLogIdempotency(unittest.TestCase):
    def test_already_logged(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            f.write(json.dumps({"scan_ts": "2026-07-12T00:00:00Z",
                                "score_version": 3, "rank": 1}) + "\n")
            path = f.name
        try:
            self.assertTrue(log_picks.already_logged(path, "2026-07-12T00:00:00Z", 3))
            self.assertFalse(log_picks.already_logged(path, "2026-07-12T01:00:00Z", 3))
            self.assertFalse(log_picks.already_logged(path, "2026-07-12T00:00:00Z", 4))
        finally:
            os.unlink(path)


class TestToFloat(unittest.TestCase):
    def test_handles_garbage(self):
        self.assertEqual(scanner.to_float("1.5"), 1.5)
        self.assertIsNone(scanner.to_float(None))
        self.assertIsNone(scanner.to_float("not a number"))




class TestTolerance(unittest.TestCase):
    def test_precommitted_windows(self):
        self.assertEqual(scoring.horizon_tolerance_s(0.5), 3 * 3600)   # 12h -> 3h
        self.assertEqual(scoring.horizon_tolerance_s(1), 6 * 3600)     # 1d -> 6h
        self.assertEqual(scoring.horizon_tolerance_s(7), 12 * 3600)    # 7d -> 12h cap
        self.assertEqual(scoring.horizon_tolerance_s(0.05), 3600)      # floor 1h


class TestMedian(unittest.TestCase):
    def test_even_averages_middles(self):
        self.assertEqual(scoring.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(scoring.median([3, 1]), 2)
        self.assertEqual(scoring.median([7]), 7)
        self.assertIsNone(scoring.median([]))


class TestFilterBacktest(unittest.TestCase):
    def entry(self, **kw):
        values = {
            "epoch": 0.0, "token": "0xt", "pool": "0xp",
            "key": ("0xp", "base"), "score": 65.0,
            "liquidity_usd": 50_000.0, "age_days": 1.0,
            "verified": None, "top10_pct": None, "transfer_ok": None,
        }
        values.update(kw)
        return fb.Entry(**values)

    def rule(self, **kw):
        values = {
            "liquidity_floor": 10_000.0, "min_age_days": 0.0,
            "score_threshold": 0.0, "require_verified": False,
            "top10_max": None, "require_transfer_ok": False,
        }
        values.update(kw)
        return fb.Rule(**values)

    def test_strict_onchain_gates_reject_unknown(self):
        entry = self.entry()
        self.assertTrue(fb.passes_rule(entry, self.rule()))
        self.assertFalse(fb.passes_rule(
            entry, self.rule(require_verified=True)))
        self.assertFalse(fb.passes_rule(
            entry, self.rule(top10_max=30.0)))
        self.assertFalse(fb.passes_rule(
            entry, self.rule(require_transfer_ok=True)))

    def test_first_qualifying_entry_per_token(self):
        entries = [
            self.entry(epoch=0, score=40, liquidity_usd=5_000),
            self.entry(epoch=1, score=70, pool="0xbest", key=("0xbest", "base")),
            self.entry(epoch=1, score=60, pool="0xother", key=("0xother", "base")),
            self.entry(epoch=2, score=90, pool="0xlater", key=("0xlater", "base")),
            self.entry(epoch=1, token="0xu", pool="0xu", key=("0xu", "base")),
        ]
        selected = fb.first_qualifying_entries(entries, self.rule())
        self.assertEqual([e.token for e in selected], ["0xt", "0xu"])
        self.assertEqual(selected[0].pool, "0xbest")

    def test_censored_outcomes_produce_strict_rug_bounds(self):
        day = 86400.0
        entries = [
            self.entry(token="0xr", pool="0xr", key=("0xr", "base")),
            self.entry(token="0xc", pool="0xc", key=("0xc", "base")),
            self.entry(epoch=1.5 * day, token="0xp", pool="0xp",
                       key=("0xp", "base")),
        ]
        prices = {
            ("0xr", "base"): [(0, 1.0, 10_000), (day, 0.05, 10_000)],
            ("0xc", "base"): [(0, 1.0, 10_000)],
            ("0xp", "base"): [(1.5 * day, 1.0, 10_000)],
        }
        result = fb.evaluate_entries(entries, prices, 2 * day, 1)
        self.assertEqual(result["measured"], 1)
        self.assertEqual(result["censored"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["rugs"], 1)
        self.assertEqual(result["rug_rate_lower"], 0.5)
        self.assertEqual(result["rug_rate_upper"], 1.0)
        self.assertAlmostEqual(result["return_median"], -95.0)


class TestRepollRetirement(unittest.TestCase):
    def test_long_missing_pools_are_retired(self):
        now = 20 * 86400
        known = {"ancient": (now - 15 * 86400, 5_000),
                 "recent": (now - 3600, 5_000)}
        due = scanner.due_for_repoll(known, set(), now)
        self.assertNotIn("ancient", due)
        self.assertIn("recent", due)


class TestCandidateSide(unittest.TestCase):
    def test_side_is_reported(self):
        self.assertEqual(scoring.candidate(row(), NOW)["side"], "base")
        r = row(base_token=WETH, base_symbol="WETH",
                quote_token="0xccc", quote_symbol="TOK")
        self.assertEqual(scoring.candidate(r, NOW)["side"], "quote")

    def test_quote_candidate_without_quote_price_has_no_price(self):
        r = row(base_token=WETH, base_symbol="WETH",
                quote_token="0xccc", quote_symbol="TOK", quote_price_usd=None)
        c = scoring.candidate(r, NOW)
        self.assertIsNotNone(c)          # still scoreable...
        self.assertIsNone(c["price_usd"])  # ...but not price-validatable

if __name__ == "__main__":
    unittest.main()


class TestSurges(unittest.TestCase):
    def test_surge_predicate(self):
        import surges
        self.assertTrue(surges.is_surge(50_000, 20_000))     # 2.5x, big volume
        self.assertFalse(surges.is_surge(10_000, 20_000))    # below volume floor
        self.assertFalse(surges.is_surge(50_000, 500))       # dust pool
        self.assertFalse(surges.is_surge(30_000, 20_000))    # only 1.5x liquidity


class TestSpikeWatch(unittest.TestCase):
    def test_alert_predicate_and_cooldown(self):
        import spike_watch as sw
        now = 1_000_000
        self.assertTrue(sw.should_alert(20_000, 30_000, 0, now))
        self.assertFalse(sw.should_alert(20_000, 30_000, now - 60, now))  # cooldown
        self.assertFalse(sw.should_alert(5_000, 30_000, 0, now))          # small vol
        self.assertFalse(sw.should_alert(20_000, 2_000, 0, now))          # dust pool
        self.assertFalse(sw.should_alert(20_000, 100_000, 0, now))        # not violent


class TestChainPulse(unittest.TestCase):
    def test_log2_score_anchors(self):
        import chain_pulse as cp
        self.assertEqual(cp._log2_score(1.0), 50)    # flat
        self.assertEqual(cp._log2_score(2.0), 100)   # doubled
        self.assertEqual(cp._log2_score(0.5), 0)     # halved
        self.assertEqual(cp._log2_score(0), 0)
        self.assertEqual(cp._log2_score(8.0), 100)   # clamped

    def test_weights_sum_to_one(self):
        import chain_pulse as cp
        self.assertAlmostEqual(sum(w for _, w in cp.PULSE_WEIGHTS), 1.0)
