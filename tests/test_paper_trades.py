"""Tests for the append-only paper-trade ledger and portfolio valuation."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import crypt_data
import paper_trades as pt
import report_html


TOKEN_A = "0xAa00000000000000000000000000000000000001"
TOKEN_B = "0xbb00000000000000000000000000000000000002"
TOKEN_A_NORM = TOKEN_A.lower()
TOKEN_B_NORM = TOKEN_B.lower()
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def trade_id(suffix: str) -> str:
    return f"pt-20260101T000000Z-{suffix}"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "paper_trades.jsonl"

    def add(
        self,
        token=TOKEN_A,
        price="2",
        at="2026-01-01T00:00:00Z",
        usd="10",
        *,
        suffix="00000001",
        symbol="AAA",
    ):
        return pt.add_trade(
            token,
            price,
            at,
            usd,
            symbol=symbol,
            ledger_path=self.ledger,
            trade_id=trade_id(suffix),
            recorded_at=at,
        )


class TestLedger(LedgerCase):
    def test_multiple_entries_same_token_are_independent_exact_lots(self):
        first = self.add(usd="10", price="2", suffix="00000001")
        second = self.add(
            usd="10",
            price="3",
            at="2026-01-02T01:00:00+01:00",
            suffix="00000002",
        )

        lots = pt.load_lots(self.ledger)
        self.assertEqual([lot.trade_id for lot in lots], [first.trade_id, second.trade_id])
        self.assertEqual(lots[0].token, TOKEN_A_NORM)
        self.assertEqual(lots[0].quantity, Decimal("5"))
        self.assertEqual(
            lots[1].quantity,
            Decimal("3.3333333333333333333333333333333333333333333333333"),
        )
        self.assertEqual(lots[1].entry_ts, "2026-01-02T00:00:00Z")

        raw = pt.load_events(self.ledger)
        self.assertIsInstance(raw[0]["quantity"], str)
        self.assertEqual(raw[0]["event"], "open")

    def test_token_decimal_and_timestamp_validation(self):
        with self.assertRaisesRegex(pt.LedgerError, "40 hex"):
            self.add(token="0xabc")
        with self.assertRaisesRegex(pt.LedgerError, "greater than zero"):
            self.add(price="0")
        with self.assertRaisesRegex(pt.LedgerError, "timezone"):
            self.add(at="2026-01-01T00:00:00")
        with self.assertRaisesRegex(pt.LedgerError, "future"):
            pt.add_trade(
                TOKEN_A, "1", "2026-01-02T00:00:00Z", "10",
                ledger_path=self.ledger,
                recorded_at="2026-01-01T00:00:00Z",
            )

    def test_close_and_void_enforce_state_transitions(self):
        closed = self.add(suffix="00000001")
        voided = self.add(token=TOKEN_B, suffix="00000002", symbol="BBB")

        result = pt.close_trade(
            closed.trade_id,
            "3",
            "2026-01-02T00:00:00Z",
            ledger_path=self.ledger,
            recorded_at="2026-01-02T00:00:00Z",
        )
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.exit_price_usd, Decimal("3"))
        with self.assertRaisesRegex(pt.LedgerError, "already closed"):
            pt.close_trade(closed.trade_id, "4", "2026-01-03T00:00:00Z", ledger_path=self.ledger)
        with self.assertRaisesRegex(pt.LedgerError, "only an open lot"):
            pt.void_trade(closed.trade_id, "mistake", ledger_path=self.ledger)

        result = pt.void_trade(
            voided.trade_id,
            "wrong token",
            ledger_path=self.ledger,
            recorded_at="2026-01-02T00:00:00Z",
        )
        self.assertEqual(result.status, "void")
        with self.assertRaisesRegex(pt.LedgerError, "is void"):
            pt.close_trade(voided.trade_id, "1", "2026-01-03T00:00:00Z", ledger_path=self.ledger)
        with self.assertRaisesRegex(pt.LedgerError, "unknown trade_id"):
            pt.void_trade(trade_id("99999999"), "missing", ledger_path=self.ledger)

    def test_close_before_entry_is_rejected_without_append(self):
        lot = self.add(at="2026-01-02T00:00:00Z")
        before = len(pt.load_events(self.ledger))
        with self.assertRaisesRegex(pt.LedgerError, "before entry"):
            pt.close_trade(lot.trade_id, "3", "2026-01-01T00:00:00Z", ledger_path=self.ledger)
        self.assertEqual(len(pt.load_events(self.ledger)), before)

    def test_malformed_and_contradictory_events_report_line_number(self):
        lot = self.add()
        event = pt.load_events(self.ledger)[0]
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        with self.assertRaisesRegex(pt.LedgerError, r"line 2: duplicate open"):
            pt.load_lots(self.ledger)

        other = Path(self.temp.name) / "bad.jsonl"
        other.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(pt.LedgerError, r"line 1: unsupported version"):
            pt.load_lots(other)

        other.write_text(json.dumps(event) + "\nnot-json\n", encoding="utf-8")
        with self.assertRaisesRegex(pt.LedgerError, r"line 2: invalid JSON"):
            pt.load_lots(other)


class ValuationCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(db.SCHEMA)

    def token(self, address, symbol):
        self.conn.execute(
            "INSERT INTO tokens(address,symbol,name,first_seen_at) VALUES(?,?,?,?)",
            (address.lower(), symbol, symbol, "2025-12-01T00:00:00Z"),
        )

    def pool(self, address, base, quote):
        self.conn.execute(
            """INSERT INTO pools(address,base_token,quote_token,dex,name,pool_created_at,first_seen_at)
               VALUES(?,?,?,?,?,?,?)""",
            (address, base.lower(), quote.lower(), "test", address, None, "2025-12-01T00:00:00Z"),
        )

    def snapshot(self, pool, ts, base_price, quote_price, liquidity):
        self.conn.execute(
            """INSERT INTO snapshots(ts,pool_address,price_usd,quote_price_usd,liquidity_usd)
               VALUES(?,?,?,?,?)""",
            (ts, pool, base_price, quote_price, liquidity),
        )
        self.conn.commit()


class TestPriceObservations(ValuationCase):
    def test_matches_both_sides_and_chooses_highest_liquidity_per_timestamp(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-base", TOKEN_A, WETH)
        self.pool("pool-quote", WETH, TOKEN_A)
        self.snapshot("pool-base", "2026-01-01T06:00:00Z", 2.0, 1800.0, 100)
        self.snapshot("pool-quote", "2026-01-01T06:00:00+00:00", 1800.0, 2.5, 1000)
        self.snapshot("pool-base", "2026-01-01T12:00:00Z", 3.0, 1800.0, 50)

        observations = pt.price_observations(self.conn, [TOKEN_A])
        self.assertEqual(len(observations[TOKEN_A_NORM]), 2)
        self.assertEqual(observations[TOKEN_A_NORM][0].price_usd, Decimal("2.5"))
        self.assertEqual(observations[TOKEN_A_NORM][0].side, "quote")
        self.assertEqual(observations[TOKEN_A_NORM][0].symbol, "AAA")

        latest = pt.latest_marks(observations, as_of="2026-01-01T12:30:00Z")
        self.assertEqual(latest[TOKEN_A_NORM].price_usd, Decimal("3.0"))
        earlier = pt.latest_marks(observations, as_of="2026-01-01T07:00:00Z")
        self.assertEqual(earlier[TOKEN_A_NORM].price_usd, Decimal("2.5"))


class TestPortfolio(ValuationCase):
    def test_multiple_lots_realized_unrealized_and_trend_math(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-01T06:00:00Z", 3.5, 1800, 1000)
        self.snapshot("pool-a", "2026-01-01T12:00:00Z", 4.0, 1800, 1000)
        self.snapshot("pool-a", "2026-01-01T18:00:00Z", 5.0, 1800, 1000)

        first = self.add(price="2", usd="100", suffix="00000001")
        self.add(
            price="3",
            usd="60",
            at="2026-01-01T06:00:00Z",
            suffix="00000002",
        )
        pt.close_trade(
            first.trade_id,
            "4",
            "2026-01-01T12:00:00Z",
            ledger_path=self.ledger,
            recorded_at="2026-01-01T12:00:00Z",
        )

        portfolio = pt.build_portfolio(
            self.conn,
            self.ledger,
            now="2026-01-01T18:00:00Z",
            stale_after_hours=24,
        )
        summary = portfolio["summary"]
        self.assertEqual(summary["total_deployed_usd"], 160.0)
        self.assertEqual(summary["open_market_value_usd"], 100.0)
        self.assertEqual(summary["realized_pnl_usd"], 100.0)
        self.assertEqual(summary["unrealized_pnl_usd"], 40.0)
        self.assertEqual(summary["total_pnl_usd"], 140.0)
        self.assertEqual(summary["total_return_pct"], 87.5)
        self.assertEqual(summary["price_coverage_pct"], 100.0)
        self.assertEqual(summary["open_lots"], 1)
        self.assertEqual(summary["closed_lots"], 1)

        by_time = {point["ts"]: point for point in portfolio["trend"]}
        self.assertEqual(by_time["2026-01-01T00:00:00Z"]["pnl_usd"], 0.0)
        # First lot marks at 3.5; the second lot's exact entry point stays at 3.
        self.assertEqual(by_time["2026-01-01T06:00:00Z"]["pnl_usd"], 75.0)
        self.assertEqual(by_time["2026-01-01T12:00:00Z"]["pnl_usd"], 120.0)
        self.assertEqual(by_time["2026-01-01T18:00:00Z"]["pnl_usd"], 140.0)

        second_row = next(row for row in portfolio["lots"] if row["status"] == "open")
        self.assertEqual(second_row["mark_price_usd"], 5.0)
        self.assertEqual(second_row["pnl_usd"], 40.0)
        self.assertEqual(second_row["price_status"], "fresh")

    def test_unpriced_coverage_and_stale_mark_are_explicit(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-01T01:00:00Z", 3.0, 1800, 1000)
        self.add(price="2", usd="75", suffix="00000001")
        self.add(token=TOKEN_B, price="1", usd="25", suffix="00000002", symbol="BBB")

        portfolio = pt.build_portfolio(
            self.conn,
            self.ledger,
            now="2026-01-02T01:00:00Z",
            stale_after_hours=6,
        )
        summary = portfolio["summary"]
        self.assertEqual(summary["price_coverage_pct"], 75.0)
        self.assertEqual(summary["stale_open_lots"], 1)
        self.assertEqual(summary["unpriced_open_lots"], 1)
        self.assertEqual(summary["unrealized_pnl_usd"], 37.5)
        self.assertEqual(summary["known_pnl_usd"], 37.5)
        self.assertIsNone(summary["total_pnl_usd"])
        self.assertIsNone(summary["total_return_pct"])
        self.assertEqual(len(portfolio["warnings"]), 2)
        status = {row["token"]: row["price_status"] for row in portfolio["lots"]}
        self.assertEqual(status[TOKEN_A_NORM], "stale")
        self.assertEqual(status[TOKEN_B_NORM], "unpriced")

    def test_all_unpriced_portfolio_never_reports_break_even_total(self):
        self.add(token=TOKEN_B, price="1", usd="25", symbol="BBB")
        portfolio = pt.build_portfolio(
            self.conn, self.ledger, now="2026-01-02T00:00:00Z"
        )
        self.assertEqual(portfolio["summary"]["price_coverage_pct"], 0.0)
        self.assertIsNone(portfolio["summary"]["total_pnl_usd"])
        self.assertIsNone(portfolio["summary"]["total_return_pct"])
        rendered = report_html.paper_section(portfolio)
        self.assertIn("unavailable until price coverage reaches 100%", rendered)

    def test_voided_lot_is_visible_but_excluded_from_totals(self):
        lot = self.add(usd="50")
        pt.void_trade(
            lot.trade_id,
            "typo",
            ledger_path=self.ledger,
            recorded_at="2026-01-01T01:00:00Z",
        )
        portfolio = pt.build_portfolio(self.conn, self.ledger, now="2026-01-02T00:00:00Z")
        self.assertEqual(portfolio["summary"]["total_deployed_usd"], 0.0)
        self.assertEqual(portfolio["summary"]["voided_lots"], 1)
        self.assertEqual(portfolio["lots"][0]["price_status"], "void")
        self.assertEqual(portfolio["trend"], [])

    def test_as_of_summary_excludes_future_entry_and_future_close(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-02T06:00:00Z", 3.0, 1800, 1000)
        lot = self.add(
            price="2", usd="10", at="2026-01-02T00:00:00Z", suffix="00000001"
        )
        pt.close_trade(
            lot.trade_id,
            "4",
            "2026-01-03T00:00:00Z",
            ledger_path=self.ledger,
            recorded_at="2026-01-03T00:00:00Z",
        )

        before_entry = pt.build_portfolio(
            self.conn, self.ledger, now="2026-01-01T23:00:00Z"
        )
        self.assertEqual(before_entry["summary"]["total_lots"], 0)
        self.assertEqual(before_entry["lots"], [])

        before_close = pt.build_portfolio(
            self.conn, self.ledger, now="2026-01-02T12:00:00Z"
        )
        self.assertEqual(before_close["summary"]["open_lots"], 1)
        self.assertEqual(before_close["summary"]["closed_lots"], 0)
        self.assertEqual(before_close["summary"]["realized_pnl_usd"], 0.0)
        self.assertEqual(before_close["summary"]["unrealized_pnl_usd"], 5.0)
        self.assertEqual(before_close["lots"][0]["status"], "open")

    def test_trend_forward_fills_observation_exactly_at_entry(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-01T06:00:00Z", 4.0, 1800, 1000)
        self.add(
            price="3", usd="30", at="2026-01-01T06:00:00Z", suffix="00000001"
        )
        portfolio = pt.build_portfolio(
            self.conn, self.ledger, now="2026-01-01T12:00:00Z"
        )
        by_time = {point["ts"]: point for point in portfolio["trend"]}
        self.assertEqual(by_time["2026-01-01T06:00:00Z"]["pnl_usd"], 0.0)
        self.assertEqual(by_time["2026-01-01T12:00:00Z"]["pnl_usd"], 10.0)
        self.assertEqual(by_time["2026-01-01T12:00:00Z"]["unpriced_lots"], 0)

    def test_backfilled_entry_is_visible_and_warned(self):
        pt.add_trade(
            TOKEN_B,
            "1",
            "2026-01-01T00:00:00Z",
            "25",
            symbol="BBB",
            ledger_path=self.ledger,
            trade_id=trade_id("00000001"),
            recorded_at="2026-01-01T01:00:00Z",
        )
        portfolio = pt.build_portfolio(
            self.conn, self.ledger, now="2026-01-01T02:00:00Z"
        )
        self.assertEqual(portfolio["summary"]["backfilled_lots"], 1)
        self.assertTrue(portfolio["lots"][0]["backfilled"])
        self.assertEqual(portfolio["lots"][0]["entry_delay_minutes"], 60.0)
        self.assertTrue(any("backfilled" in item for item in portfolio["warnings"]))
        self.assertIn("backfilled +1.0h later", report_html.paper_section(portfolio))

    def test_nonempty_report_renders_fractional_as_of_timestamp(self):
        self.token(TOKEN_A, "AAA")
        self.token(WETH, "WETH")
        self.pool("pool-a", TOKEN_A, WETH)
        self.snapshot("pool-a", "2026-01-01T06:00:00Z", 3.0, 1800, 1000)
        self.add(price="2", usd="10")

        rendered = report_html.build(
            self.conn,
            ledger_path=self.ledger,
            now=datetime(2026, 1, 1, 7, 0, 0, 123456, tzinfo=timezone.utc),
            picks_dir=Path(self.temp.name) / "empty-picks",
        )
        self.assertIn("Paper P&amp;L over time", rendered)
        self.assertIn("pt-20260101T000000Z-00000001", rendered)
        self.assertIn("2026-01-01T07:00:00.123456Z", rendered)


class TestEncryptedLedgerFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old_cwd)

    def test_pack_includes_paper_ledger_but_not_local_database(self):
        data = Path("data")
        data.mkdir()
        (data / "paper_trades.jsonl").write_bytes(b"private-ledger\n")
        (data / "scanner.db").write_bytes(b"private-db")
        with mock.patch.object(
            crypt_data, "encrypt_bytes", side_effect=lambda _key, value: b"ENC:" + value
        ):
            crypt_data.pack(b"key")
        encrypted = Path("dataenc/paper_trades.jsonl.enc")
        self.assertEqual(encrypted.read_bytes(), b"ENC:private-ledger\n")
        self.assertFalse(Path("dataenc/scanner.db.enc").exists())

    def test_private_unpack_preserves_ledger_but_public_unpack_restores_it(self):
        Path("data").mkdir()
        Path("dataenc").mkdir()
        ledger = Path("data/paper_trades.jsonl")
        encrypted = Path("dataenc/paper_trades.jsonl.enc")
        ledger.write_text("new-private\n", encoding="utf-8")
        encrypted.write_bytes(b"old-public")
        with mock.patch.object(crypt_data, "decrypt_bytes", return_value=b"old-public\n"):
            crypt_data.unpack(b"key")
            self.assertEqual(ledger.read_text(encoding="utf-8"), "new-private\n")
            Path(".public").touch()
            crypt_data.unpack(b"key")
        self.assertEqual(ledger.read_text(encoding="utf-8"), "old-public\n")


class TestPaperReportFormatting(unittest.TestCase):
    def test_tiny_token_prices_are_not_rounded_to_zero(self):
        self.assertEqual(report_html.fmt_token_price(1e-8), "$0.00000001")
        self.assertEqual(report_html.fmt_token_price(1e-12), "$0.000000000001")

    def test_void_row_is_labeled_excluded(self):
        portfolio = {
            "as_of": "2026-01-02T00:00:00Z",
            "summary": {
                "total_lots": 1, "open_lots": 0, "closed_lots": 0,
                "total_deployed_usd": 0.0, "open_cost_basis_usd": 0.0,
                "open_market_value_usd": 0.0, "unrealized_pnl_usd": 0.0,
                "realized_pnl_usd": 0.0, "total_pnl_usd": 0.0,
                "total_return_pct": None, "price_coverage_pct": 100.0,
                "stale_open_lots": 0, "unpriced_open_lots": 0,
            },
            "lots": [{
                "trade_id": trade_id("00000001"), "token": TOKEN_A_NORM,
                "symbol": "AAA", "status": "void", "entry_ts": "2026-01-01T00:00:00Z",
                "entry_price_usd": 1e-8, "invested_usd": 25.0, "quantity": 2.5e9,
                "value_usd": None, "pnl_usd": None, "return_pct": None,
                "price_status": "void", "void_reason": "typo",
            }],
            "trend": [], "warnings": [],
        }
        rendered = report_html.paper_section(portfolio)
        self.assertIn("excluded", rendered)
        self.assertIn("$0.00000001", rendered)
        self.assertIn("no deployed capital", rendered)

    def test_unknown_interval_is_not_bridged_by_total_line(self):
        points = [
            {"ts": "2026-01-01T00:00:00Z", "pnl_usd": 0.0,
             "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0,
             "price_coverage_pct": 100.0},
            {"ts": "2026-01-01T06:00:00Z", "pnl_usd": None,
             "known_pnl_usd": 2.0, "realized_pnl_usd": 0.0,
             "unrealized_pnl_usd": 2.0, "price_coverage_pct": 50.0,
             "unpriced_lots": 1},
            {"ts": "2026-01-01T12:00:00Z", "pnl_usd": 5.0,
             "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 5.0,
             "price_coverage_pct": 100.0},
        ]
        rendered = report_html.paper_pnl_chart(points)
        self.assertEqual(rendered.count('class="paper-total-segment"'), 2)
        self.assertIn('data-gap="true"', rendered)
        self.assertIn("Shaded gaps", rendered)


if __name__ == "__main__":
    unittest.main()
