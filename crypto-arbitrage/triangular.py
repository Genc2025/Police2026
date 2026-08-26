from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.example.json"
USER_CONFIG = ROOT / "config.json"
BINANCE_PUBLIC = "https://data-api.binance.vision"


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    symbol: str
    side: str
    price: float
    max_input: float


@dataclass(frozen=True)
class Triangle:
    exchange: str
    path: str
    symbols: str
    start_eur: float
    gross_end_eur: float
    net_end_eur: float
    gross_profit_pct: float
    net_profit_pct: float
    net_profit_eur: float
    market_latency_ms: float
    observed_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected = path or (USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG)
    config = json.loads(selected.read_text(encoding="utf-8"))
    tri = config.get("triangular")
    if not isinstance(tri, dict):
        raise ValueError("Missing triangular config")
    required = {
        "start_asset",
        "virtual_capital_eur",
        "fee_pct",
        "slippage_pct_each_leg",
        "safety_buffer_pct",
        "minimum_net_profit_eur",
        "max_market_data_latency_ms",
    }
    missing = required - set(tri)
    if missing:
        raise ValueError(f"Missing triangular config keys: {sorted(missing)}")
    if tri["start_asset"] != "EUR":
        raise ValueError("V1 triangular engine currently requires EUR as start_asset")
    if float(tri["virtual_capital_eur"]) <= 0:
        raise ValueError("triangular.virtual_capital_eur must be > 0")
    return config


async def fetch_json(client: httpx.AsyncClient, path: str) -> Any:
    response = await client.get(f"{BINANCE_PUBLIC}{path}", timeout=8.0, headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    return response.json()


async def fetch_market(client: httpx.AsyncClient) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], float]:
    started = time.perf_counter()
    tickers, exchange_info = await asyncio.gather(
        fetch_json(client, "/api/v3/ticker/bookTicker"),
        fetch_json(client, "/api/v3/exchangeInfo"),
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    symbols = {
        item["symbol"]: item
        for item in exchange_info.get("symbols", [])
        if item.get("status") == "TRADING" and item.get("isSpotTradingAllowed", True)
    }
    return tickers, symbols, latency_ms


def build_edges(tickers: list[dict[str, Any]], symbols: dict[str, dict[str, Any]]) -> list[Edge]:
    edges: list[Edge] = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        meta = symbols.get(symbol)
        if not meta:
            continue
        try:
            bid = float(ticker["bidPrice"])
            bid_qty = float(ticker["bidQty"])
            ask = float(ticker["askPrice"])
            ask_qty = float(ticker["askQty"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(bid, bid_qty, ask, ask_qty) <= 0:
            continue

        base = meta["baseAsset"]
        quote = meta["quoteAsset"]

        # Sell base at best bid: BASE -> QUOTE. Input capacity is bidQty BASE.
        edges.append(Edge(base, quote, symbol, "SELL", bid, bid_qty))

        # Buy base at best ask: QUOTE -> BASE. Input capacity is askPrice * askQty QUOTE.
        edges.append(Edge(quote, base, symbol, "BUY", ask, ask * ask_qty))
    return edges


def convert(amount: float, edge: Edge, fee_rate: float, slippage_rate: float) -> float | None:
    if amount <= 0 or amount > edge.max_input:
        return None
    retained = 1.0 - fee_rate - slippage_rate
    if retained <= 0:
        return None
    if edge.side == "SELL":
        return amount * edge.price * retained
    return (amount / edge.price) * retained


def convert_gross(amount: float, edge: Edge) -> float | None:
    if amount <= 0 or amount > edge.max_input:
        return None
    if edge.side == "SELL":
        return amount * edge.price
    return amount / edge.price


def find_best_triangle(edges: list[Edge], config: dict[str, Any], latency_ms: float) -> Triangle | None:
    tri = config["triangular"]
    start = str(tri["start_asset"])
    capital = float(tri["virtual_capital_eur"])
    fee_rate = float(tri["fee_pct"]) / 100.0
    slip_rate = float(tri["slippage_pct_each_leg"]) / 100.0
    safety_rate = float(tri["safety_buffer_pct"]) / 100.0
    max_latency = float(tri["max_market_data_latency_ms"])

    if latency_ms > max_latency:
        return None

    adjacency: dict[str, list[Edge]] = {}
    for edge in edges:
        adjacency.setdefault(edge.src, []).append(edge)

    best: Triangle | None = None
    observed_at = utc_now()

    for first in adjacency.get(start, []):
        if first.dst == start:
            continue
        for second in adjacency.get(first.dst, []):
            if second.dst in {start, first.src} or second.symbol == first.symbol:
                continue
            for third in adjacency.get(second.dst, []):
                if third.dst != start:
                    continue
                if len({first.symbol, second.symbol, third.symbol}) != 3:
                    continue

                gross1 = convert_gross(capital, first)
                if gross1 is None:
                    continue
                gross2 = convert_gross(gross1, second)
                if gross2 is None:
                    continue
                gross3 = convert_gross(gross2, third)
                if gross3 is None:
                    continue

                net1 = convert(capital, first, fee_rate, slip_rate)
                if net1 is None:
                    continue
                net2 = convert(net1, second, fee_rate, slip_rate)
                if net2 is None:
                    continue
                net3 = convert(net2, third, fee_rate, slip_rate)
                if net3 is None:
                    continue

                net_end = net3 - (capital * safety_rate)
                net_profit = net_end - capital
                gross_profit_pct = ((gross3 / capital) - 1.0) * 100.0
                net_profit_pct = (net_profit / capital) * 100.0
                candidate = Triangle(
                    exchange="binance",
                    path=f"{start}->{first.dst}->{second.dst}->{start}",
                    symbols=f"{first.symbol},{second.symbol},{third.symbol}",
                    start_eur=capital,
                    gross_end_eur=gross3,
                    net_end_eur=net_end,
                    gross_profit_pct=gross_profit_pct,
                    net_profit_pct=net_profit_pct,
                    net_profit_eur=net_profit,
                    market_latency_ms=latency_ms,
                    observed_at=observed_at,
                )
                if best is None or candidate.net_profit_eur > best.net_profit_eur:
                    best = candidate
    return best


def init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS triangular_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            path TEXT NOT NULL,
            symbols TEXT NOT NULL,
            start_eur REAL NOT NULL,
            gross_end_eur REAL NOT NULL,
            net_end_eur REAL NOT NULL,
            gross_profit_pct REAL NOT NULL,
            net_profit_pct REAL NOT NULL,
            net_profit_eur REAL NOT NULL,
            market_latency_ms REAL NOT NULL,
            observed_at TEXT NOT NULL,
            qualifies INTEGER NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def persist(connection: sqlite3.Connection | None, result: Triangle | None, minimum: float) -> None:
    if connection is None or result is None:
        return
    connection.execute(
        """
        INSERT INTO triangular_opportunities(
            exchange, path, symbols, start_eur, gross_end_eur, net_end_eur,
            gross_profit_pct, net_profit_pct, net_profit_eur, market_latency_ms,
            observed_at, qualifies
        ) VALUES (
            :exchange, :path, :symbols, :start_eur, :gross_end_eur, :net_end_eur,
            :gross_profit_pct, :net_profit_pct, :net_profit_eur, :market_latency_ms,
            :observed_at, :qualifies
        )
        """,
        {**asdict(result), "qualifies": int(result.net_profit_eur >= minimum)},
    )
    connection.commit()


def print_result(result: Triangle | None, minimum: float) -> None:
    if result is None:
        print("TRIANGULAR | no fresh/full-liquidity EUR triangle found")
        return
    status = "PAPER OPPORTUNITY" if result.net_profit_eur >= minimum else "NO TRADE"
    print(
        f"TRIANGULAR | {result.path} | {result.symbols} | "
        f"gross {result.gross_profit_pct:+.4f}% | net {result.net_profit_pct:+.4f}% "
        f"= EUR {result.net_profit_eur:+.4f} | latency {result.market_latency_ms:.0f}ms | {status}"
    )


async def run(config: dict[str, Any], cycles: int, no_db: bool) -> int:
    tri = config["triangular"]
    interval = float(config.get("scan_interval_seconds", 3.0))
    minimum = float(tri["minimum_net_profit_eur"])
    db_path = ROOT / str(config.get("sqlite_path", "paper_arbitrage.db"))
    connection = None if no_db else init_db(db_path)
    headers = {
        "User-Agent": "Police2026-Crypto-Arbitrage-Triangular/1.0",
        "Accept": "application/json",
    }

    print("Binance Triangular Arbitrage V1 — READ ONLY / PAPER TRADING")
    print(f"Start capital: EUR {float(tri['virtual_capital_eur']):.2f}")
    print("No API keys. No orders. No withdrawals.\n")

    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            for cycle in range(cycles):
                try:
                    tickers, symbols, latency_ms = await fetch_market(client)
                    edges = build_edges(tickers, symbols)
                    result = find_best_triangle(edges, config, latency_ms)
                    persist(connection, result, minimum)
                    print_result(result, minimum)
                except Exception as exc:
                    print(f"TRIANGULAR | warning: {type(exc).__name__}: {exc}")
                    if cycle == cycles - 1:
                        return 2
                if cycle + 1 < cycles:
                    await asyncio.sleep(interval)
    finally:
        if connection is not None:
            connection.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance EUR triangular-arbitrage paper scanner")
    parser.add_argument("--cycles", type=int, default=1, help="Number of snapshots to evaluate")
    parser.add_argument("--no-db", action="store_true", help="Disable SQLite persistence")
    parser.add_argument("--config", type=Path, help="Path to JSON config")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles must be >= 1")
    return args


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    try:
        return asyncio.run(run(config, args.cycles, args.no_db))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
