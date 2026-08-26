from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class MarketSupport:
    exchange: str
    pairs: frozenset[str]
    warning: str | None = None


async def fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url, timeout=10.0, headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    return response.json()


async def binance_markets(client: httpx.AsyncClient) -> MarketSupport:
    data = await fetch_json(client, "https://data-api.binance.vision/api/v3/exchangeInfo")
    pairs = {
        f"{item['baseAsset']}/{item['quoteAsset']}"
        for item in data.get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("isSpotTradingAllowed", True)
        and item.get("baseAsset")
        and item.get("quoteAsset")
    }
    return MarketSupport("binance", frozenset(pairs))


async def kraken_markets(client: httpx.AsyncClient) -> MarketSupport:
    data = await fetch_json(client, "https://api.kraken.com/0/public/AssetPairs")
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    pairs: set[str] = set()
    for item in data.get("result", {}).values():
        wsname = item.get("wsname")
        if isinstance(wsname, str) and "/" in wsname:
            pairs.add(wsname)
    return MarketSupport("kraken", frozenset(pairs))


async def coinbase_markets(client: httpx.AsyncClient) -> MarketSupport:
    data = await fetch_json(client, "https://api.exchange.coinbase.com/products")
    pairs: set[str] = set()
    for item in data if isinstance(data, list) else []:
        base = item.get("base_currency")
        quote = item.get("quote_currency")
        # Exclude products explicitly marked offline/disabled when those fields are present.
        if not base or not quote:
            continue
        if item.get("status") not in (None, "online"):
            continue
        if item.get("trading_disabled") is True:
            continue
        pairs.add(f"{base}/{quote}")
    return MarketSupport("coinbase", frozenset(pairs))


async def bitstamp_markets(client: httpx.AsyncClient) -> MarketSupport:
    data = await fetch_json(client, "https://www.bitstamp.net/api/v2/markets/")
    pairs: set[str] = set()
    for item in data if isinstance(data, list) else []:
        base = item.get("base_currency") or item.get("base")
        quote = item.get("counter_currency") or item.get("quote_currency") or item.get("counter")
        if base and quote:
            pairs.add(f"{str(base).upper()}/{str(quote).upper()}")
            continue
        symbol = item.get("market_symbol") or item.get("url_symbol")
        # Keep parsing conservative: symbol-only rows aren't guessed into assets.
        if symbol:
            continue
    return MarketSupport("bitstamp", frozenset(pairs))


FETCHERS = (binance_markets, kraken_markets, coinbase_markets, bitstamp_markets)


async def discover(assets: list[str]) -> tuple[list[MarketSupport], list[str]]:
    headers = {
        "User-Agent": "Police2026-Crypto-Arbitrage-Market-Probe/1.0",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(*(fetcher(client) for fetcher in FETCHERS), return_exceptions=True)

    supports: list[MarketSupport] = []
    errors: list[str] = []
    for fetcher, result in zip(FETCHERS, results):
        name = fetcher.__name__.replace("_markets", "")
        if isinstance(result, Exception):
            errors.append(f"{name}: {type(result).__name__}: {result}")
        else:
            supports.append(result)
    return supports, errors


def print_matrix(assets: list[str], supports: list[MarketSupport]) -> None:
    by_exchange = {support.exchange: support.pairs for support in supports}
    exchanges = ["binance", "kraken", "coinbase", "bitstamp"]
    print("EUR spot market support (live discovery)")
    print("=" * 54)
    print(f"{'PAIR':10} " + " ".join(f"{name:10}" for name in exchanges) + " VENUES")
    for asset in assets:
        pair = f"{asset}/EUR"
        flags = [pair in by_exchange.get(name, frozenset()) for name in exchanges]
        cells = ["YES" if flag else "-" for flag in flags]
        print(f"{pair:10} " + " ".join(f"{cell:10}" for cell in cells) + f" {sum(flags)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover public EUR spot markets across target exchanges")
    parser.add_argument(
        "--assets",
        nargs="+",
        default=["BTC", "ETH", "SOL", "XRP", "ADA", "LINK"],
        help="Base assets to check against EUR",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assets = [asset.upper() for asset in args.assets]
    supports, errors = asyncio.run(discover(assets))
    print_matrix(assets, supports)
    for error in errors:
        print(f"warning: {error}")
    # At least two venues must be discoverable for cross-exchange work to be meaningful.
    return 0 if len(supports) >= 2 else 2


if __name__ == "__main__":
    raise SystemExit(main())
