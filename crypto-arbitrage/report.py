from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "paper_arbitrage_usdt.db"


def scalar(connection: sqlite3.Connection, sql: str, params=()):
    row = connection.execute(sql, params).fetchone()
    return None if row is None else row[0]


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def print_cross_exchange(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "quotes") or not table_exists(connection, "opportunities"):
        print("Cross-exchange: no data")
        return

    quote_rows = scalar(connection, "SELECT COUNT(*) FROM quotes") or 0
    scans = scalar(connection, "SELECT COUNT(DISTINCT scan_id) FROM quotes") or 0
    candidates = scalar(connection, "SELECT COUNT(*) FROM opportunities") or 0
    qualified = scalar(connection, "SELECT COUNT(*) FROM opportunities WHERE qualifies = 1") or 0
    avg_net = scalar(connection, "SELECT AVG(net_profit_quote) FROM opportunities")
    quote_asset = scalar(connection, "SELECT quote_asset FROM opportunities LIMIT 1") or "QUOTE"
    best = connection.execute(
        """
        SELECT pair, buy_exchange, sell_exchange, net_profit_quote, net_profit_pct,
               gross_spread_pct, observed_at
        FROM opportunities
        ORDER BY net_profit_quote DESC
        LIMIT 1
        """
    ).fetchone()

    print("Cross-exchange")
    print("--------------")
    print(f"Scans:                 {scans}")
    print(f"Quote snapshots:       {quote_rows}")
    print(f"Best-route candidates: {candidates}")
    print(f"Qualified paper trades:{qualified:>5}")
    if candidates:
        print(f"Qualification rate:    {(qualified / candidates) * 100.0:8.3f}%")
    if avg_net is not None:
        print(f"Average best-route P&L:{quote_asset} {avg_net:+.4f}")
    if best:
        pair, buy, sell, net_quote, net_pct, gross_pct, observed_at = best
        print(f"Best route:            {pair} {buy}->{sell}")
        print(f"Best gross spread:     {gross_pct:+.4f}%")
        print(f"Best estimated net:    {net_pct:+.4f}% / {quote_asset} {net_quote:+.4f}")
        print(f"Best observed:         {observed_at}")


def print_triangular(connection: sqlite3.Connection) -> None:
    print("\nTriangular")
    print("----------")
    if not table_exists(connection, "triangular_opportunities"):
        print("No triangular data")
        return

    candidates = scalar(connection, "SELECT COUNT(*) FROM triangular_opportunities") or 0
    qualified = scalar(
        connection,
        "SELECT COUNT(*) FROM triangular_opportunities WHERE qualifies = 1",
    ) or 0
    avg_net = scalar(connection, "SELECT AVG(net_profit_quote) FROM triangular_opportunities")
    start_asset = scalar(connection, "SELECT start_asset FROM triangular_opportunities LIMIT 1") or "QUOTE"
    best = connection.execute(
        """
        SELECT path, symbols, net_profit_quote, net_profit_pct, gross_profit_pct,
               market_latency_ms, observed_at
        FROM triangular_opportunities
        ORDER BY net_profit_quote DESC
        LIMIT 1
        """
    ).fetchone()

    print(f"Snapshots with route:  {candidates}")
    print(f"Qualified paper trades:{qualified:>5}")
    if candidates:
        print(f"Qualification rate:    {(qualified / candidates) * 100.0:8.3f}%")
    if avg_net is not None:
        print(f"Average best-cycle P&L:{start_asset} {avg_net:+.4f}")
    if best:
        path, symbols, net_quote, net_pct, gross_pct, latency, observed_at = best
        print(f"Best cycle:            {path}")
        print(f"Symbols:               {symbols}")
        print(f"Best gross return:     {gross_pct:+.4f}%")
        print(f"Best estimated net:    {net_pct:+.4f}% / {start_asset} {net_quote:+.4f}")
        print(f"Market latency:        {latency:.0f} ms")
        print(f"Best observed:         {observed_at}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize crypto arbitrage paper-scanner results")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No paper database found at {args.db}")
        return 2

    connection = sqlite3.connect(args.db)
    try:
        print("Crypto Arbitrage Paper Report")
        print("=============================")
        print_cross_exchange(connection)
        print_triangular(connection)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
