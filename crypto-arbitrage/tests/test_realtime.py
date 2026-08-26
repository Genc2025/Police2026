from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime import RealtimeScanner, parse_binance, parse_coinbase, parse_kraken


def config():
    return {
        "virtual_capital_eur": 100.0,
        "slippage_pct_each_leg": 0.0,
        "safety_buffer_pct": 0.0,
        "minimum_net_profit_eur": 0.10,
        "max_quote_latency_ms": 1500.0,
        "max_snapshot_skew_ms": 1000.0,
        "fees_pct": {
            "binance": 0.0,
            "kraken": 0.0,
            "coinbase": 0.0,
            "bitstamp": 0.0,
        },
    }


def test_parse_binance_book_ticker():
    quote = parse_binance({
        "stream": "btceur@bookTicker",
        "data": {"s": "BTCEUR", "b": "99.0", "B": "2", "a": "100.0", "A": "3"},
    })
    assert quote is not None
    assert quote.exchange == "binance"
    assert quote.pair == "BTC/EUR"
    assert quote.bid == 99.0
    assert quote.ask_qty == 3.0


def test_parse_kraken_ticker_snapshot():
    quotes = parse_kraken({
        "channel": "ticker",
        "type": "snapshot",
        "data": [{
            "symbol": "ETH/EUR",
            "bid": 99.0,
            "bid_qty": 4.0,
            "ask": 100.0,
            "ask_qty": 5.0,
        }],
    })
    assert len(quotes) == 1
    assert quotes[0].exchange == "kraken"
    assert quotes[0].pair == "ETH/EUR"


def test_parse_coinbase_ticker():
    quote = parse_coinbase({
        "type": "ticker",
        "product_id": "BTC-EUR",
        "best_bid": "99.0",
        "best_bid_size": "2.0",
        "best_ask": "100.0",
        "best_ask_size": "3.0",
    })
    assert quote is not None
    assert quote.exchange == "coinbase"
    assert quote.bid_qty == 2.0
    assert quote.ask == 100.0


def test_realtime_scanner_evaluates_when_two_fresh_venues_exist():
    scanner = RealtimeScanner(config(), max_age_ms=5000.0)
    first = parse_binance({"data": {"s": "BTCEUR", "b": "99", "B": "10", "a": "100", "A": "10"}})
    second = parse_coinbase({
        "type": "ticker",
        "product_id": "BTC-EUR",
        "best_bid": "101",
        "best_bid_size": "10",
        "best_ask": "102",
        "best_ask_size": "10",
    })
    assert first is not None and second is not None
    scanner.ingest(first)
    scanner.ingest(second)
    assert scanner.evaluations >= 1
    assert scanner.paper_opportunities >= 1
    assert scanner.best_net_eur is not None and scanner.best_net_eur > 0
