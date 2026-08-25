from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.example.json"
USER_CONFIG = ROOT / "config.json"


@dataclass(frozen=True)
class Quote:
    exchange: str
    pair: str
    bid: float
    ask: float


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


async def fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url, timeout=8.0)
    response.raise_for_status()
    return response.json()


async def binance_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["binance"]
    data = await fetch_json(
        client,
        f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}",
    )
    return Quote("binance", pair, float(data["bidPrice"]), float(data["askPrice"]))


async def kraken_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["kraken"]
    data = await fetch_json(
        client,
        f"https://api.kraken.com/0/public/Ticker?pair={symbol}",
    )
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    payload = next(iter(data["result"].values()))
    return Quote("kraken", pair, float(payload["b"][0]), float(payload["a"][0]))


async def coinbase_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    product = SYMBOLS[pair]["coinbase"]
    data = await fetch_json(
        client,
        f"https://api.exchange.coinbase.com/products/{product}/ticker",
    )
    return Quote("coinbase", pair, float(data["bid"]), float(data["ask"]))


async def bitstamp_quote(client: httpx.AsyncClient, pair: str) -> Quote:
    symbol = SYMBOLS[pair]["bitstamp"]
    data = await fetch_json(
        client,
        f"https://www.bitstamp.net/api/v2/ticker/{symbol}/",
    )
    return Quote("bitstamp", pair, float(data["bid"]), float(data["ask"]))


FETCHERS = {
    "binance": binance_quote,
    "kraken": kraken_quote,
    "coinbase": coinbase_quote,
    "bitstamp": bitstamp_quote,
}


def load_config() -> dict[str, Any]:
    path = USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(quotes: list[Quote], config: dict[str, Any]) -> dict[str, Any]:
    capital = float(config["virtual_capital_eur"])
    fees = config["fees_pct"]
    slip_each = float(config["slippage_pct_each_leg"])
    buffer_pct = float(config["safety_buffer_pct"])

    best: dict[str, Any] | None = None

    for buy in quotes:
        for sell in quotes:
            if buy.exchange == sell.exchange:
                continue

            gross_pct = ((sell.bid / buy.ask) - 1.0) * 100.0
            estimated_cost_pct = (
                float(fees[buy.exchange])
                + float(fees[sell.exchange])
                + (2.0 * slip_each)
                + buffer_pct
            )
            net_pct = gross_pct - estimated_cost_pct
            net_eur = capital * (net_pct / 100.0)

            candidate = {
                "pair": buy.pair,
                "buy_exchange": buy.exchange,
                "buy_ask": buy.ask,
                "sell_exchange": sell.exchange,
                "sell_bid": sell.bid,
                "gross_pct": gross_pct,
                "estimated_cost_pct": estimated_cost_pct,
                "net_pct": net_pct,
                "net_eur": net_eur,
            }
            if best is None or candidate["net_eur"] > best["net_eur"]:
                best = candidate

    if best is None:
        raise RuntimeError("No cross-exchange comparison available")
    return best


async def collect_pair(client: httpx.AsyncClient, pair: str) -> tuple[list[Quote], list[str]]:
    tasks = [FETCHERS[name](client, pair) for name in FETCHERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    quotes: list[Quote] = []
    errors: list[str] = []

    for name, result in zip(FETCHERS, results):
        if isinstance(result, Exception):
            errors.append(f"{name}: {result}")
        else:
            quotes.append(result)
    return quotes, errors


def print_result(result: dict[str, Any], minimum: float) -> None:
    status = "PAPER OPPORTUNITY" if result["net_eur"] >= minimum else "NO TRADE"
    print(
        f"{result['pair']:7} | BUY {result['buy_exchange']:9} {result['buy_ask']:.4f} | "
        f"SELL {result['sell_exchange']:9} {result['sell_bid']:.4f} | "
        f"gross {result['gross_pct']:+.4f}% | costs {result['estimated_cost_pct']:.4f}% | "
        f"net {result['net_pct']:+.4f}% = EUR {result['net_eur']:+.4f} | {status}"
    )


async def main() -> None:
    config = load_config()
    interval = float(config["scan_interval_seconds"])
    minimum = float(config["minimum_net_profit_eur"])

    print("Crypto Arbitrage Scanner V1 — READ ONLY / PAPER TRADING")
    print(f"Virtual capital: EUR {float(config['virtual_capital_eur']):.2f}")
    print("Press Ctrl+C to stop.\n")

    headers = {"User-Agent": "Police2026-Crypto-Arbitrage-Scanner/1.0"}
    async with httpx.AsyncClient(headers=headers) as client:
        while True:
            for pair in config["pairs"]:
                quotes, errors = await collect_pair(client, pair)
                if len(quotes) >= 2:
                    print_result(evaluate(quotes, config), minimum)
                else:
                    print(f"{pair}: insufficient live quotes")
                for error in errors:
                    print(f"  warning: {error}")
            print()
            await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
