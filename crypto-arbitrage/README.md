# Crypto Arbitrage Scanner V1

Read-only scanner for public market data. No API keys, no deposits, no real orders.

Initial scope:
- Exchanges: Binance, Kraken, Coinbase Exchange, Bitstamp
- Pairs: BTC/EUR, ETH/EUR
- Virtual capital: EUR 100
- Compares executable top-of-book prices (ask to buy, bid to sell)
- Applies configurable taker fees, slippage estimate, and safety buffer
- Reports only simulated net opportunities

## Safety

This version cannot place trades and does not accept exchange credentials.

## Run

```bash
cd crypto-arbitrage
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python scanner.py
```

## Configuration

Copy `config.example.json` to `config.json` if you want to change virtual capital, fee assumptions, buffer, symbols, or scan interval.

Fee values are assumptions for paper testing and must be replaced with the user's actual exchange fee tier before any live-trading version is considered.
