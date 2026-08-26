from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from triangular import Edge, build_edges, find_best_triangle


def config(**tri_overrides):
    triangular = {
        "start_asset": "USDT",
        "virtual_capital": 100.0,
        "fee_pct": 0.10,
        "slippage_pct_each_leg": 0.05,
        "safety_buffer_pct": 0.05,
        "minimum_net_profit": 0.10,
        "max_market_data_latency_ms": 1500.0,
    }
    triangular.update(tri_overrides)
    return {"triangular": triangular}


def profitable_edges():
    return [
        Edge("USDT", "BTC", "BTCUSDT", "BUY", 100.0, 1000.0),
        Edge("BTC", "USDT", "BTCUSDT", "SELL", 99.0, 1000.0),
        Edge("BTC", "ETH", "ETHBTC", "BUY", 0.5, 1000.0),
        Edge("ETH", "BTC", "ETHBTC", "SELL", 0.49, 1000.0),
        Edge("ETH", "USDT", "ETHUSDT", "SELL", 60.0, 1000.0),
        Edge("USDT", "ETH", "ETHUSDT", "BUY", 61.0, 1000.0),
    ]


def test_finds_profitable_three_leg_cycle():
    result = find_best_triangle(profitable_edges(), config(), latency_ms=50.0)
    assert result is not None
    assert result.path == "USDT->BTC->ETH->USDT"
    assert result.start_asset == "USDT"
    assert result.net_profit_quote > 0
    assert result.gross_profit_pct > result.net_profit_pct


def test_rejects_stale_market_snapshot():
    result = find_best_triangle(
        profitable_edges(),
        config(max_market_data_latency_ms=100.0),
        latency_ms=250.0,
    )
    assert result is None


def test_rejects_triangle_without_enough_top_book_capacity():
    edges = profitable_edges()
    edges[0] = Edge("USDT", "BTC", "BTCUSDT", "BUY", 100.0, 50.0)
    result = find_best_triangle(edges, config(), latency_ms=50.0)
    assert result is not None
    assert result.path != "USDT->BTC->ETH->USDT"


def test_fees_can_remove_apparent_gross_profit():
    edges = [
        Edge("USDT", "BTC", "BTCUSDT", "BUY", 100.0, 1000.0),
        Edge("BTC", "ETH", "ETHBTC", "BUY", 0.5, 1000.0),
        Edge("ETH", "USDT", "ETHUSDT", "SELL", 50.2, 1000.0),
    ]
    result = find_best_triangle(
        edges,
        config(fee_pct=0.20, slippage_pct_each_leg=0.05, safety_buffer_pct=0.05),
        latency_ms=50.0,
    )
    assert result is not None
    assert result.gross_profit_pct > 0
    assert result.net_profit_quote < 0


def test_build_edges_creates_buy_and_sell_directions():
    tickers = [{
        "symbol": "BTCUSDT",
        "bidPrice": "99.0",
        "bidQty": "2.0",
        "askPrice": "100.0",
        "askQty": "3.0",
    }]
    symbols = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
        }
    }
    edges = build_edges(tickers, symbols)
    sell = next(edge for edge in edges if edge.side == "SELL")
    buy = next(edge for edge in edges if edge.side == "BUY")
    assert (sell.src, sell.dst, sell.max_input) == ("BTC", "USDT", 2.0)
    assert (buy.src, buy.dst, buy.max_input) == ("USDT", "BTC", 300.0)
