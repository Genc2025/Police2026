from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import websockets

from market_probe import discover
from scanner import Quote, evaluate, load_config, support_map


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class LiveQuote:
    quote: Quote
    received_monotonic: float


def make_quote(exchange: str, pair: str, bid: Any, bid_qty: Any, ask: Any, ask_qty: Any) -> Quote:
    return Quote(
        exchange=exchange,
        pair=pair,
        bid=float(bid),
        bid_qty=float(bid_qty),
        ask=float(ask),
        ask_qty=float(ask_qty),
        observed_at=utc_now(),
        latency_ms=0.0,
    )


def parse_binance(message: dict[str, Any], symbol_to_pair: dict[str, str]) -> Quote | None:
    data = message.get("data", message)
    symbol = str(data.get("s", "")).upper()
    pair = symbol_to_pair.get(symbol)
    if not pair or not all(key in data for key in ("b", "B", "a", "A")):
        return None
    return make_quote("binance", pair, data["b"], data["B"], data["a"], data["A"])


def parse_kraken(message: dict[str, Any], allowed_pairs: set[str]) -> list[Quote]:
    if message.get("channel") != "ticker" or not isinstance(message.get("data"), list):
        return []
    quotes: list[Quote] = []
    for data in message["data"]:
        pair = str(data.get("symbol", ""))
        if pair not in allowed_pairs:
            continue
        required = ("bid", "bid_qty", "ask", "ask_qty")
        if all(key in data for key in required):
            quotes.append(make_quote("kraken", pair, data["bid"], data["bid_qty"], data["ask"], data["ask_qty"]))
    return quotes


def parse_coinbase(message: dict[str, Any], product_to_pair: dict[str, str]) -> Quote | None:
    if message.get("type") != "ticker":
        return None
    pair = product_to_pair.get(str(message.get("product_id", "")))
    if not pair:
        return None
    required = ("best_bid", "best_bid_size", "best_ask", "best_ask_size")
    if not all(key in message for key in required):
        return None
    return make_quote(
        "coinbase",
        pair,
        message["best_bid"],
        message["best_bid_size"],
        message["best_ask"],
        message["best_ask_size"],
    )


class RealtimeScanner:
    def __init__(self, config: dict[str, Any], max_age_ms: float = 1200.0) -> None:
        self.config = config
        self.max_age_ms = max_age_ms
        self.quotes: dict[tuple[str, str], LiveQuote] = {}
        self.updates = 0
        self.evaluations = 0
        self.paper_opportunities = 0
        self.best_net_quote: float | None = None
        self.best_description: str | None = None
        self.last_print: dict[str, float] = {}

    def ingest(self, quote: Quote) -> None:
        if min(quote.bid, quote.bid_qty, quote.ask, quote.ask_qty) <= 0:
            return
        self.quotes[(quote.exchange, quote.pair)] = LiveQuote(quote, time.monotonic())
        self.updates += 1
        self.evaluate_pair(quote.pair)

    def fresh_quotes(self, pair: str) -> list[Quote]:
        now = time.monotonic()
        return [
            live.quote
            for live in self.quotes.values()
            if live.quote.pair == pair and (now - live.received_monotonic) * 1000.0 <= self.max_age_ms
        ]

    def evaluate_pair(self, pair: str) -> None:
        quotes = self.fresh_quotes(pair)
        if len(quotes) < 2:
            return
        opportunity = evaluate(quotes, self.config)
        if opportunity is None:
            return
        self.evaluations += 1
        minimum = float(self.config["minimum_net_profit"])
        quote_asset = str(self.config["capital_asset"]).upper()
        if self.best_net_quote is None or opportunity.net_profit_quote > self.best_net_quote:
            self.best_net_quote = opportunity.net_profit_quote
            self.best_description = (
                f"{pair} {opportunity.buy_exchange}->{opportunity.sell_exchange} "
                f"gross={opportunity.gross_spread_pct:+.4f}% "
                f"net={quote_asset} {opportunity.net_profit_quote:+.4f}"
            )
        if opportunity.net_profit_quote >= minimum:
            self.paper_opportunities += 1
            now = time.monotonic()
            if now - self.last_print.get(pair, 0.0) >= 0.25:
                self.last_print[pair] = now
                print(
                    "REALTIME PAPER OPPORTUNITY | "
                    f"{pair} BUY {opportunity.buy_exchange} {opportunity.buy_ask:.8f} -> "
                    f"SELL {opportunity.sell_exchange} {opportunity.sell_bid:.8f} | "
                    f"gross {opportunity.gross_spread_pct:+.4f}% | "
                    f"net {quote_asset} {opportunity.net_profit_quote:+.4f}"
                )


def selected_pairs(config: dict[str, Any], supported: Iterable[str]) -> list[str]:
    allowed = set(supported)
    return [pair for pair in config["pairs"] if pair in allowed]


async def binance_feed(scanner: RealtimeScanner, stop: asyncio.Event, pairs: list[str]) -> None:
    symbol_to_pair = {pair.replace("/", "").upper(): pair for pair in pairs}
    streams = "/".join(f"{symbol.lower()}@bookTicker" for symbol in symbol_to_pair)
    if not streams:
        return
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=8, close_timeout=2) as ws:
                async for raw in ws:
                    quote = parse_binance(json.loads(raw), symbol_to_pair)
                    if quote:
                        scanner.ingest(quote)
                    if stop.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"binance websocket warning: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1)


async def kraken_feed(scanner: RealtimeScanner, stop: asyncio.Event, pairs: list[str]) -> None:
    if not pairs:
        return
    url = "wss://ws.kraken.com/v2"
    allowed_pairs = set(pairs)
    subscribe = {
        "method": "subscribe",
        "params": {
            "channel": "ticker",
            "symbol": pairs,
            "event_trigger": "bbo",
            "snapshot": True,
        },
    }
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=8, close_timeout=2) as ws:
                await ws.send(json.dumps(subscribe))
                async for raw in ws:
                    for quote in parse_kraken(json.loads(raw), allowed_pairs):
                        scanner.ingest(quote)
                    if stop.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"kraken websocket warning: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1)


async def coinbase_feed(scanner: RealtimeScanner, stop: asyncio.Event, pairs: list[str]) -> None:
    product_to_pair = {pair.replace("/", "-"): pair for pair in pairs}
    if not product_to_pair:
        return
    url = "wss://ws-feed.exchange.coinbase.com"
    subscribe = {
        "type": "subscribe",
        "product_ids": list(product_to_pair),
        "channels": ["ticker"],
    }
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=8, close_timeout=2) as ws:
                await ws.send(json.dumps(subscribe))
                async for raw in ws:
                    quote = parse_coinbase(json.loads(raw), product_to_pair)
                    if quote:
                        scanner.ingest(quote)
                    if stop.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"coinbase websocket warning: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1)


async def run(config: dict[str, Any], seconds: float, max_age_ms: float) -> int:
    supports, discovery_errors = await discover()
    markets = support_map(supports)
    feed_pairs = {
        exchange: selected_pairs(config, markets.get(exchange, frozenset()))
        for exchange in ("binance", "kraken", "coinbase")
    }

    scanner = RealtimeScanner(config, max_age_ms=max_age_ms)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(binance_feed(scanner, stop, feed_pairs["binance"])),
        asyncio.create_task(kraken_feed(scanner, stop, feed_pairs["kraken"])),
        asyncio.create_task(coinbase_feed(scanner, stop, feed_pairs["coinbase"])),
    ]
    quote_asset = str(config["capital_asset"]).upper()
    print("Realtime Arbitrage V3 — PUBLIC WEBSOCKETS / PAPER ONLY")
    print(f"Capital: {quote_asset} {float(config['virtual_capital']):.2f}")
    print(f"Duration: {seconds:.1f}s | max quote age: {max_age_ms:.0f}ms")
    for exchange, pairs in feed_pairs.items():
        print(f"{exchange:8}: {', '.join(pairs) if pairs else 'no configured supported pairs'}")
    for error in discovery_errors:
        print(f"discovery warning: {error}")
    print("No API keys. No orders. No withdrawals.\n")

    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    connected = sorted({exchange for exchange, _pair in scanner.quotes})
    observed_pairs = sorted({pair for _exchange, pair in scanner.quotes})
    print("\nRealtime summary")
    print(f"Feeds observed:       {', '.join(connected) if connected else 'none'}")
    print(f"Pairs observed:       {', '.join(observed_pairs) if observed_pairs else 'none'}")
    print(f"Quote updates:        {scanner.updates}")
    print(f"Route evaluations:    {scanner.evaluations}")
    print(f"Qualifying events:    {scanner.paper_opportunities}")
    print(f"Best observed:        {scanner.best_description or 'none'}")
    return 0 if len(connected) >= 2 and scanner.evaluations > 0 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Realtime public-WebSocket arbitrage paper scanner")
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--max-age-ms", type=float, default=1200.0)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be > 0")
    if args.max_age_ms <= 0:
        parser.error("--max-age-ms must be > 0")
    return args


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    try:
        return asyncio.run(run(config, args.seconds, args.max_age_ms))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
