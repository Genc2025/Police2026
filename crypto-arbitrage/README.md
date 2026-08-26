# Crypto Arbitrage Scanner

Read-only arbitrage research system using public market data. **No API keys, deposits, real orders, withdrawals, or leverage.**

## Current default

- Quote/capital asset: **USDT**
- Paper capital: **100 USDT**
- Configured assets: BTC, ETH, SOL, XRP, ADA, LINK, DOGE, AVAX against USDT
- Exchanges: Binance, Kraken, Coinbase Exchange, Bitstamp
- Live market discovery filters unsupported pairs per venue before scanning
- Cross-exchange REST order-book scanner
- Realtime WebSocket scanner for Binance, Kraken and Coinbase
- Binance triangular-arbitrage paper engine starting and ending in USDT
- SQLite evidence journal and combined report
- Unit tests + GitHub Actions live smoke tests

EUR is no longer required. The code is quote-asset aware, so EUR can still be used later through configuration if desired.

## Cost and liquidity model

The paper engines use executable best bid/ask and observed top-of-book quantity rather than last-trade price. A route is rejected when the full configured notional does not fit available top-of-book liquidity.

The default paper model also subtracts configurable taker-fee assumptions, per-leg slippage allowance, a safety buffer, quote-latency limits, and snapshot-skew limits. These assumptions are intentionally conservative and are not a guarantee of real execution quality.

## Important capital note

`virtual_capital` is the paper notional used to evaluate one route. Cross-exchange arbitrage still requires inventory to be pre-funded on the relevant venues in a real implementation. A 100 USDT paper route therefore does **not** mean a 100 USDT total real portfolio could necessarily execute both legs simultaneously.

Triangular arbitrage is different because all three legs occur on one exchange and the same starting inventory can rotate through the cycle; execution, fee, minimum-order, precision, and partial-fill risks still remain.

## Run

```bash
cd crypto-arbitrage
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

Discover live USDT support:

```bash
python market_probe.py --quote USDT --assets BTC ETH SOL XRP ADA LINK DOGE AVAX
```

Realtime WebSocket paper scan:

```bash
python realtime.py --seconds 30
```

Cross-exchange REST paper scan:

```bash
python scanner.py --cycles 10
```

Binance triangular paper scan:

```bash
python triangular.py --cycles 10
```

Combined report:

```bash
python report.py
```

Unit tests:

```bash
pytest -q tests
```

## Configuration

Copy `config.example.json` to `config.json` to change `capital_asset`, `virtual_capital`, fee assumptions, slippage, safety buffer, pairs, scan interval, SQLite path, or minimum paper profit.

The fee values in the example config are **paper-testing assumptions**, not verified personal account fee tiers. Before any live-trading version, actual account-specific fees and exchange trading rules must be fetched and validated.

## Gates before real money

1. Sustained realtime paper evidence over materially longer periods.
2. Multi-level order-book fill simulation rather than only top-of-book capacity.
3. Exchange trading filters: min notional, quantity step, price tick and asset precision.
4. Actual account fee tiers and fee-token effects where applicable.
5. Inventory/rebalancing and transfer-cost model for cross-exchange routes.
6. Paper execution state machine for simultaneous legs, partial fills and unwind risk.
7. Kill switches, maximum-loss controls, stale-feed handling and exchange disconnect handling.
8. Only after those gates: optional trade-only API integration with withdrawals disabled.
