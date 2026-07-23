"""Derived hourly Top-10 paper strategy from the immutable pick log.

The pick JSONL files are the signal ledger; this module never writes another
trade ledger.  Prospective signals carry a versioned strategy ID at logging
time.  Older picks from the same score version form a clearly separate
historical book.

Strategy v1 is deliberately simple and precommitted: $1 per signal and a
fixed 24-hour exit. A prospective signal becomes available at ``logged_at``
and fills only at the first valid recorded pool-side price within the next
two hours; after that it is a terminal missed fill. The earlier scan quote
remains provenance, never an executable fill. The +24h outcome uses the first
recorded quote from its target through +6h, otherwise it is censored. Marks at
1h, 6h, 72h, and 168h are observational only. All results are gross of costs.
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import json
import math
import os
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import db
import scoring


STRATEGY_VERSION = 1
NOTIONAL_USD = 1.0
HOLD_HOURS = 24
FILL_WINDOW_HOURS = 2
EXIT_OBSERVATION_TOLERANCE_HOURS = 6
OBSERVATION_HOURS = (1, 6, 72, 168)
TOP_N = 10
MARK_STALE_HOURS = 2
PICKS_DIR = Path(db.DATA_DIR) / "picks"
STRATEGY_ID_RE = re.compile(r"^auto-top10-v(?P<strategy>\d+)-score-v(?P<score>\d+)$")
SCAN_MANIFEST_TYPE = "automatic_strategy_scan_v1"


class StrategyDataError(ValueError):
    """A targeted signal is malformed or conflicts with the immutable log."""


def strategy_id(score_version: int) -> str:
    """The strategy identity is segmented whenever the score model changes."""
    return f"auto-top10-v{STRATEGY_VERSION}-score-v{int(score_version)}"


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise StrategyDataError("invalid strategy timestamp") from exc
    else:
        raise StrategyDataError("invalid strategy timestamp")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StrategyDataError("strategy timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def _ts(value: datetime | str) -> str:
    dt = _utc(value)
    text = dt.isoformat(timespec="microseconds")
    return text.replace(".000000+00:00", "Z").replace("+00:00", "Z")


def make_entry_id(
    strategy: str, scan_ts: str, rank: int, token: str, pool: str
) -> str:
    """Stable ID: retries produce the same member without relying on wall time."""
    material = "|".join(
        (strategy, _ts(scan_ts), str(int(rank)), token.lower(), pool.lower())
    ).encode("utf-8")
    return "ap-" + hashlib.sha256(material).hexdigest()[:24]


def signal_metadata(
    *,
    score_version: int,
    scan_ts: str,
    rank: int,
    token: str,
    pool: str,
    side: str,
    logged_at: datetime | str,
) -> dict[str, object]:
    """Metadata stamped onto new public-deployment pick records."""
    sid = strategy_id(score_version)
    if side not in {"base", "quote"}:
        raise StrategyDataError("invalid strategy asset side")
    logged = _ts(logged_at)
    if _utc(logged) < _utc(scan_ts):
        raise StrategyDataError("logged_at cannot be before scan_ts")
    return {
        "strategy_id": sid,
        "entry_id": make_entry_id(sid, scan_ts, rank, token, pool),
        "notional_usd": NOTIONAL_USD,
        "hold_hours": HOLD_HOURS,
        "fill_window_hours": FILL_WINDOW_HOURS,
        "outcome_tolerance_hours": EXIT_OBSERVATION_TOLERANCE_HOURS,
        "logged_at": logged,
        "side": side,
    }


@dataclass(frozen=True)
class _Observation:
    ts: str
    epoch: float
    price: float | None
    liquidity: float


@dataclass(frozen=True)
class _Signal:
    entry_id: str
    strategy_id: str | None
    book: str
    scan_ts: str
    logged_at: str | None
    rank: int
    score: float
    score_version: int
    token: str
    symbol: str | None
    pool: str
    side: str
    signal_price: float | None
    signal_liquidity: float | None
    notional: float
    hold_hours: int
    fill_window_hours: float | None
    outcome_tolerance_hours: float
    strategy_version: int | None


def _finite_positive(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyDataError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise StrategyDataError(f"{name} must be greater than zero")
    return number


def _finite_number(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyDataError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise StrategyDataError(f"{name} must be finite")
    return number


def _optional_positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_strategy_id(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = STRATEGY_ID_RE.fullmatch(value)
    if match:
        return int(match.group("strategy")), int(match.group("score"))
    if value.startswith("auto-top10-"):
        raise StrategyDataError("malformed automatic strategy_id")
    return None


def _read_records(picks_dir: os.PathLike[str] | str) -> list[dict[str, object]]:
    paths = sorted(
        glob.glob(os.path.join(str(picks_dir), "*.jsonl"))
        + glob.glob(os.path.join(str(picks_dir), "*.jsonl.gz"))
    )
    records: list[dict[str, object]] = []
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StrategyDataError(
                        f"{path}:{line_no}: invalid JSON: {exc.msg}"
                    ) from exc
                if isinstance(record, dict):
                    records.append(record)
    return records


def _capture_summary(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Aggregate immutable public scan manifests without treating them as picks."""
    manifests: dict[tuple[str, int], dict[str, object]] = {}
    for raw in records:
        if raw.get("_meta") != SCAN_MANIFEST_TYPE:
            continue
        try:
            scan_ts = _ts(raw["scan_ts"])
            version = int(raw["score_version"])
            strategy = str(raw["strategy_id"])
            parsed_strategy = _parse_strategy_id(strategy)
            if parsed_strategy is None or parsed_strategy[1] != version:
                raise StrategyDataError(
                    "scan manifest strategy_id conflicts with score_version"
                )
            complete_scan = raw["complete_scan"]
            if not isinstance(complete_scan, bool):
                raise StrategyDataError(
                    "scan manifest complete_scan must be boolean"
                )
            reason = str(raw["reason"])
            values = {
                "scan_ts": scan_ts,
                "score_version": version,
                "strategy_id": strategy,
                "candidate_count": int(raw["candidate_count"]),
                "tradeable_candidate_count": int(
                    raw["tradeable_candidate_count"]
                ),
                "eligible_cohort_size": int(raw["eligible_cohort_size"]),
                "stamped_entry_count": int(raw["stamped_entry_count"]),
                "requests": int(raw["requests"]),
                "failed_requests": int(raw["failed_requests"]),
                "complete_scan": complete_scan,
                "reason": reason,
            }
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StrategyDataError):
                raise
            raise StrategyDataError(f"malformed automatic scan manifest: {exc}") from exc
        numeric = (
            "candidate_count",
            "tradeable_candidate_count",
            "eligible_cohort_size",
            "stamped_entry_count",
            "requests",
            "failed_requests",
        )
        if any(values[name] < 0 for name in numeric):
            raise StrategyDataError("automatic scan manifest counts cannot be negative")
        if (
            values["stamped_entry_count"] > values["eligible_cohort_size"]
            or values["eligible_cohort_size"] > TOP_N
            or values["stamped_entry_count"] not in {0, TOP_N}
        ):
            raise StrategyDataError("automatic scan manifest cohort counts conflict")
        key = (scan_ts, version)
        prior = manifests.get(key)
        if prior is not None and prior != values:
            raise StrategyDataError(
                f"conflicting automatic scan manifests for {scan_ts} score-v{version}"
            )
        manifests[key] = values

    rows = sorted(
        manifests.values(),
        key=lambda row: (_utc(str(row["scan_ts"])), int(row["score_version"])),
    )
    stamped = [row for row in rows if int(row["stamped_entry_count"]) > 0]
    reasons: dict[str, int] = defaultdict(int)
    for row in rows:
        reasons[str(row["reason"])] += 1
    by_strategy = []
    for sid in sorted({str(row["strategy_id"]) for row in rows}):
        segment = [row for row in rows if row["strategy_id"] == sid]
        segment_stamped = [
            row for row in segment if int(row["stamped_entry_count"]) > 0
        ]
        by_strategy.append({
            "strategy_id": sid,
            "score_version": int(segment[0]["score_version"]),
            "attempted_scans": len(segment),
            "stamped_scans": len(segment_stamped),
            "capture_rate_pct": (
                len(segment_stamped) / len(segment) * 100 if segment else None
            ),
        })
    return {
        "manifest_type": SCAN_MANIFEST_TYPE,
        "attempted_scans": len(rows),
        "stamped_scans": len(stamped),
        "gated_scans": len(rows) - len(stamped),
        "capture_rate_pct": len(stamped) / len(rows) * 100 if rows else None,
        "eligible_entries": sum(int(row["eligible_cohort_size"]) for row in rows),
        "stamped_entries": sum(int(row["stamped_entry_count"]) for row in rows),
        "failed_request_scans": sum(
            int(row["failed_requests"]) > 0 for row in rows
        ),
        "reason_counts": dict(sorted(reasons.items())),
        "first_manifest_ts": rows[0]["scan_ts"] if rows else None,
        "latest_manifest_ts": rows[-1]["scan_ts"] if rows else None,
        "segments": by_strategy,
    }


def _pool_sides(conn, pools: Iterable[str]) -> dict[str, tuple[str, str | None]]:
    values = sorted(set(pools))
    result: dict[str, tuple[str, str | None]] = {}
    for offset in range(0, len(values), 800):
        chunk = values[offset:offset + 800]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        for pool, base, quote in conn.execute(
            f"SELECT address,base_token,quote_token FROM pools WHERE address IN ({marks})",
            chunk,
        ):
            result[str(pool).lower()] = (
                str(base).lower(), str(quote).lower() if quote else None
            )
    return result


def _core_fingerprint(signal: _Signal) -> tuple[object, ...]:
    # logged_at is intentionally excluded: a retry can observe a different
    # wall clock but is still the same deterministic entry.
    return (
        signal.strategy_id,
        signal.book,
        signal.scan_ts,
        signal.rank,
        signal.score,
        signal.score_version,
        signal.token,
        signal.symbol,
        signal.pool,
        signal.side,
        signal.signal_price,
        signal.signal_liquidity,
        signal.notional,
        signal.hold_hours,
        signal.fill_window_hours,
        signal.outcome_tolerance_hours,
        signal.strategy_version,
    )


def _signals(
    conn,
    picks_dir: os.PathLike[str] | str,
    score_version: int,
) -> tuple[list[_Signal], list[_Signal]]:
    # Keep every stamped automatic signal, regardless of its strategy/score
    # version. Only the unstamped historical preview is restricted to the
    # caller's current score version.
    records = []
    for record in _read_records(picks_dir):
        if record.get("_meta") is not None:
            continue
        recorded_sid = record.get("strategy_id")
        parsed_sid = _parse_strategy_id(recorded_sid)
        try:
            version = int(record.get("score_version"))
            rank = int(record.get("rank", 99))
        except (TypeError, ValueError):
            if parsed_sid is not None:
                raise StrategyDataError("stamped prospective record has invalid rank/version")
            continue
        if not 1 <= rank <= TOP_N:
            continue
        if parsed_sid is not None or (recorded_sid is None and version == score_version):
            records.append(record)
    pools = {
        str(record.get("pool", "")).lower() for record in records if record.get("pool")
    }
    sides = _pool_sides(conn, pools)
    prospective: dict[str, _Signal] = {}
    historical: dict[str, _Signal] = {}
    all_ids: dict[str, _Signal] = {}

    for record in records:
        try:
            version = int(record["score_version"])
            rank = int(record["rank"])
            scan_ts = _ts(record["scan_ts"])
            token = str(record.get("token") or record.get("base_token") or "").lower()
            pool = str(record["pool"]).lower()
            if len(token) != 42 or not token.startswith("0x"):
                raise StrategyDataError("token must be a full EVM address")
            pool_pair = sides.get(pool)
            if pool_pair is None:
                raise StrategyDataError("pick pool is absent from scanner data")
            inferred = "base" if token == pool_pair[0] else (
                "quote" if token == pool_pair[1] else None
            )
            if inferred is None:
                raise StrategyDataError("pick token is not a member of its pool")
            explicit_side = record.get("side")
            if explicit_side is not None and explicit_side != inferred:
                raise StrategyDataError(
                    f"recorded side {explicit_side!r} conflicts with token/pool"
                )
            recorded_sid = record.get("strategy_id")
            parsed_sid = _parse_strategy_id(recorded_sid)
            book = "prospective" if parsed_sid is not None else "historical"
            if book == "prospective":
                for required in ("entry_id", "logged_at", "side", "notional_usd", "hold_hours"):
                    if record.get(required) is None:
                        raise StrategyDataError(f"prospective record is missing {required}")
                assert parsed_sid is not None and isinstance(recorded_sid, str)
                strategy_version, sid_score_version = parsed_sid
                if sid_score_version != version:
                    raise StrategyDataError(
                        "strategy_id score version conflicts with pick score_version"
                    )
                notional = _finite_positive(record["notional_usd"], "notional_usd")
                raw_hold = _finite_positive(record["hold_hours"], "hold_hours")
                hold = int(raw_hold)
                if raw_hold != hold:
                    raise StrategyDataError("hold_hours must be a whole number")
                fill_window = _finite_positive(
                    record.get("fill_window_hours", FILL_WINDOW_HOURS),
                    "fill_window_hours",
                )
                outcome_tolerance = _finite_positive(
                    record.get(
                        "outcome_tolerance_hours",
                        EXIT_OBSERVATION_TOLERANCE_HOURS,
                    ),
                    "outcome_tolerance_hours",
                )
                entry_id = str(record["entry_id"])
                expected_id = make_entry_id(recorded_sid, scan_ts, rank, token, pool)
                if entry_id != expected_id:
                    raise StrategyDataError(f"entry_id conflict for {scan_ts} rank {rank}")
                logged_at = _ts(record["logged_at"])
                if _utc(logged_at) < _utc(scan_ts):
                    raise StrategyDataError("logged_at cannot be before scan_ts")
            else:
                notional = NOTIONAL_USD
                hold = HOLD_HOURS
                fill_window = None
                outcome_tolerance = EXIT_OBSERVATION_TOLERANCE_HOURS
                strategy_version = None
                historical_sid = f"historical-score-v{version}"
                entry_id = make_entry_id(historical_sid, scan_ts, rank, token, pool)
                logged_at = None
            signal = _Signal(
                entry_id=entry_id,
                strategy_id=str(recorded_sid) if recorded_sid else None,
                book=book,
                scan_ts=scan_ts,
                logged_at=logged_at,
                rank=rank,
                score=_finite_number(record["score"], "score"),
                score_version=version,
                token=token,
                symbol=str(record["symbol"]) if record.get("symbol") else None,
                pool=pool,
                side=inferred,
                signal_price=_optional_positive(record.get("price_usd")),
                signal_liquidity=_optional_positive(record.get("liquidity_usd")),
                notional=notional,
                hold_hours=hold,
                fill_window_hours=fill_window,
                outcome_tolerance_hours=outcome_tolerance,
                strategy_version=strategy_version,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StrategyDataError):
                raise
            raise StrategyDataError(f"malformed score-v{score_version} pick: {exc}") from exc

        previous = all_ids.get(entry_id)
        if previous is not None:
            if _core_fingerprint(previous) != _core_fingerprint(signal):
                raise StrategyDataError(f"conflicting duplicate entry_id {entry_id}")
            # Exact retry duplicate: keep the earliest durable signal record.
            if signal.logged_at and previous.logged_at and signal.logged_at < previous.logged_at:
                all_ids[entry_id] = signal
                (prospective if book == "prospective" else historical)[entry_id] = signal
        else:
            all_ids[entry_id] = signal
            target = prospective if book == "prospective" else historical
            target[entry_id] = signal
    key = lambda item: (_utc(item.logged_at or item.scan_ts), item.rank, item.entry_id)
    return sorted(prospective.values(), key=key), sorted(historical.values(), key=key)


def _observations(
    conn, signals: Sequence[_Signal], cutoff: datetime
) -> dict[tuple[str, str], list[_Observation]]:
    pools = sorted({signal.pool for signal in signals})
    sides_by_pool = defaultdict(set)
    for signal in signals:
        sides_by_pool[signal.pool].add(signal.side)
    result: dict[tuple[str, str], list[_Observation]] = defaultdict(list)
    for offset in range(0, len(pools), 800):
        chunk = pools[offset:offset + 800]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT s.ts,s.pool_address,s.price_usd,s.quote_price_usd,s.liquidity_usd
            FROM snapshots s WHERE s.pool_address IN ({marks}) ORDER BY s.ts
            """,
            chunk,
        ).fetchall()
        for raw_ts, pool, base_price, quote_price, liquidity in rows:
            try:
                timestamp = _utc(raw_ts)
            except StrategyDataError:
                continue
            if timestamp > cutoff:
                continue
            for side in sides_by_pool[str(pool).lower()]:
                raw_price = base_price if side == "base" else quote_price
                try:
                    price = float(raw_price) if raw_price is not None else None
                except (TypeError, ValueError):
                    price = None
                if price is not None and (not math.isfinite(price) or price <= 0):
                    price = None
                try:
                    liq = max(float(liquidity or 0), 0.0)
                except (TypeError, ValueError):
                    liq = 0.0
                result[(str(pool).lower(), side)].append(
                    _Observation(_ts(timestamp), timestamp.timestamp(), price, liq)
                )
    return result


def _is_drained(observation: _Observation, entry_liquidity: float) -> bool:
    return observation.liquidity <= 100 or observation.liquidity <= 0.02 * entry_liquidity


def _outcome(
    series: Sequence[_Observation],
    entry_dt: datetime,
    entry_price: float,
    entry_liquidity: float,
    hours: int,
    now: datetime,
    tolerance_hours: float | None = None,
) -> dict[str, object]:
    target = entry_dt + timedelta(hours=hours)
    tolerance = timedelta(
        hours=tolerance_hours
        if tolerance_hours is not None
        else scoring.horizon_tolerance_s(hours / 24) / 3600
    )
    base = {
        "target_ts": _ts(target),
        "window_end_ts": _ts(target + tolerance),
        "tolerance_hours": tolerance.total_seconds() / 3600,
        "status": "pending",
        "observed_ts": None,
        "observation_delay_hours": None,
        "price_usd": None,
        "return_pct": None,
    }

    def observed_fields(observation: _Observation) -> dict[str, object]:
        return {
            "observed_ts": observation.ts,
            "observation_delay_hours": (
                observation.epoch - target.timestamp()
            ) / 3600,
        }

    if now < target:
        return base
    epochs = [item.epoch for item in series]
    index = bisect_left(epochs, target.timestamp())
    # A liquidity drain is absorbing. Once the position becomes effectively
    # unexitable, a later revived price print cannot rewrite the earlier loss.
    prior_drain = next(
        (
            item
            for item in series[:index]
            if item.epoch >= entry_dt.timestamp()
            and _is_drained(item, entry_liquidity)
        ),
        None,
    )
    if prior_drain is not None:
        return {
            **base,
            "status": "rug",
            **observed_fields(prior_drain),
            "price_usd": prior_drain.price,
            "return_pct": -99.9,
        }
    window_end = (target + tolerance).timestamp()
    for observation in series[index:]:
        if observation.epoch > window_end:
            break
        if _is_drained(observation, entry_liquidity):
            return {
                **base,
                "status": "rug",
                **observed_fields(observation),
                "price_usd": observation.price,
                "return_pct": -99.9,
            }
        if observation.price is not None:
            change = (observation.price - entry_price) / entry_price * 100
            return {
                **base,
                "status": "observed",
                **observed_fields(observation),
                "price_usd": observation.price,
                "return_pct": change,
            }
    # A drain already observed before the target is absorbing even when no
    # later quote exists; otherwise wait through the precommitted window.
    prior = next(
        (item for item in reversed(series[:index]) if item.epoch >= entry_dt.timestamp()),
        None,
    )
    if prior is not None and _is_drained(prior, entry_liquidity):
        return {
            **base,
            "status": "rug",
            **observed_fields(prior),
            "price_usd": prior.price,
            "return_pct": -99.9,
        }
    if now < target + tolerance:
        return base
    return {**base, "status": "censored"}


def _latest_mark(
    series: Sequence[_Observation],
    entry_dt: datetime,
    entry_price: float,
    entry_liquidity: float,
    now: datetime,
) -> tuple[str | None, float | None, float | None, float | None, bool | None]:
    # The logged pick is authoritative at the exact entry instant. Scanner
    # observations become marks only once they are strictly later.
    later = [item for item in series if item.epoch > entry_dt.timestamp()]
    if not later:
        return None, None, None, None, None
    drained = next(
        (item for item in later if _is_drained(item, entry_liquidity)), None
    )
    if drained is not None:
        age = max((now - _utc(drained.ts)).total_seconds() / 3600, 0.0)
        return (
            drained.ts,
            drained.price if drained.price is not None else entry_price * 0.001,
            -99.9,
            age,
            age > MARK_STALE_HOURS,
        )
    mark = later[-1]
    if mark.price is None:
        valid = next((item for item in reversed(later) if item.price is not None), None)
        if valid is None:
            return None, None, None, None, None
        mark = valid
    assert mark.price is not None
    age = max((now - _utc(mark.ts)).total_seconds() / 3600, 0.0)
    return (
        mark.ts,
        mark.price,
        (mark.price - entry_price) / entry_price * 100,
        age,
        age > MARK_STALE_HOURS,
    )


def _resolved_entry(
    signal: _Signal,
    series: Sequence[_Observation],
    now: datetime,
) -> tuple[str, tuple[datetime, float, float, str] | None]:
    """Resolve an honest entry without using information before availability."""
    if signal.book == "prospective":
        assert signal.logged_at is not None
        assert signal.fill_window_hours is not None
        available = _utc(signal.logged_at).timestamp()
        deadline = available + signal.fill_window_hours * 3600
        observation = next(
            (
                item
                for item in series
                if available <= item.epoch <= deadline and item.price is not None
            ),
            None,
        )
        if observation is None:
            state = "awaiting_fill" if now.timestamp() < deadline else "missed_fill"
            return state, None
        assert observation.price is not None
        return "filled", (
            _utc(observation.ts),
            observation.price,
            observation.liquidity,
            "first_recorded_within_fill_window",
        )
    if signal.signal_price is None or signal.signal_liquidity is None:
        return "unpriced", None
    return "filled", (
        _utc(signal.scan_ts),
        signal.signal_price,
        signal.signal_liquidity,
        "historical_scan_quote",
    )


def _unfilled_entry_row(signal: _Signal, status: str) -> dict[str, object]:
    fill_deadline = (
        _ts(
            _utc(signal.logged_at)
            + timedelta(hours=float(signal.fill_window_hours))
        )
        if signal.logged_at is not None and signal.fill_window_hours is not None
        else None
    )
    marks = {
        f"{hours}h": {
            "target_ts": None,
            "window_end_ts": None,
            "tolerance_hours": None,
            "status": status,
            "observed_ts": None,
            "observation_delay_hours": None,
            "price_usd": None,
            "return_pct": None,
        }
        for hours in OBSERVATION_HOURS
    }
    return {
        "entry_id": signal.entry_id,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "book": signal.book,
        "scan_ts": signal.scan_ts,
        "decision_ts": signal.logged_at or signal.scan_ts,
        "entry_ts": None,
        "fill_ts": None,
        "logged_at": signal.logged_at,
        "rank": signal.rank,
        "score": signal.score,
        "score_version": signal.score_version,
        "token": signal.token,
        "symbol": signal.symbol,
        "pool": signal.pool,
        "side": signal.side,
        "notional_usd": signal.notional,
        "hold_hours": signal.hold_hours,
        "fill_window_hours": signal.fill_window_hours,
        "fill_deadline_ts": fill_deadline,
        "outcome_tolerance_hours": signal.outcome_tolerance_hours,
        "signal_price_usd": signal.signal_price,
        "signal_liquidity_usd": signal.signal_liquidity,
        "entry_price_source": None,
        "entry_price_usd": None,
        "entry_liquidity_usd": None,
        "quantity": None,
        "status": status,
        "exit_target_ts": None,
        "exit_observation_window_end_ts": None,
        "exit_observation_delay_hours": None,
        "exit_ts": None,
        "exit_price_usd": None,
        "exit_return_pct": None,
        "realized_pnl_usd": None,
        "mark_ts": None,
        "mark_price_usd": None,
        "mark_return_pct": None,
        "mark_age_hours": None,
        "stale_mark": None,
        "marked_value_usd": None,
        "marked_pnl_usd": None,
        "marks": marks,
    }


def _entry_row(
    signal: _Signal,
    series: Sequence[_Observation],
    now: datetime,
) -> dict[str, object]:
    resolution_status, resolved = _resolved_entry(signal, series, now)
    if resolved is None:
        return _unfilled_entry_row(signal, resolution_status)
    entry_dt, entry_price, entry_liquidity, price_source = resolved
    exit_result = _outcome(
        series,
        entry_dt,
        entry_price,
        entry_liquidity,
        signal.hold_hours,
        now,
        tolerance_hours=signal.outcome_tolerance_hours,
    )
    marks = {
        f"{hours}h": _outcome(
            series, entry_dt, entry_price, entry_liquidity, hours, now
        )
        for hours in OBSERVATION_HOURS
    }
    quantity = signal.notional / entry_price
    exit_target = _ts(entry_dt + timedelta(hours=signal.hold_hours))
    if exit_result["status"] in {"observed", "rug"}:
        return_pct = float(exit_result["return_pct"])
        exit_price = entry_price * (1 + return_pct / 100)
        proceeds = signal.notional * (1 + return_pct / 100)
        pnl = proceeds - signal.notional
        status = "realized"
        exit_ts = exit_result["observed_ts"] or exit_target
        mark_ts, mark_price, mark_return = exit_ts, exit_price, return_pct
        mark_age_hours = stale_mark = None
        marked_value, marked_pnl = proceeds, pnl
    elif exit_result["status"] == "censored":
        status = "censored"
        exit_ts = exit_price = return_pct = pnl = None
        mark_ts = mark_price = mark_return = marked_value = marked_pnl = None
        mark_age_hours = stale_mark = None
    else:
        status = "pending"
        exit_ts = exit_price = return_pct = pnl = None
        mark_ts, mark_price, mark_return, mark_age_hours, stale_mark = _latest_mark(
            series, entry_dt, entry_price, entry_liquidity, now
        )
        if mark_return is None:
            marked_value = marked_pnl = None
        else:
            marked_value = signal.notional * (1 + mark_return / 100)
            marked_pnl = marked_value - signal.notional
    return {
        "entry_id": signal.entry_id,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "book": signal.book,
        "scan_ts": signal.scan_ts,
        "decision_ts": signal.logged_at or signal.scan_ts,
        "entry_ts": _ts(entry_dt),
        "fill_ts": _ts(entry_dt),
        "logged_at": signal.logged_at,
        "rank": signal.rank,
        "score": signal.score,
        "score_version": signal.score_version,
        "token": signal.token,
        "symbol": signal.symbol,
        "pool": signal.pool,
        "side": signal.side,
        "notional_usd": signal.notional,
        "hold_hours": signal.hold_hours,
        "fill_window_hours": signal.fill_window_hours,
        "fill_deadline_ts": (
            _ts(
                _utc(signal.logged_at)
                + timedelta(hours=float(signal.fill_window_hours))
            )
            if signal.logged_at is not None and signal.fill_window_hours is not None
            else None
        ),
        "outcome_tolerance_hours": signal.outcome_tolerance_hours,
        "signal_price_usd": signal.signal_price,
        "signal_liquidity_usd": signal.signal_liquidity,
        "entry_price_source": price_source,
        "entry_price_usd": entry_price,
        "entry_liquidity_usd": entry_liquidity,
        "quantity": quantity,
        "status": status,
        "exit_target_ts": exit_target,
        "exit_observation_window_end_ts": exit_result["window_end_ts"],
        "exit_observation_delay_hours": exit_result["observation_delay_hours"],
        "exit_ts": exit_ts,
        "exit_price_usd": exit_price,
        "exit_return_pct": return_pct,
        "realized_pnl_usd": pnl,
        "mark_ts": mark_ts,
        "mark_price_usd": mark_price,
        "mark_return_pct": mark_return,
        "mark_age_hours": mark_age_hours,
        "stale_mark": stale_mark,
        "marked_value_usd": marked_value,
        "marked_pnl_usd": marked_pnl,
        "marks": marks,
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (
        ordered[middle - 1] + ordered[middle]
    ) / 2


def _stats(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    realized = [row for row in rows if row["status"] == "realized"]
    returns = [float(row["exit_return_pct"]) for row in realized]
    pnl = sum(float(row["realized_pnl_usd"]) for row in realized)
    return {
        "entry_count": len(rows),
        "pending": sum(row["status"] == "pending" for row in rows),
        "realized": len(realized),
        "censored": sum(row["status"] == "censored" for row in rows),
        "awaiting_fill": sum(row["status"] == "awaiting_fill" for row in rows),
        "missed_fill": sum(row["status"] == "missed_fill" for row in rows),
        "unpriced": sum(row["status"] == "unpriced" for row in rows),
        "mean_return_pct": sum(returns) / len(returns) if returns else None,
        "median_return_pct": _median(returns),
        "win_rate_pct": 100 * sum(value > 0 for value in returns) / len(returns)
        if returns else None,
        "rug_count": sum(value <= -90 for value in returns),
        "realized_pnl_usd": pnl,
    }


def _rank_stats(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for entry in entries:
        grouped[int(entry["rank"])].append(entry)
    return [{"rank": rank, **_stats(grouped[rank])} for rank in sorted(grouped)]


SCORE_BANDS = ((80, 101, "80-100"), (60, 80, "60-79"),
               (40, 60, "40-59"), (0, 40, "0-39"))


def _score_stats(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    output = []
    for low, high, label in SCORE_BANDS:
        rows = [entry for entry in entries if low <= float(entry["score"]) < high]
        if rows:
            output.append({"band": label, **_stats(rows)})
    return output


def _realized_trend(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    by_ts: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for entry in entries:
        if entry["status"] == "realized":
            by_ts[str(entry["exit_ts"])].append(entry)
    cumulative_pnl = 0.0
    cumulative_entries = 0
    cumulative_notional = 0.0
    output = []
    for timestamp in sorted(by_ts, key=_utc):
        rows = by_ts[timestamp]
        period_pnl = sum(float(row["realized_pnl_usd"]) for row in rows)
        cumulative_pnl += period_pnl
        cumulative_entries += len(rows)
        cumulative_notional += sum(float(row["notional_usd"]) for row in rows)
        output.append({
            "ts": timestamp,
            "period_pnl_usd": period_pnl,
            "cumulative_pnl_usd": cumulative_pnl,
            "period_entries": len(rows),
            "cumulative_entries": cumulative_entries,
            "cumulative_notional_usd": cumulative_notional,
            "cumulative_return_pct": cumulative_pnl / cumulative_notional * 100
            if cumulative_notional else None,
        })
    return output


def _summary(entries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    pending = [entry for entry in entries if entry["status"] == "pending"]
    realized = [entry for entry in entries if entry["status"] == "realized"]
    censored = [entry for entry in entries if entry["status"] == "censored"]
    awaiting = [entry for entry in entries if entry["status"] == "awaiting_fill"]
    missed = [entry for entry in entries if entry["status"] == "missed_fill"]
    unpriced = [entry for entry in entries if entry["status"] == "unpriced"]
    deployed = [*pending, *realized, *censored]
    marked_pending = [
        entry for entry in pending if entry.get("marked_value_usd") is not None
    ]
    stale_pending = [
        entry for entry in marked_pending if entry.get("stale_mark") is True
    ]
    fresh_marked_pending = [
        entry for entry in marked_pending if entry.get("stale_mark") is not True
    ]
    unmarked_pending = [
        entry for entry in pending if entry.get("marked_value_usd") is None
    ]
    pending_value = sum(float(entry["marked_value_usd"]) for entry in marked_pending)
    pending_pnl = sum(float(entry["marked_pnl_usd"]) for entry in marked_pending)
    realized_proceeds = sum(
        float(entry["notional_usd"]) + float(entry["realized_pnl_usd"])
        for entry in realized
    )
    realized_pnl = sum(float(entry["realized_pnl_usd"]) for entry in realized)
    known_notional = sum(
        float(entry["notional_usd"]) for entry in marked_pending + realized
    )
    known_value = pending_value + realized_proceeds
    known_pnl = pending_pnl + realized_pnl
    fresh_known_notional = sum(
        float(entry["notional_usd"])
        for entry in fresh_marked_pending + realized
    )
    realized_returns = [float(entry["exit_return_pct"]) for entry in realized]
    wins = sum(value > 0 for value in realized_returns)
    matured = len(realized) + len(censored)
    total_notional = sum(float(entry["notional_usd"]) for entry in entries)
    return {
        "entry_count": len(entries),
        "cohort_count": len({
            (entry.get("strategy_id"), entry["scan_ts"]) for entry in entries
        }),
        "unique_tokens": len({entry["token"] for entry in entries}),
        "pending_entries": len(pending),
        "marked_pending_entries": len(marked_pending),
        "realized_entries": len(realized),
        "censored_entries": len(censored),
        "awaiting_fill_entries": len(awaiting),
        "missed_fill_entries": len(missed),
        "unpriced_entries": len(unpriced),
        "deployed_entries": len(deployed),
        "fresh_marked_pending_entries": len(fresh_marked_pending),
        "stale_pending_entries": len(stale_pending),
        "unmarked_pending_entries": len(unmarked_pending),
        "total_notional_usd": total_notional,
        "deployed_notional_usd": sum(
            float(entry["notional_usd"]) for entry in deployed
        ),
        "awaiting_fill_notional_usd": sum(
            float(entry["notional_usd"]) for entry in awaiting
        ),
        "missed_fill_notional_usd": sum(
            float(entry["notional_usd"]) for entry in missed
        ),
        "unpriced_notional_usd": sum(
            float(entry["notional_usd"]) for entry in unpriced
        ),
        "fresh_marked_pending_notional_usd": sum(
            float(entry["notional_usd"]) for entry in fresh_marked_pending
        ),
        "stale_pending_notional_usd": sum(
            float(entry["notional_usd"]) for entry in stale_pending
        ),
        "unmarked_pending_notional_usd": sum(
            float(entry["notional_usd"]) for entry in unmarked_pending
        ),
        "pending_notional_usd": sum(float(entry["notional_usd"]) for entry in pending),
        "realized_notional_usd": sum(float(entry["notional_usd"]) for entry in realized),
        "censored_notional_usd": sum(float(entry["notional_usd"]) for entry in censored),
        "pending_marked_value_usd": pending_value,
        "pending_marked_pnl_usd": pending_pnl,
        "realized_proceeds_usd": realized_proceeds,
        "realized_pnl_usd": realized_pnl,
        "known_value_usd": known_value if known_notional else None,
        "known_pnl_usd": known_pnl if known_notional else None,
        "known_notional_usd": known_notional,
        "known_return_pct": known_pnl / known_notional * 100 if known_notional else None,
        "price_coverage_pct": (
            known_notional / total_notional * 100 if total_notional else 100.0
        ),
        "recorded_price_coverage_pct": (
            known_notional / total_notional * 100 if total_notional else 100.0
        ),
        "fresh_price_coverage_pct": (
            fresh_known_notional / total_notional * 100
            if total_notional else 100.0
        ),
        "matured_entries": matured,
        "observed_outcomes": len(realized),
        "winning_entries": wins,
        # Backward-compatible point estimate, explicitly observed-only.
        "win_rate_pct": 100 * wins / len(realized_returns)
        if realized_returns else None,
        "win_rate_observed_pct": 100 * wins / len(realized_returns)
        if realized_returns else None,
        "win_rate_lower_bound_pct": 100 * wins / matured if matured else None,
        "win_rate_upper_bound_pct": 100 * (wins + len(censored)) / matured
        if matured else None,
        "rug_entries": sum(value <= -90 for value in realized_returns),
    }


def _book(
    kind: str,
    sid: str | None,
    signals: Sequence[_Signal],
    observations: Mapping[tuple[str, str], Sequence[_Observation]],
    now: datetime,
) -> dict[str, object]:
    entries = [
        _entry_row(signal, observations.get((signal.pool, signal.side), ()), now)
        for signal in signals
    ]
    strategy_ids = sorted({
        str(signal.strategy_id) for signal in signals if signal.strategy_id
    })
    score_versions = sorted({signal.score_version for signal in signals})
    segments = []
    for strategy in strategy_ids:
        segment_entries = [
            entry for entry in entries if entry.get("strategy_id") == strategy
        ]
        segments.append({
            "strategy_id": strategy,
            "score_version": segment_entries[0]["score_version"],
            "entry_count": len(segment_entries),
            "summary": _summary(segment_entries),
        })
    return {
        "book": kind,
        "strategy_id": sid,
        "strategy_ids": strategy_ids,
        "score_versions": score_versions,
        "segments": segments,
        "summary": _summary(entries),
        "entries": entries,
        "rank_stats": _rank_stats(entries),
        "score_stats": _score_stats(entries),
        "realized_trend": _realized_trend(entries),
    }


def build_strategy(
    conn,
    picks_dir: os.PathLike[str] | str = PICKS_DIR,
    now: datetime | str | None = None,
    score_version: int = scoring.SCORE_VERSION,
) -> dict[str, object]:
    """Build the combined live book plus a current-score historical preview.

    Every stamped automatic strategy/score version remains in ``prospective``;
    ``score_version`` scopes only the unstamped historical comparison.
    """
    as_of = _utc(now or datetime.now(timezone.utc))
    capture = _capture_summary(_read_records(picks_dir))
    prospective, historical = _signals(conn, picks_dir, int(score_version))
    all_signals = [*prospective, *historical]
    observations = _observations(conn, all_signals, as_of)
    sid = strategy_id(score_version)
    prospective_book = _book(
        "prospective", "combined", prospective, observations, as_of
    )
    historical_book = _book(
        "historical", None, historical, observations, as_of
    )
    return {
        "as_of": _ts(as_of),
        "score_version": int(score_version),
        "strategy_id": sid,
        "notional_usd": NOTIONAL_USD,
        "hold_hours": HOLD_HOURS,
        "fill_window_hours": FILL_WINDOW_HOURS,
        "outcome_tolerance_hours": EXIT_OBSERVATION_TOLERANCE_HOURS,
        "capture": capture,
        "prospective_strategy_ids": prospective_book["strategy_ids"],
        "prospective": prospective_book,
        "historical": historical_book,
    }
