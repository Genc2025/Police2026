from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scanner import Quote, evaluate, symbol_for


def config(**overrides):
    value = {
        "capital_asset": "USDT",
        "virtual_capital": 100.0,
        "scan_interval_seconds": 3.0,
        "slippage_pct_each_leg": 0.05,
        "safety_buffer_pct": 0.05,
        "minimum_net_profit": 0.10,
        "max_quote_latency_ms": 1500.0,
        "max_snapshot_skew_ms": 1000.0,
        "pairs": ["BTC/USDT"],
        "fees_pct": {
            "binance": 0.10,
            "kraken": 0.40,
            "coinbase": 0.60,
            "bitstamp": 0.30,
        },
    }
    value.update(overrides)
    return value


def quote(exchange, bid, bid_qty, ask, ask_qty, *, observed_at="2026-08-26T00:00:00.000+00:00", latency_ms=10.0):
    return Quote(exchange, "BTC/USDT", bid, bid_qty, ask, ask_qty, observed_at, latency_ms)


def test_symbol_translation_for_usdt_markets():
    assert symbol_for("binance", "BTC/USDT") == "BTCUSDT"
    assert symbol_for("kraken", "BTC/USDT") == "XBTUSDT"
    assert symbol_for("kraken", "DOGE/USDT") == "XDGUSDT"
    assert symbol_for("coinbase", "ETH/USDT") == "ETH-USDT"
    assert symbol_for("bitstamp", "XRP/USDT") == "xrpusdt"


def test_selects_best_cross_exchange_route():
    quotes = [
        quote("binance", 99.8, 10, 100.0, 10),
        quote("kraken", 101.8, 10, 102.0, 10),
        quote("coinbase", 100.4, 10, 100.6, 10),
    ]
    result = evaluate(quotes, config())
    assert result is not None
    assert result.buy_exchange == "binance"
    assert result.sell_exchange == "kraken"
    assert result.quote_asset == "USDT"
    assert result.net_profit_quote > 0


def test_fees_can_turn_gross_spread_negative_net():
    quotes = [
        quote("binance", 99.9, 10, 100.0, 10),
        quote("kraken", 100.5, 10, 100.7, 10),
    ]
    result = evaluate(quotes, config())
    assert result is not None
    assert result.gross_spread_pct > 0
    assert result.net_profit_quote < 0


def test_requires_full_top_of_book_liquidity_in_either_direction():
    quotes = [
        quote("binance", 99.8, 0.10, 100.0, 0.10),
        quote("kraken", 110.0, 0.10, 110.2, 0.10),
    ]
    result = evaluate(quotes, config())
    assert result is None


def test_never_compares_exchange_with_itself():
    quotes = [quote("binance", 105.0, 10, 100.0, 10)]
    assert evaluate(quotes, config()) is None


def test_higher_safety_buffer_reduces_net_profit():
    quotes = [
        quote("binance", 99.8, 10, 100.0, 10),
        quote("kraken", 102.0, 10, 102.2, 10),
    ]
    low = evaluate(quotes, config(safety_buffer_pct=0.01))
    high = evaluate(quotes, config(safety_buffer_pct=0.50))
    assert low is not None and high is not None
    assert low.net_profit_quote > high.net_profit_quote


def test_rejects_route_when_quote_latency_is_too_high():
    quotes = [
        quote("binance", 99.8, 10, 100.0, 10, latency_ms=2000.0),
        quote("kraken", 102.0, 10, 102.2, 10),
    ]
    assert evaluate(quotes, config(max_quote_latency_ms=1000.0)) is None


def test_rejects_route_when_snapshot_skew_is_too_high():
    quotes = [
        quote("binance", 99.8, 10, 100.0, 10, observed_at="2026-08-26T00:00:00.000+00:00"),
        quote("kraken", 102.0, 10, 102.2, 10, observed_at="2026-08-26T00:00:02.500+00:00"),
    ]
    assert evaluate(quotes, config(max_snapshot_skew_ms=1000.0)) is None
