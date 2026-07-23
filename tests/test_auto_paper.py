"""Focused tests for the derived automatic Top-10 paper strategy."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_paper
import db
import log_picks
import scoring


TOKEN_A = "0xaa00000000000000000000000000000000000001"
TOKEN_B = "0xbb00000000000000000000000000000000000002"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
T0 = "2026-01-01T00:00:00Z"


class AutoPaperCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.picks = Path(self.temp.name) / "picks"
        self.picks.mkdir()
        self.pick_file = self.picks / "2026-01-01.jsonl"
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(db.SCHEMA)
        for address, symbol in ((TOKEN_A, "AAA"), (TOKEN_B, "BBB"), (WETH, "WETH")):
            self.conn.execute(
                "INSERT INTO tokens(address,symbol,name,first_seen_at) VALUES(?,?,?,?)",
                (address, symbol, symbol, T0),
            )

    def pool(self, address, base, quote):
        self.conn.execute(
            """INSERT INTO pools(address,base_token,quote_token,dex,name,pool_created_at,first_seen_at)
               VALUES(?,?,?,?,?,?,?)""",
            (address, base, quote, "test", address, T0, T0),
        )
        self.conn.commit()

    def snapshot(self, pool, ts, base_price, quote_price, liquidity=10_000):
        self.conn.execute(
            """INSERT INTO snapshots(ts,pool_address,price_usd,quote_price_usd,liquidity_usd)
               VALUES(?,?,?,?,?)""",
            (ts, pool, base_price, quote_price, liquidity),
        )
        self.conn.commit()

    def record(
        self,
        *,
        token=TOKEN_A,
        pool="pool-a",
        rank=1,
        score=80,
        price=10,
        liquidity=10_000,
        prospective=False,
        side=None,
        scan_ts=T0,
        logged_at=None,
        version=scoring.SCORE_VERSION,
    ):
        rec = {
            "scan_ts": scan_ts,
            "score_version": version,
            "rank": rank,
            "score": score,
            "pool": pool,
            "token": token,
            "symbol": "AAA" if token == TOKEN_A else "BBB",
            "price_usd": price,
            "liquidity_usd": liquidity,
            "vol_h24_usd": 20_000,
        }
        if prospective:
            rec.update(auto_paper.signal_metadata(
                score_version=version,
                scan_ts=scan_ts,
                rank=rank,
                token=token,
                pool=pool,
                side=side,
                logged_at=logged_at or scan_ts,
            ))
        return rec

    def write(self, *records):
        with self.pick_file.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")


class TestSignalBooks(AutoPaperCase):
    def test_targeted_pick_with_missing_pool_fails_closed(self):
        self.write(self.record(pool="missing-pool"))
        with self.assertRaisesRegex(auto_paper.StrategyDataError, "absent from scanner data"):
            auto_paper.build_strategy(self.conn, self.picks, now=T0)

    def test_base_historical_and_quote_prospective_are_separate(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.pool("pool-b", WETH, TOKEN_B)
        self.snapshot("pool-a", T0, 10, 1800)
        self.snapshot("pool-a", "2026-01-02T00:00:00Z", 15, 1800)
        self.snapshot("pool-b", T0, 1800, 2)
        self.snapshot("pool-b", "2026-01-01T01:00:00Z", 1800, 3)
        self.snapshot("pool-b", "2026-01-01T06:00:00Z", 1800, 4)
        self.write(
            self.record(),
            self.record(token=TOKEN_B, pool="pool-b", price=2,
                        prospective=True, side="quote", rank=2,
                        logged_at="2026-01-01T00:05:00Z"),
        )

        result = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T12:00:00Z"
        )
        self.assertEqual(result["historical"]["summary"]["entry_count"], 1)
        self.assertEqual(result["prospective"]["summary"]["entry_count"], 1)
        historical = result["historical"]["entries"][0]
        prospective = result["prospective"]["entries"][0]
        self.assertEqual(historical["side"], "base")
        self.assertIsNone(historical["strategy_id"])
        self.assertEqual(prospective["side"], "quote")
        self.assertEqual(prospective["strategy_id"], result["strategy_id"])
        self.assertEqual(prospective["status"], "pending")
        self.assertEqual(prospective["decision_ts"], "2026-01-01T00:05:00Z")
        self.assertEqual(prospective["entry_ts"], "2026-01-01T01:00:00Z")
        self.assertEqual(prospective["signal_price_usd"], 2.0)
        self.assertEqual(prospective["entry_price_usd"], 3.0)
        self.assertEqual(prospective["mark_price_usd"], 4.0)
        self.assertAlmostEqual(prospective["marked_pnl_usd"], 1 / 3)

    def test_exact_duplicate_dedupes_and_conflicting_id_fails_closed(self):
        self.pool("pool-a", TOKEN_A, WETH)
        record = self.record(prospective=True, side="base")
        retry = {**record, "logged_at": "2026-01-01T00:01:00Z"}
        self.write(record, retry)
        result = auto_paper.build_strategy(self.conn, self.picks, now=T0)
        self.assertEqual(result["prospective"]["summary"]["entry_count"], 1)
        self.assertEqual(result["prospective"]["entries"][0]["logged_at"], T0)

        conflict = {**record, "price_usd": 11}
        self.write(record, conflict)
        with self.assertRaisesRegex(auto_paper.StrategyDataError, "conflicting duplicate"):
            auto_paper.build_strategy(self.conn, self.picks, now=T0)

    def test_prospective_waits_for_first_valid_post_signal_price(self):
        self.pool("pool-b", WETH, TOKEN_B)
        # The scan quote exists before the signal is available. A null quote
        # after logging is skipped; the later valid quote becomes the fill.
        self.snapshot("pool-b", T0, 1800, 2, 10_000)
        self.snapshot("pool-b", "2026-01-01T01:00:00Z", 1800, None, 9_000)
        record = self.record(
            token=TOKEN_B,
            pool="pool-b",
            price=2,
            prospective=True,
            side="quote",
            logged_at="2026-01-01T00:30:00Z",
        )
        self.write(record)

        awaiting = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T01:30:00Z"
        )["prospective"]
        row = awaiting["entries"][0]
        self.assertEqual(row["status"], "awaiting_fill")
        self.assertIsNone(row["entry_ts"])
        self.assertEqual(awaiting["summary"]["awaiting_fill_entries"], 1)
        self.assertEqual(awaiting["summary"]["deployed_notional_usd"], 0.0)

        self.snapshot("pool-b", "2026-01-01T02:00:00Z", 1800, 3, 8_000)
        filled = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T02:00:00Z"
        )["prospective"]
        row = filled["entries"][0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["entry_ts"], "2026-01-01T02:00:00Z")
        self.assertEqual(row["entry_price_usd"], 3.0)
        self.assertEqual(row["entry_liquidity_usd"], 8_000.0)
        self.assertEqual(row["exit_target_ts"], "2026-01-02T02:00:00Z")
        self.assertIsNone(row["mark_price_usd"])
        self.assertIsNone(row["marked_value_usd"])
        self.assertIsNone(filled["summary"]["known_pnl_usd"])
        self.assertEqual(filled["summary"]["price_coverage_pct"], 0.0)
        self.assertEqual(filled["summary"]["deployed_notional_usd"], 1.0)

        self.snapshot("pool-b", "2026-01-01T03:00:00Z", 1800, 3.3, 7_500)
        marked = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T06:00:00Z"
        )["prospective"]
        row = marked["entries"][0]
        self.assertEqual(row["mark_price_usd"], 3.3)
        self.assertEqual(row["mark_age_hours"], 3.0)
        self.assertTrue(row["stale_mark"])
        self.assertAlmostEqual(row["marked_value_usd"], 1.1)
        self.assertEqual(marked["summary"]["stale_pending_entries"], 1)
        self.assertEqual(marked["summary"]["fresh_marked_pending_entries"], 0)
        self.assertEqual(marked["summary"]["fresh_price_coverage_pct"], 0.0)

    def test_prospective_signal_expires_after_fill_window(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", T0, 10, 1800, 10_000)
        self.write(self.record(
            prospective=True,
            side="base",
            logged_at="2026-01-01T00:30:00Z",
        ))
        book = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T02:30:00Z"
        )["prospective"]
        row = book["entries"][0]
        self.assertEqual(row["status"], "missed_fill")
        self.assertEqual(row["fill_deadline_ts"], "2026-01-01T02:30:00Z")
        self.assertIsNone(row["entry_ts"])
        self.assertEqual(book["summary"]["missed_fill_entries"], 1)
        self.assertEqual(book["summary"]["deployed_notional_usd"], 0.0)

    def test_invalid_historical_price_is_unpriced_not_fatal(self):
        self.pool("pool-b", WETH, TOKEN_B)
        self.write(self.record(token=TOKEN_B, pool="pool-b", price=None))
        book = auto_paper.build_strategy(self.conn, self.picks, now=T0)["historical"]
        self.assertEqual(book["entries"][0]["status"], "unpriced")
        self.assertEqual(book["summary"]["unpriced_entries"], 1)
        self.assertEqual(book["summary"]["deployed_notional_usd"], 0.0)

    def test_combined_prospective_keeps_all_stamped_score_versions(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-01T01:00:00Z", 11, 1800)
        v2 = self.record(
            prospective=True,
            side="base",
            version=2,
            logged_at="2026-01-01T00:10:00Z",
        )
        v3 = self.record(
            prospective=True,
            side="base",
            version=3,
            logged_at="2026-01-01T00:20:00Z",
        )
        historical_v3 = self.record(rank=2, version=3)
        self.write(v2, v3, historical_v3)

        payload = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-01T02:00:00Z", score_version=3
        )
        live = payload["prospective"]
        self.assertEqual(live["summary"]["entry_count"], 2)
        self.assertEqual(live["score_versions"], [2, 3])
        self.assertEqual(
            live["strategy_ids"],
            [auto_paper.strategy_id(2), auto_paper.strategy_id(3)],
        )
        self.assertEqual(len({row["entry_id"] for row in live["entries"]}), 2)
        self.assertEqual(len(live["segments"]), 2)
        self.assertEqual(payload["historical"]["summary"]["entry_count"], 1)


class TestOutcomes(AutoPaperCase):
    def test_fixed_24h_exit_and_observational_marks(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", T0, 10, 1800)
        self.snapshot("pool-a", "2026-01-01T01:00:00Z", 11, 1800)
        self.snapshot("pool-a", "2026-01-01T06:00:00Z", 12, 1800)
        self.snapshot("pool-a", "2026-01-02T01:00:00Z", 15, 1800)
        self.snapshot("pool-a", "2026-01-04T00:00:00Z", 8, 1800)
        self.snapshot("pool-a", "2026-01-08T00:00:00Z", 20, 1800)
        self.write(self.record())

        result = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-08T01:00:00Z"
        )
        entry = result["historical"]["entries"][0]
        self.assertEqual(entry["status"], "realized")
        self.assertEqual(entry["exit_ts"], "2026-01-02T01:00:00Z")
        self.assertEqual(entry["exit_return_pct"], 50.0)
        self.assertAlmostEqual(entry["realized_pnl_usd"], 0.5)
        self.assertAlmostEqual(entry["marks"]["1h"]["return_pct"], 10.0)
        self.assertAlmostEqual(entry["marks"]["6h"]["return_pct"], 20.0)
        self.assertAlmostEqual(entry["marks"]["72h"]["return_pct"], -20.0)
        self.assertAlmostEqual(entry["marks"]["168h"]["return_pct"], 100.0)

    def test_missing_exit_is_censored_but_drain_is_absorbing_rug(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.pool("pool-b", TOKEN_B, WETH)
        self.snapshot("pool-a", T0, 10, 1800)
        self.snapshot("pool-a", "2026-01-01T02:00:00Z", 11, 1800, 10_000)
        self.snapshot("pool-b", T0, 5, 1800)
        self.snapshot("pool-b", "2026-01-01T23:00:00Z", 100, 1800, 100)
        self.write(
            self.record(),
            self.record(token=TOKEN_B, pool="pool-b", price=5, rank=2),
        )

        result = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-03T00:00:00Z"
        )
        entries = {entry["token"]: entry for entry in result["historical"]["entries"]}
        self.assertEqual(entries[TOKEN_A]["status"], "censored")
        self.assertIsNone(entries[TOKEN_A]["exit_return_pct"])
        self.assertEqual(entries[TOKEN_A]["marks"]["6h"]["status"], "censored")
        self.assertEqual(entries[TOKEN_B]["status"], "realized")
        self.assertEqual(entries[TOKEN_B]["exit_return_pct"], -99.9)
        self.assertEqual(entries[TOKEN_B]["marks"]["24h"] if "24h" in entries[TOKEN_B]["marks"] else "absent", "absent")

        summary = result["historical"]["summary"]
        self.assertEqual(summary["realized_entries"], 1)
        self.assertEqual(summary["censored_entries"], 1)
        self.assertAlmostEqual(summary["realized_pnl_usd"], -0.999)
        self.assertEqual(summary["rug_entries"], 1)
        self.assertEqual(summary["price_coverage_pct"], 50.0)
        self.assertEqual(summary["matured_entries"], 2)
        self.assertEqual(summary["observed_outcomes"], 1)
        self.assertEqual(summary["win_rate_observed_pct"], 0.0)
        self.assertEqual(summary["win_rate_lower_bound_pct"], 0.0)
        self.assertEqual(summary["win_rate_upper_bound_pct"], 50.0)

    def test_horizon_skips_null_quote_before_valid_quote_in_window(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", T0, 10, 1800)
        self.snapshot("pool-a", "2026-01-02T00:00:00Z", None, 1800)
        self.snapshot("pool-a", "2026-01-02T00:30:00Z", 12, 1800)
        self.write(self.record())

        entry = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-02T02:00:00Z"
        )["historical"]["entries"][0]
        self.assertEqual(entry["status"], "realized")
        self.assertEqual(entry["exit_ts"], "2026-01-02T00:30:00Z")
        self.assertEqual(entry["exit_return_pct"], 20.0)
        self.assertEqual(entry["exit_observation_delay_hours"], 0.5)
        self.assertEqual(
            entry["exit_observation_window_end_ts"],
            "2026-01-02T06:00:00Z",
        )

    def test_drain_is_absorbing_even_if_price_and_liquidity_recover(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", T0, 10, 1800, 10_000)
        self.snapshot("pool-a", "2026-01-01T01:00:00Z", 11, 1800, 10_000)
        self.snapshot("pool-a", "2026-01-01T06:00:00Z", 1, 1800, 100)
        self.snapshot("pool-a", "2026-01-02T00:00:00Z", 20, 1800, 10_000)
        self.write(self.record())

        entry = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-02T01:00:00Z"
        )["historical"]["entries"][0]
        self.assertEqual(entry["status"], "realized")
        self.assertEqual(entry["exit_ts"], "2026-01-01T06:00:00Z")
        self.assertEqual(entry["exit_return_pct"], -99.9)
        self.assertEqual(entry["marks"]["1h"]["return_pct"], 10.0)
        self.assertEqual(entry["marks"]["6h"]["status"], "rug")

    def test_summary_rank_score_and_trend_reconcile_entry_rows(self):
        self.pool("pool-a", TOKEN_A, WETH)
        self.pool("pool-b", TOKEN_B, WETH)
        self.snapshot("pool-a", "2026-01-02T00:00:00Z", 12, 1800)
        self.snapshot("pool-b", "2026-01-02T00:00:00Z", 4, 1800)
        self.write(
            self.record(score=85, rank=1),
            self.record(token=TOKEN_B, pool="pool-b", price=5, score=65, rank=2),
        )
        result = auto_paper.build_strategy(
            self.conn, self.picks, now="2026-01-03T00:00:00Z"
        )["historical"]
        rows = result["entries"]
        self.assertEqual(result["summary"]["realized_entries"], 2)
        self.assertAlmostEqual(
            result["summary"]["realized_pnl_usd"],
            sum(row["realized_pnl_usd"] for row in rows),
        )
        self.assertEqual([row["rank"] for row in result["rank_stats"]], [1, 2])
        self.assertEqual([row["band"] for row in result["score_stats"]], ["80-100", "60-79"])
        trend = result["realized_trend"][-1]
        self.assertAlmostEqual(trend["cumulative_pnl_usd"], result["summary"]["realized_pnl_usd"])
        self.assertEqual(trend["cumulative_entries"], 2)


class TestLogPicksMetadata(unittest.TestCase):
    def test_unpriceable_quote_side_is_removed_before_strategy_ranking(self):
        invalid = {"price_usd": None, "score": 99}
        valid = {"price_usd": 2.5, "score": 80}
        with mock.patch.object(
            scoring, "ranked_candidates", return_value=[invalid, valid]
        ):
            ranked = log_picks.ranked_tradeable_candidates([], None)
        self.assertEqual(ranked, [valid])

    def test_only_complete_full_top10_scan_is_strategy_eligible(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute(
            "INSERT INTO scan_meta(ts, requests, failed) VALUES(?,?,?)",
            (T0, 20, 0),
        )
        self.assertTrue(log_picks.complete_strategy_scan(conn, T0, 10))
        self.assertFalse(log_picks.complete_strategy_scan(conn, T0, 9))
        small = log_picks.strategy_scan_gate(conn, T0, 9)
        self.assertEqual(small["reason"], "fewer_than_10_priceable_candidates")
        conn.execute("UPDATE scan_meta SET failed = 1 WHERE ts = ?", (T0,))
        self.assertFalse(log_picks.complete_strategy_scan(conn, T0, 10))
        partial = log_picks.strategy_scan_gate(conn, T0, 10)
        self.assertEqual(partial["reason"], "partial_scan")
        self.assertEqual(partial["failed_requests"], 1)
        self.assertFalse(
            log_picks.complete_strategy_scan(
                conn, "2026-01-01T01:00:00Z", 10
            )
        )
        missing = log_picks.strategy_scan_gate(
            conn, "2026-01-01T01:00:00Z", 10
        )
        self.assertEqual(missing["reason"], "missing_scan_metadata")

    def test_public_scan_manifest_is_idempotent_capture_denominator(self):
        gate = {
            "eligible": True,
            "complete_scan": True,
            "requests": 29,
            "failed_requests": 0,
            "reason": "stamped",
        }
        manifest = log_picks.make_strategy_scan_manifest(
            T0,
            ranked_count=14,
            tradeable_count=13,
            eligible_cohort_size=10,
            stamped_entry_count=10,
            gate=gate,
            recorded_at="2026-01-01T00:05:00Z",
        )
        self.assertEqual(manifest["_meta"], auto_paper.SCAN_MANIFEST_TYPE)
        self.assertEqual(manifest["stamped_entry_count"], 10)
        capture = auto_paper._capture_summary([manifest, dict(manifest)])
        self.assertEqual(capture["attempted_scans"], 1)
        self.assertEqual(capture["stamped_scans"], 1)
        self.assertEqual(capture["capture_rate_pct"], 100.0)
        self.assertEqual(capture["reason_counts"], {"stamped": 1})

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "2026-01-01.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest) + "\n")
            self.assertTrue(
                log_picks.already_logged(path, T0, scoring.SCORE_VERSION)
            )

    def test_capture_accepts_older_strategy_versions(self):
        manifest = {
            "_meta": auto_paper.SCAN_MANIFEST_TYPE,
            "scan_ts": T0,
            "score_version": 2,
            "strategy_id": "auto-top10-v9-score-v2",
            "candidate_count": 10,
            "tradeable_candidate_count": 10,
            "eligible_cohort_size": 10,
            "stamped_entry_count": 10,
            "requests": 20,
            "failed_requests": 0,
            "complete_scan": True,
            "reason": "stamped",
        }
        capture = auto_paper._capture_summary([manifest])
        self.assertEqual(capture["segments"][0]["strategy_id"], manifest["strategy_id"])

    def test_public_record_has_deterministic_strategy_fields(self):
        candidate = {
            "token": TOKEN_A,
            "symbol": "AAA",
            "side": "base",
            "price_usd": 2.5,
            "score": 88.84,
            "subs": [0.5] * len(scoring.SCORE_WEIGHTS),
            "r": {
                "address": "pool-a",
                "liquidity_usd": 20_000,
                "vol_h24_usd": 10_000,
            },
        }
        first = log_picks.make_pick_record(
            candidate, 1, T0, public=True, logged_at="2026-01-01T00:01:00Z"
        )
        second = log_picks.make_pick_record(
            candidate, 1, T0, public=True, logged_at="2026-01-01T00:02:00Z"
        )
        self.assertEqual(first["strategy_id"], auto_paper.strategy_id(scoring.SCORE_VERSION))
        self.assertEqual(first["entry_id"], second["entry_id"])
        self.assertEqual(first["notional_usd"], 1.0)
        self.assertEqual(first["hold_hours"], 24)
        self.assertEqual(first["side"], "base")
        self.assertEqual(first["logged_at"], "2026-01-01T00:01:00Z")
        private = log_picks.make_pick_record(candidate, 1, T0, public=False)
        self.assertNotIn("strategy_id", private)
        invalid = log_picks.make_pick_record(
            {**candidate, "price_usd": None},
            1,
            T0,
            public=True,
            logged_at="2026-01-01T00:01:00Z",
        )
        self.assertNotIn("strategy_id", invalid)


if __name__ == "__main__":
    unittest.main()
