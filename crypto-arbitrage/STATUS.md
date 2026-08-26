# Status

- Branch: `crypto-arbitrage-v1`
- Mode: read-only / paper trading only
- Real trading: disabled
- API keys: not used
- Default capital asset: USDT
- Default paper trade notional: 100 USDT
- Configured pairs: BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT, ADA/USDT, LINK/USDT, DOGE/USDT, AVAX/USDT
- Exchanges: Binance, Kraken, Coinbase Exchange, Bitstamp
- Market discovery: live public metadata before scans
- Cross-exchange: REST order-book engine with fees, slippage, safety buffer, liquidity, latency and snapshot-skew gates
- Realtime: Binance + Kraken + Coinbase public WebSocket engine with stale-quote rejection
- Triangular: Binance USDT three-leg paper engine
- Persistence: `paper_arbitrage_usdt.db`
- Reporting: combined cross-exchange + triangular report
- Tests: deterministic scanner, realtime parser and triangular engine tests
- CI: unit tests + live USDT discovery + realtime WebSocket smoke + REST paper sample + triangular sample + artifact

## Still blocked from real money

Do not enable real trading yet. Remaining gates include sustained realtime paper evidence, multi-level order-book fill simulation, exchange min-notional/step/tick rules, actual account fee tiers, inventory/rebalancing costs, simultaneous-leg/partial-fill controls and hard kill switches.
