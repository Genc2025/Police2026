from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.example.json"
USER_CONFIG = ROOT / "config.json"


@dataclass(frozen=True)
class Quote:
    exchange: str
    pair: str
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    observed_at: str


@dataclass(frozen=True)
class Opportunity:
    pair: str
    buy_exchange: str
    buy_ask: float
    buy_ask_qty: float
    sell_exchange: str
    sell_bid: float
    sell_bid_qty: float
    capital_eur: float
    base_qty: float
    gross_spread_pct: float
    estimated_cost_pct: float
    net_profit_pct: float
    net_profit_eur: float
    observed_at: str


SYMBOLS = {
    "BTC/EUR": {
        "binance": "BTCEUR",
        "kraken": "XBTEUR",
        "coinbase": "BTC-EUR",
        "bitstamp": "btceur",
    },
    "ETH/EUR": {
        "binance": "ETHEUR",
        "kraken": "ETHEUR",
        "coinbase": "ETH-EUR",
        "bitstamp": "etheur",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


async def fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url, timeout=8.0, headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    return response.json()


async def binance_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["binance"]
    data = await fetch_json(
        client,
        f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=5",
    )
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("binance", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now())


async def kraken_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["kraken"]
    data = await fetch_json(
        client,
        f"https://api.kraken.com/0/public/Depth?pair={symbol}&count=5",
    )
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    payload = next(iter(data["result"].values()))
    bid = payload["bids"][0]
    ask = payload["asks"][0]
    return Quote("kraken", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now())


async def coinbase_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    product = SYMBOLS[pair]["coinbase"]
    data = await fetch_json(
        client,
        f"https://api.exchange.coinbase.com/products/{product}/book?level=1",
    )
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("coinbase", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now())


async def bitstamp_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["bitstamp"]
    data = await fetch_json(
        client,
        f"https://www.bitstamp.net/api/v2/order_book/{symbol}/",
    )
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("bitstamp", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now())


FETCHERS = {
    "binance": binance_quote,
    "kraken": kraken_quote,
    "coinbase": coinbase_quote,
    "bitstamp": bitstamp_quote,
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected = path or (USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG)
    config = json.loads(selected.read_text(encoding="utf-8"))
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "virtual_capital_eur",
        "scan_interval_seconds",
        "slippage_pct_each_leg",
        "safety_buffer_pct",
        "minimum_net_profit_eur",
        "pairs",
        "fees_pct",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"Missing config keys: {sorted(missing)}")
    if float(config["virtual_capital_eur"]) <= 0:
        raise ValueError("virtual_capital_eur must be > 0")
    if float(config["scan_interval_seconds"]) < 1.0:
        raise ValueError("scan_interval_seconds must be >= 1.0")
    for pair in config["pairs"]:
        if pair not in SYMBOLS:
            raise ValueError(f"Unsupported pair: {pair}")
    for exchange in FETCHERS:
        if exchange not in config["fees_pct"]:
            raise ValueError(f"Missing fee assumption for {exchange}")


def evaluate(quotes: Iterable[Quote], config: dict[str, Any]) -> Opportunity | None:
    quote_list = list(quotes)
    capital = float(config["virtual_capital_eur"])
    fees = {name: float(value) / 100.0 for name, value in config["fees_pct"].items()}
    slip = float(config["slippage_pct_each_leg"]) / 100.0
    safety = float(config["safety_buffer_pct"]) / 100.0

    best: Opportunity | None = None

    for buy in quote_list:
        for sell in quote_list:
            if buy.exchange == sell.exchange or buy.pair != sell.pair:
                continue
            if buy.ask <= 0 or sell.bid <= 0 or buy.ask_qty <= 0 or sell.bid_qty <= 0:
                continue

            buy_fee = fees[buy.exchange]
            sell_fee = fees[sell.exchange]

            # Conservative paper model: pay taker fee + configured slippage on entry,
            # then sell the acquired base amount with taker fee + slippage on exit.
            effective_buy_price = buy.ask * (1.0 + buy_fee + slip)
            base_qty = capital / effective_buy_price

            # Require the full EUR 100 paper order to fit at the observed top-of-book on BOTH legs.
            if base_qty > buy.ask_qty or base_qty > sell.bid_qty:
                continue

            gross_sell_eur = base_qty * sell.bid
            net_sell_eur = gross_sell_eur * (1.0 - sell_fee - slip)
            safety_cost_eur = capital * safety
            net_profit_eur = net_sell_eur - capital - safety_cost_eur
            net_profit_pct = (net_profit_eur / capital) * 100.0
            gross_spread_pct = ((sell.bid / buy.ask) - 1.0) * 100.0
            estimated_cost_pct = gross_spread_pct - net_profit_pct

            candidate = Opportunity(
                pair=buy.pair,
                buy_exchange=buy.exchange,
                buy_ask=buy.ask,
                buy_ask_qty=buy.ask_qty,
                sell_exchange=sell.exchange,
                sell_bid=sell.bid,
                sell_bid_qty=sell.bid_qty,
                capital_eur=capital,
                base_qty=base_qty,
                gross_spread_pct=gross_spread_pct,
                estimated_cost_pct=estimated_cost_pct,
                net_profit_pct=net_profit_pct,
                net_profit_eur=net_profit_eur,
                observed_at=max(buy.observed_at, sell.observed_at),
            )
            if best is None or candidate.net_profit_eur > best.net_profit_eur:
                best = candidate

    return best


async def collect_pair(client: httpx.AsyncClient, pair: str) -> tuple[list[Quote], list[str]]:
    tasks = [FETCHERS[name](client, pair) for name in FETCHERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    quotes: list[Quote] = []
    errors: list[str] = []

    for name, result in zip(FETCHERS, results):
        if isinstance(result, Exception):
            errors.append(f"{name}: {type(result).__name__}: {result}")
        else:
            quotes.append(result)
    return quotes, errors


def init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            exchange TEXT NOT NULL,
            pair TEXT NOT NULL,
            bid REAL NOT NULL,
            bid_qty REAL NOT NULL,
            ask REAL NOT NULL,
            ask_qty REAL NOT NULL,
            observed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            buy_exchange TEXT NOT NULL,
            buy_ask REAL NOT NULL,
            sell_exchange TEXT NOT NULL,
            sell_bid REAL NOT NULL,
            capital_eur REAL NOT NULL,
            base_qty REAL NOT NULL,
            gross_spread_pct REAL NOT NULL,
            estimated_cost_pct REAL NOT NULL,
            net_profit_pct REAL NOT NULL,
            net_profit_eur REAL NOT NULL,
            observed_at TEXT NOT NULL,
            qualifies INTEGER NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def persist_scan(
    connection: sqlite3.Connection | None,
    scan_id: str,
    quotes: Iterable[Quote],
    opportunity: Opportunity | None,
    minimum_profit: float,
) -> None:
    if connection is None:
        return
    connection.executemany(
        """
        INSERT INTO quotes(scan_id, exchange, pair, bid, bid_qty, ask, ask_qty, observed_at)
        VALUES (:scan_id, :exchange, :pair, :bid, :bid_qty, :ask, :ask_qty, :observed_at)
        """,
        [{"scan_id": scan_id, **asdict(quote)} for quote in quotes],
    )
    if opportunity is not None:
        data = asdict(opportunity)
        connection.execute(
            """
            INSERT INTO opportunities(
                scan_id, pair, buy_exchange, buy_ask, sell_exchange, sell_bid,
                capital_eur, base_qty, gross_spread_pct, estimated_cost_pct,
                net_profit_pct, net_profit_eur, observed_at, qualifies
            ) VALUES (
                :scan_id, :pair, :buy_exchange, :buy_ask, :sell_exchange, :sell_bid,
                :capital_eur, :base_qty, :gross_spread_pct, :estimated_cost_pct,
                :net_profit_pct, :net_profit_eur, :observed_at, :qualifies
            )
            """,
            {
                **data,
                "scan_id": scan_id,
                "qualifies": int(opportunity.net_profit_eur >= minimum_profit),
            },
        )
    connection.commit()


def print_result(opportunity: Opportunity | None, minimum: float, pair: str) -> None:
    if opportunity is None:
        print(f"{pair:7} | NO COMPARABLE FULL-LIQUIDITY ROUTE")
        return
    status = "PAPER OPPORTUNITY" if opportunity.net_profit_eur >= minimum else "NO TRADE"
    print(
        f"{opportunity.pair:7} | BUY {opportunity.buy_exchange:9} {opportunity.buy_ask:.4f} | "
        f"SELL {opportunity.sell_exchange:9} {opportunity.sell_bid:.4f} | "
        f"gross {opportunity.gross_spread_pct:+.4f}% | costs {opportunity.estimated_cost_pct:.4f}% | "
        f"net {opportunity.net_profit_pct:+.4f}% = EUR {opportunity.net_profit_eur:+.4f} | {status}"
    )


async def scan_once(
    client: httpx.AsyncClient,
    config: dict[str, Any],
    connection: sqlite3.Connection | None,
) -> int:
    successful_exchanges: set[str] = set()
    minimum = float(config["minimum_net_profit_eur"])

    for pair in config["pairs"]:
        started = time.time_ns()
        quotes, errors = await collect_pair(client, pair)
        successful_exchanges.update(quote.exchange for quote in quotes)
        scan_id = f"{started}-{pair.replace('/', '')}"
        opportunity = evaluate(quotes, config) if len(quotes) >= 2 else None
        persist_scan(connection, scan_id, quotes, opportunity, minimum)
        print_result(opportunity, minimum, pair)
        for error in errors:
            print(f"  warning: {error}")

    return len(successful_exchanges)


async def run(config: dict[str, Any], once: bool, no_db: bool) -> int:
    interval = float(config["scan_interval_seconds"])
    db_path = ROOT / str(config.get("sqlite_path", "paper_arbitrage.db"))
    connection = None if no_db else init_db(db_path)

    print("Crypto Arbitrage Scanner V1 — READ ONLY / PAPER TRADING")
    print(f"Virtual capital: EUR {float(config['virtual_capital_eur']):.2f}")
    print(f"Minimum paper profit: EUR {float(config['minimum_net_profit_eur']):.2f}")
    print(f"SQLite journal: {'disabled' if no_db else db_path}")
    print("No API keys. No orders. No withdrawals.\n")

    headers = {
        "User-Agent": "Police2026-Crypto-Arbitrage-Scanner/1.1",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            while True:
                successful_exchanges = await scan_once(client, config, connection)
                if once:
                    return 0 if successful_exchanges >= 2 else 2
                print()
                await asyncio.sleep(interval)
    finally:
        if connection is not None:
            connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only cross-exchange arbitrage paper scanner")
    parser.add_argument("--once", action="store_true", help="Run one market snapshot and exit")
    parser.add_argument("--no-db", action="store_true", help="Do not write the SQLite paper journal")
    parser.add_argument("--config", type=Path, help="Path to a JSON config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    try:
        return asyncio.run(run(config, once=args.once, no_db=args.no_db))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
