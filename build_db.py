"""Rebuild data/scanner.db from the JSONL snapshot logs.

The GitHub Actions deployment appends every scan to data/snapshots/*.jsonl and
commits them — the repo is the data store. To analyze, pull the repo and run:

    python3 build_db.py
    python3 report.py

NOTE: this REPLACES data/scanner.db. If you also run scanner.py in SQLite mode
on this machine, keep that DB elsewhere or don't mix the two workflows.
"""

import glob
import json
import os

import db
import scanner


def main():
    files = sorted(glob.glob(os.path.join(scanner.SNAPSHOT_DIR, "*.jsonl"))
                   + glob.glob(os.path.join(scanner.SNAPSHOT_DIR, "*.jsonl.gz")))
    if not files:
        print(f"no JSONL files found in {scanner.SNAPSHOT_DIR}")
        return

    tmp = db.DB_PATH + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    real_path = db.DB_PATH
    db.DB_PATH = tmp
    try:
        conn = db.connect()
        total = 0
        for path in files:
            with scanner.open_snapshot(path) as f:
                parsed = [json.loads(line) for line in f if line.strip()]
            rows = [r for r in parsed if "_meta" not in r]
            for m in (r for r in parsed if r.get("_meta") == "scan"):
                conn.execute("INSERT OR REPLACE INTO scan_meta (ts, requests, failed) "
                             "VALUES (?,?,?)",
                             (m["ts"], m.get("requests", 0), m.get("failed", 0)))
            scanner.write_rows_db(conn, rows)
            total += len(rows)
            print(f"{os.path.basename(path)}: {len(rows)} rows")
        conn.commit()
        conn.close()
    finally:
        db.DB_PATH = real_path
    os.replace(tmp, db.DB_PATH)
    print(f"rebuilt {db.DB_PATH} with {total} snapshots from {len(files)} day(s)")

    import onchain
    cache = onchain.load_cache()
    if cache:
        conn = db.connect()
        onchain.upsert_db(conn, cache)
        conn.close()
        print(f"loaded on-chain data for {len(cache)} tokens")


if __name__ == "__main__":
    main()
