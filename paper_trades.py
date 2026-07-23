"""Append-only paper-trade ledger and portfolio valuation helpers.

The ledger deliberately records events rather than editing rows in place.  Each
``open`` event is an independent lot, so buying the same token more than once
preserves its own entry price and timestamp.  ``close`` and ``void`` events refer
back to that immutable lot ID.

Stored numeric inputs are decimal strings.  Report-facing values returned by
``build_portfolio`` are floats so they can be serialized directly into HTML/JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / "data" / "paper_trades.jsonl"
VERSION = 1
BACKFILL_THRESHOLD_MINUTES = 15
TOKEN_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRADE_ID_RE = re.compile(r"^pt-\d{8}T\d{6}Z-[0-9a-f]{8}$")


class LedgerError(ValueError):
    """The paper-trade ledger or a requested event is invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str, field: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise LedgerError(f"{field} is required")
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LedgerError(
                f"{field} must be an ISO timestamp with a timezone"
            ) from exc
    else:
        raise LedgerError(f"{field} must be an ISO timestamp with a timezone")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise LedgerError(f"{field} must include Z or a timezone offset")
    return dt.astimezone(timezone.utc)


def canonical_timestamp(value: datetime | str) -> str:
    """Return an aware ISO timestamp in canonical UTC ``...Z`` form."""
    dt = _as_utc(value)
    text = dt.isoformat(timespec="microseconds")
    if text.endswith(".000000+00:00"):
        text = text.replace(".000000+00:00", "Z")
    else:
        text = text.replace("+00:00", "Z")
    return text


def normalize_token(value: str) -> str:
    """Validate an EVM address and normalize it to lowercase."""
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value.strip()):
        raise LedgerError("token must be 0x followed by exactly 40 hex characters")
    return value.strip().lower()


def _positive_decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise LedgerError(f"{field} must be a decimal number") from exc
    if not number.is_finite() or number <= 0:
        raise LedgerError(f"{field} must be greater than zero")
    return number


def _decimal_text(value: Decimal) -> str:
    """Plain, lossless decimal text (never scientific notation)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantity(invested: Decimal, price: Decimal) -> Decimal:
    # 50 significant decimal places is deterministic and far beyond the
    # precision supplied by the upstream market-price feed.
    with localcontext() as ctx:
        ctx.prec = 50
        return invested / price


def _clean_optional(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerError(f"{field} must be text")
    cleaned = value.strip()
    return cleaned or None


def _required_text(value: object, field: str) -> str:
    cleaned = _clean_optional(value, field)
    if cleaned is None:
        raise LedgerError(f"{field} is required")
    return cleaned


@dataclass(frozen=True)
class Lot:
    """One independently entered paper-trade lot reconstructed from events."""

    trade_id: str
    token: str
    symbol: str | None
    entry_price_usd: Decimal
    entry_ts: str
    invested_usd: Decimal
    quantity: Decimal
    note: str | None
    recorded_at: str
    exit_price_usd: Decimal | None = None
    exit_ts: str | None = None
    close_note: str | None = None
    closed_recorded_at: str | None = None
    void_reason: str | None = None
    voided_at: str | None = None
    voided_recorded_at: str | None = None

    @property
    def status(self) -> str:
        if self.voided_at is not None:
            return "void"
        if self.exit_ts is not None:
            return "closed"
        return "open"


def _line_error(line_no: int, message: str) -> LedgerError:
    return LedgerError(f"paper-trade ledger line {line_no}: {message}")


def _event_base(event: object, line_no: int) -> tuple[Mapping[str, object], str, str]:
    if not isinstance(event, dict):
        raise _line_error(line_no, "event must be a JSON object")
    if event.get("version") != VERSION:
        raise _line_error(line_no, f"unsupported version {event.get('version')!r}")
    kind = event.get("event")
    if kind not in {"open", "close", "void"}:
        raise _line_error(line_no, f"unknown event {kind!r}")
    trade_id = event.get("trade_id")
    if not isinstance(trade_id, str) or not TRADE_ID_RE.fullmatch(trade_id):
        raise _line_error(line_no, "invalid trade_id")
    try:
        canonical_timestamp(event.get("recorded_at"))  # type: ignore[arg-type]
    except LedgerError as exc:
        raise _line_error(line_no, f"invalid recorded_at: {exc}") from exc
    return event, kind, trade_id


def reconstruct_lots(events: Iterable[object]) -> list[Lot]:
    """Validate ordered ledger events and reconstruct their current lot state.

    Contradictory events are rejected at the line that introduces the problem.
    The return order is the order in which lots were opened.
    """
    lots: dict[str, Lot] = {}
    order: list[str] = []
    for line_no, raw_event in enumerate(events, 1):
        event, kind, trade_id = _event_base(raw_event, line_no)
        try:
            if kind == "open":
                if trade_id in lots:
                    raise LedgerError(f"duplicate open event for {trade_id}")
                token = normalize_token(event.get("token"))  # type: ignore[arg-type]
                entry_price = _positive_decimal(event.get("entry_price_usd"), "entry_price_usd")
                invested = _positive_decimal(event.get("invested_usd"), "invested_usd")
                quantity = _positive_decimal(event.get("quantity"), "quantity")
                expected = _quantity(invested, entry_price)
                if quantity != expected:
                    raise LedgerError(
                        "quantity does not equal invested_usd / entry_price_usd"
                    )
                lot = Lot(
                    trade_id=trade_id,
                    token=token,
                    symbol=_clean_optional(event.get("symbol"), "symbol"),
                    entry_price_usd=entry_price,
                    entry_ts=canonical_timestamp(event.get("entry_ts")),  # type: ignore[arg-type]
                    invested_usd=invested,
                    quantity=quantity,
                    note=_clean_optional(event.get("note"), "note"),
                    recorded_at=canonical_timestamp(event.get("recorded_at")),  # type: ignore[arg-type]
                )
                if _as_utc(lot.entry_ts) > _as_utc(lot.recorded_at):
                    raise LedgerError("entry_ts cannot be after recorded_at")
                lots[trade_id] = lot
                order.append(trade_id)
                continue

            lot = lots.get(trade_id)
            if lot is None:
                raise LedgerError(f"{kind} event references unknown lot {trade_id}")
            if kind == "close":
                if lot.status == "closed":
                    raise LedgerError(f"duplicate close event for {trade_id}")
                if lot.status == "void":
                    raise LedgerError(f"cannot close void lot {trade_id}")
                exit_price = _positive_decimal(event.get("exit_price_usd"), "exit_price_usd")
                exit_ts = canonical_timestamp(event.get("exit_ts"))  # type: ignore[arg-type]
                if _as_utc(exit_ts) < _as_utc(lot.entry_ts):
                    raise LedgerError("exit_ts cannot be before entry_ts")
                close_recorded = canonical_timestamp(event.get("recorded_at"))  # type: ignore[arg-type]
                if _as_utc(exit_ts) > _as_utc(close_recorded):
                    raise LedgerError("exit_ts cannot be after recorded_at")
                lots[trade_id] = replace(
                    lot,
                    exit_price_usd=exit_price,
                    exit_ts=exit_ts,
                    close_note=_clean_optional(event.get("note"), "note"),
                    closed_recorded_at=close_recorded,
                )
            else:
                if lot.status == "void":
                    raise LedgerError(f"duplicate void event for {trade_id}")
                if lot.status == "closed":
                    raise LedgerError(f"cannot void closed lot {trade_id}")
                reason = _required_text(event.get("reason"), "reason")
                voided_at = canonical_timestamp(event.get("voided_at"))  # type: ignore[arg-type]
                void_recorded = canonical_timestamp(event.get("recorded_at"))  # type: ignore[arg-type]
                if _as_utc(voided_at) > _as_utc(void_recorded):
                    raise LedgerError("voided_at cannot be after recorded_at")
                lots[trade_id] = replace(
                    lot,
                    void_reason=reason,
                    voided_at=voided_at,
                    voided_recorded_at=void_recorded,
                )
        except LedgerError as exc:
            raise _line_error(line_no, str(exc)) from exc
    return [lots[trade_id] for trade_id in order]


def load_events(path: os.PathLike[str] | str = LEDGER_PATH) -> list[dict[str, object]]:
    """Load raw JSONL events; malformed JSON includes its exact line number."""
    ledger = Path(path)
    if not ledger.exists():
        return []
    events: list[dict[str, object]] = []
    with ledger.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                raise _line_error(line_no, "blank lines are not valid JSON events")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _line_error(line_no, f"invalid JSON: {exc.msg}") from exc
            if not isinstance(event, dict):
                raise _line_error(line_no, "event must be a JSON object")
            events.append(event)
    return events


def load_lots(path: os.PathLike[str] | str = LEDGER_PATH) -> list[Lot]:
    """Load, validate, and reconstruct all ledger lots."""
    return reconstruct_lots(load_events(path))


def _append_event(path: os.PathLike[str] | str, event: Mapping[str, object]) -> None:
    ledger = Path(path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if ledger.exists() and ledger.stat().st_size:
        with ledger.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) not in {b"\n", b"\r"}
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(dict(event), sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _new_trade_id(when: datetime, existing: set[str]) -> str:
    prefix = when.astimezone(timezone.utc).strftime("pt-%Y%m%dT%H%M%SZ-")
    while True:
        candidate = prefix + secrets.token_hex(4)
        if candidate not in existing:
            return candidate


def add_trade(
    token: str,
    price: Decimal | str | int | float,
    at: datetime | str,
    usd: Decimal | str | int | float,
    *,
    symbol: str | None = None,
    note: str | None = None,
    ledger_path: os.PathLike[str] | str = LEDGER_PATH,
    trade_id: str | None = None,
    recorded_at: datetime | str | None = None,
) -> Lot:
    """Append one independent ``open`` lot and return its reconstructed state."""
    lots = load_lots(ledger_path)
    entry_price = _positive_decimal(price, "price")
    invested = _positive_decimal(usd, "usd")
    entry_dt = _as_utc(at, "at")
    recorded = _as_utc(recorded_at or _utc_now(), "recorded_at")
    if entry_dt > recorded:
        raise LedgerError("entry time cannot be in the future")
    existing = {lot.trade_id for lot in lots}
    if trade_id is None:
        trade_id = _new_trade_id(recorded, existing)
    elif not TRADE_ID_RE.fullmatch(trade_id) or trade_id in existing:
        raise LedgerError("trade_id is invalid or already exists")
    quantity = _quantity(invested, entry_price)
    event: dict[str, object] = {
        "version": VERSION,
        "event": "open",
        "trade_id": trade_id,
        "recorded_at": canonical_timestamp(recorded),
        "token": normalize_token(token),
        "symbol": _clean_optional(symbol, "symbol"),
        "entry_price_usd": _decimal_text(entry_price),
        "entry_ts": canonical_timestamp(entry_dt),
        "invested_usd": _decimal_text(invested),
        "quantity": _decimal_text(quantity),
        "note": _clean_optional(note, "note"),
    }
    # Validate the proposed event against current state before it is durable.
    reconstructed = reconstruct_lots([*load_events(ledger_path), event])[-1]
    _append_event(ledger_path, event)
    return reconstructed


def close_trade(
    trade_id: str,
    price: Decimal | str | int | float,
    at: datetime | str,
    *,
    note: str | None = None,
    ledger_path: os.PathLike[str] | str = LEDGER_PATH,
    recorded_at: datetime | str | None = None,
) -> Lot:
    """Append a full-lot close event; partial closes use separate entry lots."""
    events = load_events(ledger_path)
    lots = {lot.trade_id: lot for lot in reconstruct_lots(events)}
    lot = lots.get(trade_id)
    if lot is None:
        raise LedgerError(f"unknown trade_id {trade_id}")
    if lot.status == "closed":
        raise LedgerError(f"lot {trade_id} is already closed")
    if lot.status == "void":
        raise LedgerError(f"lot {trade_id} is void")
    exit_price = _positive_decimal(price, "price")
    exit_ts = canonical_timestamp(at)
    if _as_utc(exit_ts) < _as_utc(lot.entry_ts):
        raise LedgerError("close time cannot be before entry time")
    recorded = _as_utc(recorded_at or _utc_now(), "recorded_at")
    if _as_utc(exit_ts) > recorded:
        raise LedgerError("close time cannot be in the future")
    event: dict[str, object] = {
        "version": VERSION,
        "event": "close",
        "trade_id": trade_id,
        "recorded_at": canonical_timestamp(recorded),
        "exit_price_usd": _decimal_text(exit_price),
        "exit_ts": exit_ts,
        "note": _clean_optional(note, "note"),
    }
    updated = {item.trade_id: item for item in reconstruct_lots([*events, event])}[trade_id]
    _append_event(ledger_path, event)
    return updated


def void_trade(
    trade_id: str,
    reason: str,
    *,
    ledger_path: os.PathLike[str] | str = LEDGER_PATH,
    recorded_at: datetime | str | None = None,
) -> Lot:
    """Append an audit-preserving void event for an open lot."""
    events = load_events(ledger_path)
    lots = {lot.trade_id: lot for lot in reconstruct_lots(events)}
    lot = lots.get(trade_id)
    if lot is None:
        raise LedgerError(f"unknown trade_id {trade_id}")
    if lot.status != "open":
        raise LedgerError(f"only an open lot can be voided (current status: {lot.status})")
    recorded = canonical_timestamp(recorded_at or _utc_now())
    event: dict[str, object] = {
        "version": VERSION,
        "event": "void",
        "trade_id": trade_id,
        "recorded_at": recorded,
        "voided_at": recorded,
        "reason": _required_text(reason, "reason"),
    }
    updated = {item.trade_id: item for item in reconstruct_lots([*events, event])}[trade_id]
    _append_event(ledger_path, event)
    return updated


@dataclass(frozen=True)
class PriceObservation:
    token: str
    symbol: str | None
    ts: str
    price_usd: Decimal
    liquidity_usd: float
    pool_address: str
    side: str

    @property
    def timestamp(self) -> datetime:
        return _as_utc(self.ts)


def price_observations(conn, tokens: Iterable[str]) -> dict[str, list[PriceObservation]]:
    """Return best-liquidity price per token and timestamp from scanner data.

    Held addresses are matched independently against both sides of every pool:
    ``price_usd`` prices the base token and ``quote_price_usd`` prices the quote
    token.  This is intentionally independent from candidate-scoring rules.
    """
    wanted = {normalize_token(token) for token in tokens}
    if not wanted:
        return {}
    rows = []
    # Keep report generation proportional to held tokens as the snapshot table
    # grows. SQLite commonly caps bound parameters at 999, hence 400 addresses
    # per chunk (each address list is bound once for each side of the pair).
    wanted_list = sorted(wanted)
    for offset in range(0, len(wanted_list), 400):
        chunk = wanted_list[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(conn.execute(
            f"""
        SELECT s.ts, p.address, p.base_token, tb.symbol,
               p.quote_token, tq.symbol, s.price_usd, s.quote_price_usd,
               s.liquidity_usd
        FROM snapshots s
        JOIN pools p ON p.address = s.pool_address
        LEFT JOIN tokens tb ON tb.address = p.base_token
        LEFT JOIN tokens tq ON tq.address = p.quote_token
        WHERE p.base_token IN ({placeholders})
           OR p.quote_token IN ({placeholders})
        ORDER BY s.ts, p.address
        """,
            [*chunk, *chunk],
        ).fetchall())
    best: dict[tuple[str, str], PriceObservation] = {}
    for row in rows:
        raw_ts, pool, base, base_symbol, quote, quote_symbol, base_price, quote_price, liquidity = row
        try:
            ts = canonical_timestamp(raw_ts)
        except LedgerError:
            continue
        liq = max(float(liquidity or 0), 0.0)
        candidates = (
            (base, base_symbol, base_price, "base"),
            (quote, quote_symbol, quote_price, "quote"),
        )
        for address, symbol, raw_price, side in candidates:
            if not isinstance(address, str) or address.lower() not in wanted:
                continue
            try:
                price = _positive_decimal(raw_price, "market price")
            except LedgerError:
                continue
            token = address.lower()
            observation = PriceObservation(
                token=token,
                symbol=_clean_optional(symbol, "symbol"),
                ts=ts,
                price_usd=price,
                liquidity_usd=liq,
                pool_address=str(pool),
                side=side,
            )
            key = (token, ts)
            previous = best.get(key)
            if previous is None or (observation.liquidity_usd, observation.pool_address) > (
                previous.liquidity_usd,
                previous.pool_address,
            ):
                best[key] = observation
    result: dict[str, list[PriceObservation]] = {token: [] for token in wanted}
    for observation in best.values():
        result[observation.token].append(observation)
    for series in result.values():
        series.sort(key=lambda item: item.timestamp)
    return result


def latest_marks(
    observations: Mapping[str, Sequence[PriceObservation]],
    *,
    as_of: datetime | str | None = None,
) -> dict[str, PriceObservation]:
    """Select the latest known observation at or before ``as_of`` per token."""
    cutoff = _as_utc(as_of or _utc_now(), "as_of")
    marks: dict[str, PriceObservation] = {}
    for token, series in observations.items():
        eligible = [item for item in series if item.timestamp <= cutoff]
        if eligible:
            marks[token] = max(eligible, key=lambda item: item.timestamp)
    return marks


def _money(value: Decimal) -> float:
    return float(value)


def _return_pct(pnl: Decimal, invested: Decimal) -> float:
    return float((pnl / invested) * Decimal("100"))


def _base_lot_dict(
    lot: Lot, symbol: str | None, status: str | None = None
) -> dict[str, object]:
    entry_delay_minutes = max(
        (_as_utc(lot.recorded_at) - _as_utc(lot.entry_ts)).total_seconds() / 60,
        0.0,
    )
    return {
        "trade_id": lot.trade_id,
        "token": lot.token,
        "symbol": symbol,
        "status": status or lot.status,
        "entry_ts": lot.entry_ts,
        "recorded_at": lot.recorded_at,
        "entry_delay_minutes": entry_delay_minutes,
        "backfilled": entry_delay_minutes > BACKFILL_THRESHOLD_MINUTES,
        "entry_price_usd": _money(lot.entry_price_usd),
        "invested_usd": _money(lot.invested_usd),
        "quantity": _money(lot.quantity),
        "note": lot.note,
    }


def _observation_at(
    series: Sequence[PriceObservation],
    after: datetime,
    at_or_before: datetime,
) -> PriceObservation | None:
    matches = [item for item in series if after <= item.timestamp <= at_or_before]
    return matches[-1] if matches else None


def _trend_points(
    lots: Sequence[Lot],
    observations: Mapping[str, Sequence[PriceObservation]],
    as_of: datetime,
    stale_after: timedelta,
    interval_hours: int,
) -> list[dict[str, object]]:
    tracked = [lot for lot in lots if lot.status != "void" and _as_utc(lot.entry_ts) <= as_of]
    if not tracked:
        return []
    start = min(_as_utc(lot.entry_ts) for lot in tracked)
    interval_hours = max(int(interval_hours), 1)
    raw_count = max(int((as_of - start).total_seconds() // (interval_hours * 3600)) + 1, 1)
    if raw_count > 600:
        multiplier = (raw_count + 599) // 600
        interval_hours *= multiplier
    moments = {start, as_of}
    cursor = start
    step = timedelta(hours=interval_hours)
    while cursor < as_of:
        moments.add(cursor)
        cursor += step
    for lot in tracked:
        entry = _as_utc(lot.entry_ts)
        if entry <= as_of:
            moments.add(entry)
        if lot.exit_ts:
            exit_dt = _as_utc(lot.exit_ts)
            if exit_dt <= as_of:
                moments.add(exit_dt)

    points: list[dict[str, object]] = []
    for moment in sorted(moments):
        realized = Decimal("0")
        unrealized = Decimal("0")
        active_cost = Decimal("0")
        priced_cost = Decimal("0")
        stale_lots = 0
        unpriced_lots = 0
        for lot in tracked:
            entry_dt = _as_utc(lot.entry_ts)
            if entry_dt > moment:
                continue
            if lot.exit_ts and _as_utc(lot.exit_ts) <= moment:
                assert lot.exit_price_usd is not None
                realized += (lot.quantity * lot.exit_price_usd) - lot.invested_usd
                continue
            active_cost += lot.invested_usd
            # At the exact entry instant the user's entry price is authoritative,
            # making each lot's first point exactly zero P&L.
            if moment == entry_dt:
                price = lot.entry_price_usd
                priced_cost += lot.invested_usd
                mark_age = timedelta(0)
            else:
                mark = _observation_at(observations.get(lot.token, ()), entry_dt, moment)
                if mark is None:
                    unpriced_lots += 1
                    continue
                price = mark.price_usd
                priced_cost += lot.invested_usd
                mark_age = max(moment - mark.timestamp, timedelta(0))
            unrealized += (lot.quantity * price) - lot.invested_usd
            if mark_age > stale_after:
                stale_lots += 1
        coverage = Decimal("100") if active_cost == 0 else priced_cost / active_cost * Decimal("100")
        points.append(
            {
                "ts": canonical_timestamp(moment),
                # A portfolio total is unknown while any active cost basis has
                # no post-entry market mark. Keep the known subtotal separate
                # instead of silently treating an unpriced token as break-even.
                "pnl_usd": (
                    _money(realized + unrealized)
                    if priced_cost == active_cost else None
                ),
                "known_pnl_usd": _money(realized + unrealized),
                "realized_pnl_usd": _money(realized),
                "unrealized_pnl_usd": _money(unrealized),
                "price_coverage_pct": float(coverage),
                "stale_lots": stale_lots,
                "unpriced_lots": unpriced_lots,
            }
        )
    return points


def build_portfolio(
    conn,
    ledger_path: os.PathLike[str] | str = LEDGER_PATH,
    now: datetime | str | None = None,
    stale_after_hours: int | float = 24,
    trend_interval_hours: int = 6,
) -> dict[str, object]:
    """Build the complete JSON-friendly paper-portfolio dashboard payload."""
    as_of = _as_utc(now or _utc_now(), "now")
    if stale_after_hours <= 0:
        raise LedgerError("stale_after_hours must be greater than zero")
    stale_after = timedelta(hours=float(stale_after_hours))
    all_lots = load_lots(ledger_path)
    # `now` is also useful in deterministic reports/tests. Do not leak entries
    # or exits that occur after that as-of boundary into the current summary.
    lots = [lot for lot in all_lots if _as_utc(lot.entry_ts) <= as_of]
    observations = price_observations(
        conn, {lot.token for lot in lots if lot.status != "void"}
    )

    total_deployed = Decimal("0")
    open_cost = Decimal("0")
    open_value = Decimal("0")
    closed_proceeds = Decimal("0")
    realized = Decimal("0")
    unrealized = Decimal("0")
    priced_capital = Decimal("0")
    priced_open_cost = Decimal("0")
    stale_count = 0
    unpriced_count = 0
    lot_rows: list[dict[str, object]] = []

    for lot in lots:
        series = [item for item in observations.get(lot.token, ()) if item.timestamp <= as_of]
        fallback_symbol = series[-1].symbol if series else None
        symbol = lot.symbol or fallback_symbol
        closed_as_of = bool(lot.exit_ts and _as_utc(lot.exit_ts) <= as_of)
        effective_status = "void" if lot.status == "void" else (
            "closed" if closed_as_of else "open"
        )
        row = _base_lot_dict(lot, symbol, effective_status)
        if lot.status == "void":
            row.update(
                {
                    "void_reason": lot.void_reason,
                    "voided_at": lot.voided_at,
                    "mark_price_usd": None,
                    "mark_ts": None,
                    "value_usd": None,
                    "pnl_usd": None,
                    "return_pct": None,
                    "price_status": "void",
                    "price_age_hours": None,
                }
            )
            lot_rows.append(row)
            continue

        total_deployed += lot.invested_usd
        if closed_as_of:
            assert lot.exit_price_usd is not None and lot.exit_ts is not None
            proceeds = lot.quantity * lot.exit_price_usd
            pnl = proceeds - lot.invested_usd
            closed_proceeds += proceeds
            realized += pnl
            priced_capital += lot.invested_usd
            row.update(
                {
                    "exit_price_usd": _money(lot.exit_price_usd),
                    "exit_ts": lot.exit_ts,
                    "close_note": lot.close_note,
                    "mark_price_usd": _money(lot.exit_price_usd),
                    "mark_ts": lot.exit_ts,
                    "value_usd": _money(proceeds),
                    "pnl_usd": _money(pnl),
                    "return_pct": _return_pct(pnl, lot.invested_usd),
                    "price_status": "closed",
                    "price_age_hours": None,
                }
            )
            lot_rows.append(row)
            continue

        open_cost += lot.invested_usd
        entry_dt = _as_utc(lot.entry_ts)
        eligible_marks = [item for item in series if item.timestamp >= entry_dt]
        if not eligible_marks:
            unpriced_count += 1
            row.update(
                {
                    "mark_price_usd": None,
                    "mark_ts": None,
                    "value_usd": None,
                    "pnl_usd": None,
                    "return_pct": None,
                    "price_status": "unpriced",
                    "price_age_hours": None,
                }
            )
            lot_rows.append(row)
            continue
        mark = eligible_marks[-1]
        value = lot.quantity * mark.price_usd
        pnl = value - lot.invested_usd
        age = max((as_of - mark.timestamp).total_seconds() / 3600, 0.0)
        status = "stale" if age > stale_after.total_seconds() / 3600 else "fresh"
        if status == "stale":
            stale_count += 1
        open_value += value
        unrealized += pnl
        priced_capital += lot.invested_usd
        priced_open_cost += lot.invested_usd
        row.update(
            {
                "mark_price_usd": _money(mark.price_usd),
                "mark_ts": mark.ts,
                "value_usd": _money(value),
                "pnl_usd": _money(pnl),
                "return_pct": _return_pct(pnl, lot.invested_usd),
                "price_status": status,
                "price_age_hours": age,
            }
        )
        lot_rows.append(row)

    known_pnl = realized + unrealized
    fully_priced = unpriced_count == 0
    # Keep the denominator stable and intuitive: cumulative paper P&L / all
    # deployed capital. If any open lot is unpriced, the portfolio total and
    # return are unknown rather than silently assuming that lot is flat.
    total_return = (
        float(known_pnl / total_deployed * Decimal("100"))
        if fully_priced and total_deployed else None
    )
    coverage = (
        float(priced_open_cost / open_cost * Decimal("100")) if open_cost else 100.0
    )
    warnings: list[str] = []
    backfilled_count = sum(
        lot.status != "void"
        and (_as_utc(lot.recorded_at) - _as_utc(lot.entry_ts)).total_seconds()
        > BACKFILL_THRESHOLD_MINUTES * 60
        for lot in lots
    )
    if unpriced_count:
        warnings.append(
            f"{unpriced_count} open lot(s) have no market observation at or after entry; "
            "portfolio total P&L and return are unavailable until they are priced."
        )
    if stale_count:
        warnings.append(
            f"{stale_count} open lot(s) use a market price older than "
            f"{float(stale_after_hours):g} hours."
        )
    if backfilled_count:
        warnings.append(
            f"{backfilled_count} lot(s) were recorded more than "
            f"{BACKFILL_THRESHOLD_MINUTES} minutes after their entry timestamp; "
            "treat them as backfilled when evaluating prospective results."
        )

    closed_lot_count = sum(
        bool(lot.exit_ts and _as_utc(lot.exit_ts) <= as_of) for lot in lots
    )
    open_lot_count = sum(
        lot.status != "void"
        and not bool(lot.exit_ts and _as_utc(lot.exit_ts) <= as_of)
        for lot in lots
    )
    summary = {
        "total_lots": len(lots),
        "open_lots": open_lot_count,
        "closed_lots": closed_lot_count,
        "voided_lots": sum(lot.status == "void" for lot in lots),
        "total_deployed_usd": _money(total_deployed),
        "open_cost_basis_usd": _money(open_cost),
        "open_market_value_usd": _money(open_value),
        "closed_proceeds_usd": _money(closed_proceeds),
        "realized_pnl_usd": _money(realized),
        "unrealized_pnl_usd": _money(unrealized),
        "total_pnl_usd": _money(known_pnl) if fully_priced else None,
        "known_pnl_usd": _money(known_pnl),
        "priced_capital_usd": _money(priced_capital),
        "total_return_pct": total_return,
        "price_coverage_pct": coverage,
        "stale_open_lots": stale_count,
        "unpriced_open_lots": unpriced_count,
        "backfilled_lots": backfilled_count,
    }
    return {
        "as_of": canonical_timestamp(as_of),
        "summary": summary,
        "lots": lot_rows,
        "trend": _trend_points(
            lots,
            observations,
            as_of,
            stale_after,
            trend_interval_hours,
        ),
        "warnings": warnings,
    }


def _prompt(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        raise LedgerError(f"{label} is required")
    return value


def _print_lots(lots: Sequence[Lot]) -> None:
    if not lots:
        print("No paper trades recorded.")
        return
    print("TRADE ID                         STATUS  SYMBOL   INVESTED     ENTRY TIME")
    for lot in lots:
        print(
            f"{lot.trade_id:<32} {lot.status:<7} "
            f"{(lot.symbol or lot.token[:8]):<8} "
            f"${_decimal_text(lot.invested_usd):>10}  {lot.entry_ts}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Append-only paper-trade tracker")
    parser.add_argument("--ledger", default=str(LEDGER_PATH), help="JSONL ledger path")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record a new independent buy lot")
    add.add_argument("--token")
    add.add_argument("--price", help="entry price in USD")
    add.add_argument("--at", help="entry time (ISO 8601 with timezone)")
    add.add_argument("--usd", help="paper amount invested in USD")
    add.add_argument("--symbol")
    add.add_argument("--note")

    close = sub.add_parser("close", help="close an entire lot")
    close.add_argument("trade_id")
    close.add_argument("--price", help="exit price in USD")
    close.add_argument("--at", help="exit time (ISO 8601 with timezone)")
    close.add_argument("--note")

    void = sub.add_parser("void", help="void an erroneous open lot")
    void.add_argument("trade_id")
    void.add_argument("--reason", required=True)

    sub.add_parser("list", help="list all lots")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "add":
            lot = add_trade(
                args.token or _prompt("Token address"),
                args.price or _prompt("Entry price USD"),
                args.at or _prompt("Entry timestamp"),
                args.usd or _prompt("Amount invested USD"),
                symbol=args.symbol,
                note=args.note,
                ledger_path=args.ledger,
            )
            print(f"Added {lot.trade_id}: {_decimal_text(lot.quantity)} {lot.symbol or lot.token}")
        elif args.command == "close":
            lot = close_trade(
                args.trade_id,
                args.price or _prompt("Exit price USD"),
                args.at or _prompt("Exit timestamp"),
                note=args.note,
                ledger_path=args.ledger,
            )
            print(f"Closed {lot.trade_id} at ${_decimal_text(lot.exit_price_usd)}")
        elif args.command == "void":
            lot = void_trade(args.trade_id, args.reason, ledger_path=args.ledger)
            print(f"Voided {lot.trade_id}: {lot.void_reason}")
        else:
            _print_lots(load_lots(args.ledger))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
