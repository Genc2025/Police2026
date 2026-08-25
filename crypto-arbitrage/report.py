from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "paper_arbitrage.db"


def scalar(connection: sqlite3.Connection, sql: str, params=()):
    row = connection.execute(sql, params).fetchone()
    return None if row is None else row[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize crypto arbitrage paper-scanner results")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No paper database found at {args.db}")
        return 2

    connection = sqlite3.connect(args.db)
    try:
        quote_rows = scalar(connection, "SELECT COUNT(*) FROM quotes") or 0
        scans = scalar(connection, "SELECT COUNT(DISTINCT scan_id) FROM quotes") or 0
        candidates = scalar(connection, "SELECT COUNT(*) FROM opportunities") or 0
        qualified = scalar(connection, "SELECT COUNT(*) FROM opportunities WHERE qualifies = 1") or 0
        best = connection.execute(
            """
            SELECT pair, buy_exchange, sell_exchange, net_profit_eur, net_profit_pct,
                   gross_spread_pct, observed_at
            FROM opportunities
            ORDER BY net_profit_eur DESC
            LIMIT 1
            """
        ).fetchone()

        print("Crypto Arbitrage Paper Report")
        print("=============================")
        print(f"Scans:                 {scans}")
        print(f"Quote snapshots:       {quote_rows}")
        print(f"Best-route candidates: {candidates}")
        print(f"Qualified paper trades:{qualified:>5}")
        if candidates:
            rate = (qualified / candidates) * 100.0
            print(f"Qualification rate:    {rate:8.3f}%")

        if best:
            pair, buy, sell, net_eur, net_pct, gross_pct, observed_at = best
            print("\nBest observed route")
            print(f"Pair:                  {pair}")
            print(f"Route:                 {buy} -> {sell}")
            print(f"Gross spread:          {gross_pct:+.4f}%")
            print(f"Estimated net:         {net_pct:+.4f}%")
            print(f"Paper profit:          EUR {net_eur:+.4f}")
            print(f"Observed:              {observed_at}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
