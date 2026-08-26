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

from market_probe import MarketSupport, discover


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
    latency_ms: float


@dataclass(frozen=True)
class Opportunity:
    pair: str
    quote_asset: str
    buy_exchange: str
    buy_ask: float
    buy_ask_qty: float
    sell_exchange: str
    sell_bid: float
    sell_bid_qty: float
    capital_quote: float
    base_qty: float
    gross_spread_pct: float
    estimated_cost_pct: float
    net_profit_pct: float
    net_profit_quote: float
    observed_at: str
    max_leg_latency_ms: float
    snapshot_skew_ms: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def timestamp_ms(value: str) -> float:
    return datetime.fromisoformat(value).timestamp() * 1000.0


def split_pair(pair: str) -> tuple[str, str]:
    parts = pair.upper().split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid pair: {pair}")
    return parts[0], parts[1]


def symbol_for(exchange: str, pair: str) -> str:
    base, quote = split_pair(pair)
    if exchange == "binance":
        return f"{base}{quote}"
    if exchange == "kraken":
        kraken_alias = {"BTC": "XBT", "DOGE": "XDG"}
        return f"{kraken_alias.get(base, base)}{kraken_alias.get(quote, quote)}"
    if exchange == "coinbase":
        return f"{base}-{quote}"
    if exchange == "bitstamp":
        return f"{base}{quote}".lower()
    raise ValueError(f"Unsupported exchange: {exchange}")


async def fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url, timeout=8.0, headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    return response.json()


async def binance_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = symbol_for("binance", pair)
    started = time.perf_counter()
    data = await fetch_json(
        client,
        f"https://data-api.binance.vision/api/v3/depth?symbol={symbol}&limit=5",
    )
    latency = elapsed_ms(started)
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("binance", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now(), latency)


async def kraken_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = symbol_for("kraken", pair)
    started = time.perf_counter()
    data = await fetch_json(
        client,
        f"https://api.kraken.com/0/public/Depth?pair={symbol}&count=5",
    )
    latency = elapsed_ms(started)
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    payload = next(iter(data["result"].values()))
    bid = payload["bids"][0]
    ask = payload["asks"][0]
    return Quote("kraken", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now(), latency)


async def coinbase_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    product = symbol_for("coinbase", pair)
    started = time.perf_counter()
    data = await fetch_json(
        client,
        f"https://api.exchange.coinbase.com/products/{product}/book?level=1",
    )
    latency = elapsed_ms(started)
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("coinbase", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now(), latency)


async def bitstamp_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = symbol_for("bitstamp", pair)
    started = time.perf_counter()
    data = await fetch_json(
        client,
        f"https://www.bitstamp.net/api/v2/order_book/{symbol}/",
    )
    latency = elapsed_ms(started)
    bid = data["bids"][0]
    ask = data["asks"][0]
    return Quote("bitstamp", pair, float(bid[0]), float(bid[1]), float(ask[0]), float(ask[1]), utc_now(), latency)


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
        "capital_asset",
        "virtual_capital",
        "scan_interval_seconds",
        "slippage_pct_each_leg",
        "safety_buffer_pct",
        "minimum_net_profit",
        "pairs",
        "fees_pct",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"Missing config keys: {sorted(missing)}")
    capital_asset = str(config["capital_asset"]).upper()
    if float(config["virtual_capital"]) <= 0:
        raise ValueError("virtual_capital must be > 0")
    if float(config["scan_interval_seconds"]) < 1.0:
        raise ValueError("scan_interval_seconds must be >= 1.0")
    if float(config.get("max_quote_latency_ms", 1500.0)) <= 0:
        raise ValueError("max_quote_latency_ms must be > 0")
    if float(config.get("max_snapshot_skew_ms", 1000.0)) <= 0:
        raise ValueError("max_snapshot_skew_ms must be > 0")
    for pair in config["pairs"]:
        _base, quote = split_pair(str(pair))
        if quote != capital_asset:
            raise ValueError(f"Pair {pair} does not use capital_asset {capital_asset}")
    for exchange in FETCHERS:
        if exchange not in config["fees_pct"]:
            raise ValueError(f"Missing fee assumption for {exchange}")


def evaluate(quotes: Iterable[Quote], config: dict[str, Any]) -> Opportunity | None:
    quote_list = list(quotes)
    capital = float(config["virtual_capital"])
    quote_asset = str(config["capital_asset"]).upper()
    fees = {name: float(value) / 100.0 for name, value in config["fees_pct"].items()}
    slip = float(config["slippage_pct_each_leg"]) / 100.0
    safety = float(config["safety_buffer_pct"]) / 100.0
    max_latency = float(config.get("max_quote_latency_ms", 1500.0))
    max_skew = float(config.get("max_snapshot_skew_ms", 1000.0))

    best: Opportunity | None = None

    for buy in quote_list:
        for sell in quote_list:
            if buy.exchange == sell.exchange or buy.pair != sell.pair:
                continue
            if buy.ask <= 0 or sell.bid <= 0 or buy.ask_qty <= 0 or sell.bid_qty <= 0:
                continue

            _base, pair_quote = split_pair(buy.pair)
            if pair_quote != quote_asset:
                continue

            leg_latency = max(buy.latency_ms, sell.latency_ms)
            if leg_latency > max_latency:
                continue
            skew_ms = abs(timestamp_ms(buy.observed_at) - timestamp_ms(sell.observed_at))
            if skew_ms > max_skew:
                continue

            buy_fee = fees[buy.exchange]
            sell_fee = fees[sell.exchange]
            effective_buy_price = buy.ask * (1.0 + buy_fee + slip)
            base_qty = capital / effective_buy_price

            if base_qty > buy.ask_qty or base_qty > sell.bid_qty:
                continue

            gross_sell_quote = base_qty * sell.bid
            net_sell_quote = gross_sell_quote * (1.0 - sell_fee - slip)
            safety_cost_quote = capital * safety
            net_profit_quote = net_sell_quote - capital - safety_cost_quote
            net_profit_pct = (net_profit_quote / capital) * 100.0
            gross_spread_pct = ((sell.bid / buy.ask) - 1.0) * 100.0
            estimated_cost_pct = gross_spread_pct - net_profit_pct

            candidate = Opportunity(
                pair=buy.pair,
                quote_asset=quote_asset,
                buy_exchange=buy.exchange,
                buy_ask=buy.ask,
                buy_ask_qty=buy.ask_qty,
                sell_exchange=sell.exchange,
                sell_bid=sell.bid,
                sell_bid_qty=sell.bid_qty,
                capital_quote=capital,
                base_qty=base_qty,
                gross_spread_pct=gross_spread_pct,
                estimated_cost_pct=estimated_cost_pct,
                net_profit_pct=net_profit_pct,
                net_profit_quote=net_profit_quote,
                observed_at=max(buy.observed_at, sell.observed_at),
                max_leg_latency_ms=leg_latency,
                snapshot_skew_ms=skew_ms,
            )
            if best is None or candidate.net_profit_quote > best.net_profit_quote:
                best = candidate

    return best


def support_map(supports: Iterable[MarketSupport]) -> dict[str, frozenset[str]]:
    return {support.exchange: support.pairs for support in supports}


async def collect_pair(
    client: httpx.AsyncClient,
    pair: str,
    markets: dict[str, frozenset[str]] | None = None,
) -> tuple[list[Quote], list[str]]:
    exchange_names = [
        name
        for name in FETCHERS
        if markets is None or pair in markets.get(name, frozenset())
    ]
    tasks = [FETCHERS[name](client, pair) for name in exchange_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    quotes: list[Quote] = []
    errors: list[str] = []

    for name, result in zip(exchange_names, results):
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
            observed_at TEXT NOT NULL,
            latency_ms REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            quote_asset TEXT NOT NULL,
            buy_exchange TEXT NOT NULL,
            buy_ask REAL NOT NULL,
            sell_exchange TEXT NOT NULL,
            sell_bid REAL NOT NULL,
            capital_quote REAL NOT NULL,
            base_qty REAL NOT NULL,
            gross_spread_pct REAL NOT NULL,
            estimated_cost_pct REAL NOT NULL,
            net_profit_pct REAL NOT NULL,
            net_profit_quote REAL NOT NULL,
            observed_at TEXT NOT NULL,
            qualifies INTEGER NOT NULL,
            max_leg_latency_ms REAL NOT NULL,
            snapshot_skew_ms REAL NOT NULL
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
        INSERT INTO quotes(scan_id, exchange, pair, bid, bid_qty, ask, ask_qty, observed_at, latency_ms)
        VALUES (:scan_id, :exchange, :pair, :bid, :bid_qty, :ask, :ask_qty, :observed_at, :latency_ms)
        """,
        [{"scan_id": scan_id, **asdict(quote)} for quote in quotes],
    )
    if opportunity is not None:
        data = asdict(opportunity)
        connection.execute(
            """
            INSERT INTO opportunities(
                scan_id, pair, quote_asset, buy_exchange, buy_ask, sell_exchange, sell_bid,
                capital_quote, base_qty, gross_spread_pct, estimated_cost_pct,
                net_profit_pct, net_profit_quote, observed_at, qualifies,
                max_leg_latency_ms, snapshot_skew_ms
            ) VALUES (
                :scan_id, :pair, :quote_asset, :buy_exchange, :buy_ask, :sell_exchange, :sell_bid,
                :capital_quote, :base_qty, :gross_spread_pct, :estimated_cost_pct,
                :net_profit_pct, :net_profit_quote, :observed_at, :qualifies,
                :max_leg_latency_ms, :snapshot_skew_ms
            )
            """,
            {
                **data,
                "scan_id": scan_id,
                "qualifies": int(opportunity.net_profit_quote >= minimum_profit),
            },
        )
    connection.commit()


def print_result(opportunity: Opportunity | None, minimum: float, pair: str, quote_asset: str) -> None:
    if opportunity is None:
        print(f"{pair:10} | NO COMPARABLE FULL-LIQUIDITY/FRESH ROUTE")
        return
    status = "PAPER OPPORTUNITY" if opportunity.net_profit_quote >= minimum else "NO TRADE"
    print(
        f"{opportunity.pair:10} | BUY {opportunity.buy_exchange:9} {opportunity.buy_ask:.8f} | "
        f"SELL {opportunity.sell_exchange:9} {opportunity.sell_bid:.8f} | "
        f"gross {opportunity.gross_spread_pct:+.4f}% | costs {opportunity.estimated_cost_pct:.4f}% | "
        f"net {opportunity.net_profit_pct:+.4f}% = {quote_asset} {opportunity.net_profit_quote:+.4f} | "
        f"lat {opportunity.max_leg_latency_ms:.0f}ms skew {opportunity.snapshot_skew_ms:.0f}ms | {status}"
    )


async def scan_once(
    client: httpx.AsyncClient,
    config: dict[str, Any],
    connection: sqlite3.Connection | None,
    markets: dict[str, frozenset[str]],
) -> int:
    successful_exchanges: set[str] = set()
    minimum = float(config["minimum_net_profit"])
    quote_asset = str(config["capital_asset"]).upper()

    for pair in config["pairs"]:
        started = time.time_ns()
        quotes, errors = await collect_pair(client, pair, markets)
        successful_exchanges.update(quote.exchange for quote in quotes)
        scan_id = f"{started}-{pair.replace('/', '')}"
        opportunity = evaluate(quotes, config) if len(quotes) >= 2 else None
        persist_scan(connection, scan_id, quotes, opportunity, minimum)
        print_result(opportunity, minimum, pair, quote_asset)
        for error in errors:
            print(f"  warning: {error}")

    return len(successful_exchanges)


async def run(config: dict[str, Any], cycles: int | None, no_db: bool) -> int:
    interval = float(config["scan_interval_seconds"])
    db_path = ROOT / str(config.get("sqlite_path", "paper_arbitrage_usdt.db"))
    connection = None if no_db else init_db(db_path)
    quote_asset = str(config["capital_asset"]).upper()

    supports, discovery_errors = await discover()
    markets = support_map(supports)
    pair_venues = {
        pair: [name for name, pairs in markets.items() if pair in pairs]
        for pair in config["pairs"]
    }

    print("Crypto Arbitrage Scanner V2 — READ ONLY / PAPER TRADING")
    print(f"Virtual capital: {quote_asset} {float(config['virtual_capital']):.2f}")
    print(f"Minimum paper profit: {quote_asset} {float(config['minimum_net_profit']):.2f}")
    print(f"SQLite journal: {'disabled' if no_db else db_path}")
    print("No API keys. No orders. No withdrawals.")
    print("Discovered venues:")
    for pair, venues in pair_venues.items():
        print(f"  {pair}: {', '.join(venues) if venues else 'none'}")
    for error in discovery_errors:
        print(f"  discovery warning: {error}")
    print()

    headers = {
        "User-Agent": "Police2026-Crypto-Arbitrage-Scanner/2.0",
        "Accept": "application/json",
    }
    completed = 0
    last_successful_exchanges = 0
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            while cycles is None or completed < cycles:
                last_successful_exchanges = await scan_once(client, config, connection, markets)
                completed += 1
                if cycles is not None and completed >= cycles:
                    break
                print()
                await asyncio.sleep(interval)
    finally:
        if connection is not None:
            connection.close()

    return 0 if last_successful_exchanges >= 2 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only cross-exchange arbitrage paper scanner")
    parser.add_argument("--once", action="store_true", help="Run one market snapshot and exit")
    parser.add_argument("--cycles", type=int, help="Run N market-snapshot cycles and exit")
    parser.add_argument("--no-db", action="store_true", help="Do not write the SQLite paper journal")
    parser.add_argument("--config", type=Path, help="Path to a JSON config file")
    args = parser.parse_args()
    if args.once and args.cycles is not None:
        parser.error("use either --once or --cycles, not both")
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be >= 1")
    return args


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    cycles = 1 if args.once else args.cycles
    try:
        return asyncio.run(run(config, cycles=cycles, no_db=args.no_db))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
