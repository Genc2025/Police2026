# Architecture V1

1. Fetch public best bid/ask quotes from each exchange.
2. Normalize pairs to BTC/EUR and ETH/EUR.
3. Compare every buy-exchange ask against every sell-exchange bid.
4. Subtract configured taker fees, estimated slippage on both legs, and a safety buffer.
5. Mark an opportunity only when simulated net EUR profit exceeds the configured threshold.

V1 deliberately does not place orders, store credentials, or move funds.
