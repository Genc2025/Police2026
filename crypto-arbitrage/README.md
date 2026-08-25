# Crypto Arbitrage Scanner V1

Read-only cross-exchange scanner using public market data. **No API keys, deposits, real orders, withdrawals, or leverage.**

## Current scope

- Exchanges: Binance, Kraken, Coinbase Exchange, Bitstamp
- Pairs: BTC/EUR, ETH/EUR
- Uses executable top-of-book bid/ask **and available quantity**, not last-trade price
- Requires the full configured paper notional to fit on both legs
- Applies configurable taker-fee assumptions, slippage allowance, and safety buffer
- Saves quote snapshots and best routes to SQLite
- Reports simulated opportunities only when estimated net profit reaches the configured threshold
- Unit tests + GitHub Actions live public-market smoke test

## Important capital note

`virtual_capital_eur` is the **paper notional for one arbitrage route**, not a claim that EUR 100 total portfolio can execute EUR 100 simultaneously on every venue.

Real cross-exchange arbitrage normally requires pre-funded inventory on both sides (for example EUR on the buy venue and the base crypto on the sell venue). If the total real portfolio is only EUR 100, the eventual live trade notional would normally be smaller and split across venues. V1 intentionally does not model deposits, transfers, rebalancing, or real balances yet.

## Safety

V1 cannot place trades and does not accept exchange credentials. A positive paper result is not guaranteed live profit: quotes can move before execution, fees differ by account tier, partial fills can occur, and exchange/API latency matters.

## Run

```bash
cd crypto-arbitrage
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python scanner.py
```

One live snapshot without writing SQLite:

```bash
python scanner.py --once --no-db
```

One live snapshot with the paper journal:

```bash
python scanner.py --once
python report.py
```

Run unit tests:

```bash
pytest -q tests
```

## Configuration

Copy `config.example.json` to `config.json` to change paper notional, fee assumptions, buffer, pairs, scan interval, SQLite path, or minimum paper profit.

The fee values in the example config are **paper-testing assumptions**. Before any real-trading version, they must be replaced with the actual fee tier for each funded account and verified again.

## Next engineering gates before live trading

1. Sustained paper run and measured opportunity frequency.
2. WebSocket market-data adapters with stale-quote/latency rejection.
3. Multi-level order-book fill simulation and rebalancing costs.
4. Per-exchange real fee-tier discovery after account connection.
5. Paper execution state machine for simultaneous legs and partial-fill handling.
6. Only after those gates: optional trade-only API integration with withdrawals disabled.
