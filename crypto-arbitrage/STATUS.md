# Status

- Branch: `crypto-arbitrage-v1`
- Mode: V1 read-only cross-exchange paper scanner
- Real trading: disabled
- API keys: not used
- Default paper trade notional: EUR 100
- Exchanges: Binance, Kraken, Coinbase Exchange, Bitstamp
- Pairs: BTC/EUR, ETH/EUR
- Pricing: public L2/L1 order-book best bid/ask + quantity
- Cost model: configured taker fees + per-leg slippage allowance + safety buffer
- Liquidity gate: full paper notional must fit observed top-of-book on buy and sell legs
- Persistence: SQLite quote + opportunity journal implemented
- Reporting: `report.py` implemented
- Tests: deterministic engine tests implemented
- CI: GitHub Actions unit-test + live public-market smoke workflow implemented

## Still blocked from real money

Do not enable real trading yet. Remaining gates include sustained paper evidence, WebSocket/stale-quote controls, deeper fill simulation, actual account fee tiers, inventory/rebalancing model, and simultaneous-leg/partial-fill risk controls.
