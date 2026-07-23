"""Shared SQLite setup for the Robinhood Chain scanner."""

import os
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "scanner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    address        TEXT PRIMARY KEY,
    symbol         TEXT,
    name           TEXT,
    first_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    address         TEXT PRIMARY KEY,
    base_token      TEXT NOT NULL,
    quote_token     TEXT,
    dex             TEXT,
    name            TEXT,
    pool_created_at TEXT,
    first_seen_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pools_base_token ON pools (base_token);
CREATE INDEX IF NOT EXISTS idx_pools_quote_token ON pools (quote_token);

CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    pool_address     TEXT NOT NULL,
    price_usd        REAL,
    quote_price_usd  REAL,
    liquidity_usd    REAL,
    fdv_usd          REAL,
    market_cap_usd   REAL,
    vol_h24_usd      REAL,
    buys_h24         INTEGER,
    sells_h24        INTEGER,
    buyers_h24       INTEGER,
    sellers_h24      INTEGER,
    vol_liq_ratio    REAL,
    price_change_h24 REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_pool_ts ON snapshots (pool_address, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);

CREATE TABLE IF NOT EXISTS scan_meta (
    ts        TEXT PRIMARY KEY,
    requests  INTEGER,
    failed    INTEGER
);

CREATE TABLE IF NOT EXISTS token_onchain (
    address      TEXT PRIMARY KEY,
    checked_at   TEXT NOT NULL,
    verified     INTEGER,
    creator      TEXT,
    top10_pct    REAL,
    transfer_ok  INTEGER,
    holders      INTEGER
);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # migrate pre-existing DBs (CREATE IF NOT EXISTS won't alter live tables)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "quote_price_usd" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN quote_price_usd REAL")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(token_onchain)")}
    if "holders" not in cols:
        conn.execute("ALTER TABLE token_onchain ADD COLUMN holders INTEGER")
    return conn


def latest_rows(conn):
    """Latest snapshot per pool, with metadata and on-chain data for BOTH
    sides of the pair — the tradeable asset is sometimes the quote token."""
    return conn.execute("""
        SELECT p.address, p.name, p.pool_created_at, p.first_seen_at,
               t.symbol AS base_symbol, p.base_token,
               tq.symbol AS quote_symbol, p.quote_token,
               s.price_usd, s.quote_price_usd, s.liquidity_usd, s.fdv_usd,
               s.vol_h24_usd, s.vol_liq_ratio, s.buys_h24, s.sells_h24,
               s.buyers_h24, s.sellers_h24, s.price_change_h24, s.ts,
               oc.verified, oc.top10_pct, oc.transfer_ok, oc.creator,
               oc.holders,
               ocq.verified AS q_verified, ocq.top10_pct AS q_top10_pct,
               ocq.transfer_ok AS q_transfer_ok, ocq.holders AS q_holders
        FROM pools p
        LEFT JOIN tokens t ON t.address = p.base_token
        LEFT JOIN tokens tq ON tq.address = p.quote_token
        LEFT JOIN token_onchain oc ON oc.address = p.base_token
        LEFT JOIN token_onchain ocq ON ocq.address = p.quote_token
        JOIN snapshots s ON s.id = (SELECT id FROM snapshots
                                    WHERE pool_address = p.address
                                    ORDER BY ts DESC LIMIT 1)
    """).fetchall()
