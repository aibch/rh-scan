"""Formatting-only tests for the automatic Top-10 dashboard section."""

import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_html


def _entry(book="historical", status="realized"):
    prospective = book == "prospective"
    row = {
        "entry_id": f"{book}-entry-1",
        "strategy_id": "top10-hourly-v3",
        "book": book,
        "scan_ts": "2026-07-22T10:00:00Z",
        "entry_ts": (
            "2026-07-22T10:02:00Z" if prospective
            else "2026-07-22T10:00:00Z"
        ),
        "decision_ts": (
            "2026-07-22T10:01:00Z" if prospective
            else "2026-07-22T10:00:00Z"
        ),
        "logged_at": "2026-07-22T10:01:00Z" if prospective else None,
        "rank": 1,
        "score": 88.2,
        "score_version": 3,
        "token": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "symbol": "AAA",
        "pool": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "side": "base",
        "notional_usd": 1.0,
        "hold_hours": 24,
        "fill_window_hours": 2,
        "fill_deadline_ts": "2026-07-22T12:01:00Z" if prospective else None,
        "outcome_tolerance_hours": 6,
        "entry_price_usd": 0.00000001,
        "quantity": 100_000_000,
        "status": status,
        "exit_target_ts": "2026-07-23T10:00:00Z",
        "exit_ts": None,
        "exit_price_usd": None,
        "exit_return_pct": None,
        "realized_pnl_usd": None,
        "mark_ts": "2026-07-22T12:00:00Z",
        "mark_price_usd": 0.000000011,
        "mark_return_pct": 10.0,
        "marked_value_usd": 1.1,
        "marked_pnl_usd": 0.1,
        "marks": {
            "1h": {
                "status": "observed",
                "return_pct": 3.0,
                "target_ts": "2026-07-22T11:02:00Z",
                "window_end_ts": "2026-07-22T12:02:00Z",
                "observed_ts": "2026-07-22T11:32:00Z",
                "observation_delay_hours": 0.5,
            },
            "6h": {
                "status": "observed",
                "return_pct": -4.0,
                "target_ts": "2026-07-22T16:02:00Z",
                "window_end_ts": "2026-07-22T17:32:00Z",
                "observed_ts": "2026-07-22T16:32:00Z",
                "observation_delay_hours": 0.5,
            },
            "72h": {
                "status": "pending",
                "return_pct": None,
                "target_ts": "2026-07-25T10:02:00Z",
                "window_end_ts": "2026-07-25T22:02:00Z",
                "observed_ts": None,
            },
            "168h": {
                "status": "censored",
                "return_pct": None,
                "target_ts": "2026-07-29T10:02:00Z",
                "window_end_ts": "2026-07-29T22:02:00Z",
                "observed_ts": None,
            },
        },
    }
    if status == "realized":
        row.update({
            "exit_ts": "2026-07-23T10:00:00Z",
            "exit_price_usd": 0.000000012,
            "exit_return_pct": 20.0,
            "realized_pnl_usd": 0.2,
            "mark_ts": "2026-07-23T10:00:00Z",
            "mark_price_usd": 0.000000012,
            "mark_return_pct": 20.0,
            "marked_value_usd": 1.2,
            "marked_pnl_usd": 0.2,
        })
    return row


def _book(kind, entries):
    realized = [entry for entry in entries if entry["status"] == "realized"]
    pending = [entry for entry in entries if entry["status"] == "pending"]
    summary = {
        "entry_count": len(entries),
        "cohort_count": len(entries),
        "unique_tokens": len({entry["token"] for entry in entries}),
        "pending_entries": len(pending),
        "realized_entries": len(realized),
        "censored_entries": 0,
        "awaiting_fill_entries": 0,
        "missed_fill_entries": 0,
        "unpriced_entries": 0,
        "stale_pending_entries": 0,
        "unmarked_pending_entries": 0,
        "deployed_entries": len(entries),
        "total_notional_usd": float(len(entries)),
        "deployed_notional_usd": float(len(entries)),
        "known_pnl_usd": sum(
            entry["realized_pnl_usd"] or entry["marked_pnl_usd"] or 0
            for entry in entries
        ),
        "price_coverage_pct": 100.0,
        "recorded_price_coverage_pct": 100.0,
        "fresh_price_coverage_pct": 100.0,
        "win_rate_pct": 100.0 if realized else None,
    }
    strategy_id = "top10-hourly-v3" if kind == "prospective" else None
    return {
        "book": kind,
        "strategy_id": strategy_id,
        "strategy_ids": [strategy_id] if strategy_id and entries else [],
        "score_versions": [3] if entries else [],
        "segments": ([{
            "strategy_id": strategy_id,
            "score_version": 3,
            "entry_count": len(entries),
            "summary": summary,
        }] if strategy_id and entries else []),
        "summary": summary,
        "entries": entries,
        "rank_stats": [{
            "rank": 1,
            "entry_count": len(entries),
            "pending": len(pending),
            "realized": len(realized),
            "censored": 0,
            "mean_return_pct": 20.0 if realized else None,
            "median_return_pct": 20.0 if realized else None,
            "win_rate_pct": 100.0 if realized else None,
            "rug_count": 0,
            "realized_pnl_usd": 0.2 * len(realized),
        }],
        "score_stats": ([{
            "band": "80-100",
            "entry_count": len(entries),
            "pending": len(pending),
            "realized": len(realized),
            "censored": 0,
            "mean_return_pct": 20.0,
            "median_return_pct": 20.0,
            "win_rate_pct": 100.0,
            "rug_count": 0,
            "realized_pnl_usd": 0.2 * len(realized),
        }] if realized else []),
        "realized_trend": ([{
            "ts": "2026-07-23T10:00:00Z",
            "period_pnl_usd": 0.2,
            "cumulative_pnl_usd": 0.2,
            "period_entries": 1,
            "cumulative_entries": 1,
            "cumulative_notional_usd": 1.0,
            "cumulative_return_pct": 20.0,
        }] if realized else []),
    }


def _payload(prospective, historical):
    return {
        "as_of": "2026-07-23T12:00:00Z",
        "score_version": 3,
        "strategy_id": "top10-hourly-v3",
        "notional_usd": 1.0,
        "hold_hours": 24,
        "fill_window_hours": 2,
        "outcome_tolerance_hours": 6,
        "capture": {
            "attempted_scans": 0,
            "stamped_scans": 0,
            "gated_scans": 0,
            "capture_rate_pct": None,
            "reason_counts": {},
            "first_manifest_ts": None,
        },
        "prospective": prospective,
        "historical": historical,
    }


class TestAutomaticStrategyFormatting(unittest.TestCase):
    def test_awaiting_fill_is_not_reported_as_deployed_or_backdated(self):
        entry = _entry("prospective", "pending")
        entry.update({
            "status": "awaiting_fill",
            "decision_ts": "2026-07-22T10:05:00Z",
            "logged_at": "2026-07-22T10:05:00Z",
            "entry_ts": None,
            "fill_ts": None,
            "entry_price_usd": None,
            "mark_ts": None,
            "mark_price_usd": None,
            "marked_value_usd": None,
            "marked_pnl_usd": None,
            "mark_return_pct": None,
            "marks": {
                key: {"status": "awaiting_fill", "return_pct": None}
                for key in ("1h", "6h", "72h", "168h")
            },
        })
        book = _book("prospective", [entry])
        book["summary"].update({
            "awaiting_fill_entries": 1,
            "deployed_entries": 0,
            "pending_entries": 0,
            "deployed_notional_usd": 0.0,
            "price_coverage_pct": 0.0,
            "recorded_price_coverage_pct": 0.0,
            "fresh_price_coverage_pct": 0.0,
        })

        kpis = report_html._auto_summary_kpis(book)
        table = report_html.auto_entry_table([entry])
        self.assertIn("$0", kpis)
        self.assertIn("1 awaiting fill", kpis)
        self.assertIn("0 censored", kpis)
        self.assertIn("0 missed fill", kpis)
        self.assertIn("0 unpriced", kpis)
        self.assertIn("Signal / fill", table)
        self.assertIn("awaiting price within 2h fill window", table)
        self.assertIn("awaiting fill", table)
        self.assertNotIn("rank finalized", table)

    def test_missed_fill_is_terminal_and_visibly_not_deployed(self):
        entry = _entry("prospective", "pending")
        entry.update({
            "status": "missed_fill",
            "entry_ts": None,
            "fill_ts": None,
            "entry_price_usd": None,
            "mark_ts": None,
            "mark_price_usd": None,
            "marked_value_usd": None,
            "marked_pnl_usd": None,
            "mark_return_pct": None,
            "marks": {
                key: {"status": "missed_fill", "return_pct": None}
                for key in ("1h", "6h", "72h", "168h")
            },
        })
        book = _book("prospective", [entry])
        book["summary"].update({
            "pending_entries": 0,
            "missed_fill_entries": 1,
            "deployed_entries": 0,
            "deployed_notional_usd": 0.0,
            "recorded_price_coverage_pct": 0.0,
            "fresh_price_coverage_pct": 0.0,
        })
        kpis = report_html._auto_summary_kpis(book)
        table = report_html.auto_entry_table([entry])
        self.assertIn("$0", kpis)
        self.assertIn("1 missed fill", kpis)
        self.assertIn("fill window expired", table)
        self.assertIn("<b>1h</b> missed fill", table)
        self.assertIn(">missed fill</span>", table)

    def test_win_rate_is_labeled_observed_only_with_censoring_bounds(self):
        book = _book("prospective", [_entry("prospective", "realized")])
        book["summary"].update({
            "censored_entries": 1,
            "matured_entries": 2,
            "observed_outcomes": 1,
            "win_rate_observed_pct": 100.0,
            "win_rate_lower_bound_pct": 50.0,
            "win_rate_upper_bound_pct": 100.0,
        })
        rendered = report_html._auto_summary_kpis(book)
        self.assertIn("Observed win rate", rendered)
        self.assertIn("strict bounds 50.0%–100.0% incl. censored", rendered)

    def test_empty_live_waits_and_keeps_history_visibly_separate(self):
        rendered = report_html.auto_strategy_section(_payload(
            _book("prospective", []),
            _book("historical", [_entry()]),
        ))

        self.assertIn("Automatic Top-10 strategy", rendered)
        self.assertIn("Waiting for the first public scan", rendered)
        self.assertNotIn("Live prospective book", rendered)
        self.assertIn("Historical score-v3 preview", rendered)
        self.assertIn("Retrospective, backdated scan-price replay only", rendered)
        self.assertEqual(rendered.count('class="kpis auto-kpis"'), 1)

    def test_live_and_historical_books_get_independent_metrics_and_tables(self):
        rendered = report_html.auto_strategy_section(_payload(
            _book("prospective", [_entry("prospective", "pending")]),
            _book("historical", [_entry()]),
        ))

        self.assertIn("Live prospective book", rendered)
        self.assertIn("Historical score-v3 preview", rendered)
        self.assertNotIn("Waiting for the first public scan", rendered)
        self.assertEqual(rendered.count('class="kpis auto-kpis"'), 2)
        self.assertIn("Recent live entries", rendered)
        self.assertIn("Recent historical entries", rendered)
        self.assertIn("Cumulative realized 24h P&amp;L", rendered)
        self.assertIn("Median 24h return by entry rank", rendered)
        self.assertIn("$0.00000001", rendered)
        self.assertIn("<b>1h</b> +3.0%", rendered)
        self.assertIn("<b>6h</b> -4.0%", rendered)
        self.assertIn("<b>3d</b> pending", rendered)
        self.assertIn("<b>7d</b> censored", rendered)
        self.assertIn("<b>AAA</b>", rendered)
        self.assertIn("filled +1m after signal", rendered)
        self.assertIn("scan quote is provenance only", rendered)
        self.assertIn("two hours after ranking", rendered)
        self.assertIn("through +6h", rendered)
        self.assertIn("observed 2026-07-22T11:32:00Z (+0.5h from target)", rendered)
        self.assertIn('aria-label="Median 24-hour return by entry rank"', rendered)
        self.assertIn("Median 24h return by score band", rendered)
        self.assertIn(
            'aria-label="Median 24-hour return by entry score band"', rendered
        )
        self.assertIn("Pending exposure by token", rendered)
        self.assertIn("Historical entry concentration", rendered)
        self.assertIn(
            'aria-label="Pending notional exposure share by token"', rendered
        )
        self.assertIn(
            'aria-label="Historical entry notional concentration by token"',
            rendered,
        )

    def test_recent_entry_table_is_bounded_and_escapes_token_labels(self):
        entries = []
        for i in range(30):
            entry = _entry("prospective", "pending")
            entry["entry_id"] = f"entry-{i}"
            entry["symbol"] = "<script>alert(1)</script>"
            entry["entry_ts"] = f"2026-07-22T{(i % 24):02d}:00:00Z"
            entries.append(entry)

        rendered = report_html.auto_entry_table(entries, limit=24)

        self.assertEqual(rendered.count("<tr>"), 25)  # header + 24 rows
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_pending_real_mark_discloses_staleness(self):
        entry = _entry("prospective", "pending")
        entry.update({"mark_age_hours": 3.0, "stale_mark": True})
        rendered = report_html.auto_entry_table([entry])
        self.assertIn("stale · 3.0h old", rendered)

    def test_capture_and_combined_versions_are_labeled_without_overclaiming(self):
        first = _entry("prospective", "realized")
        first["strategy_id"] = "auto-top10-v1-score-v3"
        second = _entry("prospective", "realized")
        second["entry_id"] = "v4-entry"
        second["strategy_id"] = "auto-top10-v1-score-v4"
        second["score_version"] = 4
        live = _book("prospective", [first, second])
        live["strategy_ids"] = [
            "auto-top10-v1-score-v3",
            "auto-top10-v1-score-v4",
        ]
        live["score_versions"] = [3, 4]
        live["segments"] = []
        for entry in (first, second):
            segment_summary = dict(live["summary"])
            segment_summary.update({
                "entry_count": 1,
                "cohort_count": 1,
                "deployed_entries": 1,
                "realized_entries": 1,
                "censored_entries": 0,
                "missed_fill_entries": 0,
            })
            live["segments"].append({
                "strategy_id": entry["strategy_id"],
                "score_version": entry["score_version"],
                "entry_count": 1,
                "summary": segment_summary,
            })
        payload = _payload(live, _book("historical", []))
        payload["capture"] = {
            "attempted_scans": 5,
            "stamped_scans": 3,
            "gated_scans": 2,
            "capture_rate_pct": 60.0,
            "reason_counts": {"partial_scan": 2, "stamped": 3},
            "first_manifest_ts": "2026-07-23T08:00:00Z",
        }
        rendered = report_html.auto_strategy_section(payload)
        self.assertIn("Combined live prospective book", rendered)
        self.assertIn("2 versions", rendered)
        self.assertIn("auto-top10-v1-score-v3", rendered)
        self.assertIn("auto-top10-v1-score-v4", rendered)
        self.assertIn("Logged scan attempts", rendered)
        self.assertIn("Logged-attempt acceptance", rendered)
        self.assertIn("60%", rendered)
        self.assertIn("not in this denominator", rendered)

    def test_coverage_kpi_discloses_stale_and_unmarked_counts(self):
        book = _book("prospective", [_entry("prospective", "pending")])
        book["summary"].update({
            "recorded_price_coverage_pct": 80.0,
            "fresh_price_coverage_pct": 20.0,
            "stale_pending_entries": 6,
            "unmarked_pending_entries": 2,
        })
        rendered = report_html._auto_summary_kpis(book)
        self.assertIn("Recorded-price coverage", rendered)
        self.assertIn("fresh 20% · 6 stale · 2 unmarked", rendered)

    def test_concentration_and_score_visuals_are_bounded(self):
        entries = []
        for i in range(9):
            entry = _entry("historical", "realized")
            entry["entry_id"] = f"entry-{i}"
            entry["token"] = "0x" + f"{i:040x}"
            entry["symbol"] = f"T{i}"
            entries.append(entry)
        exposure = report_html.auto_token_exposure_chart(entries)
        score = report_html.auto_score_band_chart([
            {
                "band": f"band-{i}",
                "realized": 2,
                "pending": 0,
                "censored": 0,
                "median_return_pct": i - 4,
                "win_rate_pct": 50.0,
                "rug_count": 0,
            }
            for i in range(10)
        ])

        self.assertEqual(exposure.count("<rect"), 7)  # six tokens + Other
        self.assertIn(">Other<", exposure)
        self.assertEqual(score.count("<rect"), 8)
        self.assertIn("<title>", exposure)
        self.assertIn("<title>", score)

    def test_trend_sampling_keeps_bucket_extrema(self):
        rows = [(index, float(index), {}) for index in range(500)]
        rows[251] = (251, -999.0, {})
        rows[252] = (252, 1_999.0, {})
        sampled = report_html._auto_sample_trend(rows, max_points=40)
        self.assertLessEqual(len(sampled), 40)
        self.assertIn(rows[0], sampled)
        self.assertIn(rows[-1], sampled)
        self.assertIn(rows[251], sampled)
        self.assertIn(rows[252], sampled)


if __name__ == "__main__":
    unittest.main()
