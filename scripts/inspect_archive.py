"""Read-only diagnostics for the TRIO options archive. Prints coverage only."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DEFAULT = Path.home() / "AppData/Local/TRIO/options/options_archive.sqlite3"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print(f"archive missing: {path}")
        return 1
    con = sqlite3.connect(f"file:{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = [row["name"] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tables)
    for table in tables:
        try:
            count = con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except sqlite3.Error:
            continue
        print(f"  {table}: {count} rows")
    if "candles" in tables:
        days = con.execute(
            """SELECT DISTINCT substr(timestamp_utc, 1, 10) AS day
               FROM candles ORDER BY day""").fetchall()
        print("candle days:", [row["day"] for row in days])
    if "quotes" in tables:
        days = con.execute(
            """SELECT DISTINCT substr(observed_at_utc, 1, 10) AS day
               FROM quotes ORDER BY day""").fetchall()
        print("quote days:", [row["day"] for row in days])
    if "contracts" in tables:
        rows = con.execute(
            """SELECT underlying, COUNT(*) AS n,
                      MIN(expiry_date) AS first_expiry, MAX(expiry_date) AS last_expiry
               FROM contracts GROUP BY underlying ORDER BY underlying""").fetchall()
        for row in rows:
            print(f"contracts {row['underlying']}: {row['n']} "
                  f"(expiry {row['first_expiry']}..{row['last_expiry']})")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())