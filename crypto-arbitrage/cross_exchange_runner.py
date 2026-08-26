import asyncio
import copy
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets

STATE_PATH = Path(__file__).with_name("headless_state.json")
BRANCH = os.getenv("GITHUB_REF_NAME", "claude/html-demo-rn-app-jz85o7")
RUNTIME_SECONDS = int(os.getenv("PAPER_RUNTIME_SECONDS", "20400"))
SAVE_INTERVAL = int(os.getenv("PAPER_SAVE_INTERVAL_SECONDS", "300"))
MAX_NOTIONAL = float(os.getenv("PAPER_MAX_NOTIONAL", "100"))
MIN_SIGNAL = float(os.getenv("PAPER_MIN_SIGNAL", "0.01"))
MIN_FILL = float(os.getenv("PAPER_MIN_FILL", "0.005"))
EXEC_DELAY = float(os.getenv("PAPER_EXEC_DELAY", "0.350"))
MAX_QUOTE_AGE = float(os.getenv("PAPER_MAX_QUOTE_AGE", "2.0"))
MAX_SKEW = float(os.getenv("PAPER_MAX_SKEW", "0.75"))
REARM_NET = 0.0

FEES = {
    "binance": 0.0010,
    "kraken": 0.0040,
    "coinbase": 0.0060,
    "bitstamp": 0.0030,
}

ASSETS = [
    "BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "DOGE", "AVAX", "BCH",
    "LTC", "DOT", "UNI", "AAVE", "NEAR", "ATOM", "SUI", "SHIB", "HBAR",
]
PAIRS = [f"{a}/USDT" for a in ASSETS]

quotes = {e: {} for e in FEES}
feed_updates = {e: 0 for e in FEES}
armed = {}
execution_lock = asyncio.Lock()
publish_lock = asyncio.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cross-exchange-paper-research/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def load_state():
    if STATE_PATH.exists():
        try:
            d = json.loads(STATE_PATH.read_text())
            bal = float(d.get("balance", 100.0))
            hist = d.get("history", []) if isinstance(d.get("history", []), list) else []
            return {"balance": bal if bal > 0 else 100.0, "history": hist[:300]}
        except Exception:
            pass
    return {"balance": 100.0, "history": []}


def write_state(snapshot):
    snapshot["updated_at"] = utcnow()
    STATE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def git_publish_snapshot(snapshot):
    write_state(snapshot)
    subprocess.run(["git", "add", str(STATE_PATH)], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", "Update cross-exchange paper state [skip ci]"], check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], check=True)
    subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"], check=True)


async def publish(state):
    async with publish_lock:
        snap = copy.deepcopy(state)
        try:
            await asyncio.to_thread(git_publish_snapshot, snap)
        except Exception as exc:
            state["last_error"] = f"state publish failed: {exc}"


def canonical_base(name):
    name = str(name).upper()
    return {"XBT": "BTC", "XDG": "DOGE"}.get(name, name)


def discover_markets():
    out = {e: set() for e in FEES}

    try:
        data = fetch_json("https://data-api.binance.vision/api/v3/exchangeInfo")
        for s in data.get("symbols", []):
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                p = f"{s.get('baseAsset')}/USDT"
                if p in PAIRS:
                    out["binance"].add(p)
    except Exception:
        out["binance"] = set(PAIRS)

    try:
        data = fetch_json("https://api.exchange.coinbase.com/products")
        for p in data:
            if p.get("quote_currency") == "USDT" and p.get("status", "online") == "online":
                pair = f"{p.get('base_currency')}/USDT"
                if pair in PAIRS:
                    out["coinbase"].add(pair)
    except Exception:
        pass

    try:
        data = fetch_json("https://www.bitstamp.net/api/v2/markets/")
        for m in data:
            base = canonical_base(m.get("base_currency", ""))
            quote = str(m.get("counter_currency", "")).upper()
            if quote == "USDT":
                pair = f"{base}/USDT"
                if pair in PAIRS:
                    out["bitstamp"].add(pair)
    except Exception:
        pass

    try:
        data = fetch_json("https://api.kraken.com/0/public/AssetPairs")
        for _, info in (data.get("result") or {}).items():
            ws = info.get("wsname") or ""
            if "/" not in ws:
                continue
            base, quote = ws.split("/", 1)
            base = canonical_base(base)
            quote = quote.upper()
            if quote == "USDT":
                pair = f"{base}/USDT"
                if pair in PAIRS:
                    out["kraken"].add(pair)
    except Exception:
        pass

    return out


def set_quote(exchange, pair, bid, bid_qty, ask, ask_qty):
    try:
        bid = float(bid); bid_qty = float(bid_qty); ask = float(ask); ask_qty = float(ask_qty)
    except Exception:
        return
    if bid <= 0 or ask <= 0 or bid_qty <= 0 or ask_qty <= 0 or ask < bid:
        return
    quotes[exchange][pair] = {
        "bid": bid, "bid_qty": bid_qty,
        "ask": ask, "ask_qty": ask_qty,
        "t": time.monotonic(),
    }
    feed_updates[exchange] += 1


async def binance_reader(pairs):
    if not pairs:
        return
    streams = "/".join(p.replace("/", "").lower() + "@bookTicker" for p in sorted(pairs))
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    d = msg.get("data", msg)
                    sym = str(d.get("s", "")).upper()
                    if not sym.endswith("USDT"):
                        continue
                    pair = sym[:-4] + "/USDT"
                    set_quote("binance", pair, d.get("b"), d.get("B"), d.get("a"), d.get("A"))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


async def coinbase_reader(pairs):
    if not pairs:
        return
    product_ids = [p.replace("/", "-") for p in sorted(pairs)]
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = {"type": "subscribe", "product_ids": product_ids, "channels": ["ticker"]}
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    d = json.loads(raw)
                    if d.get("type") != "ticker":
                        continue
                    pid = str(d.get("product_id", ""))
                    if not pid.endswith("-USDT"):
                        continue
                    pair = pid[:-5] + "/USDT"
                    bq = d.get("best_bid_size") or d.get("last_size")
                    aq = d.get("best_ask_size") or d.get("last_size")
                    set_quote("coinbase", pair, d.get("best_bid"), bq, d.get("best_ask"), aq)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


async def kraken_reader(pairs):
    if not pairs:
        return
    symbols = sorted(pairs)
    url = "wss://ws.kraken.com/v2"
    sub = {"method": "subscribe", "params": {"channel": "ticker", "symbol": symbols, "snapshot": True}}
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") != "ticker" or not isinstance(msg.get("data"), list):
                        continue
                    for d in msg["data"]:
                        pair = str(d.get("symbol", ""))
                        if pair not in pairs:
                            continue
                        set_quote("kraken", pair, d.get("bid"), d.get("bid_qty"), d.get("ask"), d.get("ask_qty"))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


async def bitstamp_reader(pairs):
    if not pairs:
        return
    url = "wss://ws.bitstamp.net"
    channel_to_pair = {"order_book_" + p.replace("/", "").lower(): p for p in pairs}
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                for ch in channel_to_pair:
                    await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": ch}}))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("event") != "data":
                        continue
                    ch = msg.get("channel")
                    pair = channel_to_pair.get(ch)
                    if not pair:
                        continue
                    d = msg.get("data") or {}
                    bids = d.get("bids") or []
                    asks = d.get("asks") or []
                    if not bids or not asks:
                        continue
                    set_quote("bitstamp", pair, bids[0][0], bids[0][1], asks[0][0], asks[0][1])
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


def route_calc(pair, buy_ex, sell_ex, notional, require_after=None):
    qb = quotes.get(buy_ex, {}).get(pair)
    qs = quotes.get(sell_ex, {}).get(pair)
    if not qb or not qs or buy_ex == sell_ex:
        return None
    now = time.monotonic()
    if now - qb["t"] > MAX_QUOTE_AGE or now - qs["t"] > MAX_QUOTE_AGE:
        return None
    if abs(qb["t"] - qs["t"]) > MAX_SKEW:
        return None
    if require_after is not None and (qb["t"] <= require_after or qs["t"] <= require_after):
        return None

    n = float(notional)
    if n <= 0:
        return None
    gross_base = n / qb["ask"]
    if gross_base > qb["ask_qty"]:
        return None
    bought_base = gross_base * (1.0 - FEES[buy_ex])
    if bought_base > qs["bid_qty"]:
        return None
    gross_sale = bought_base * qs["bid"]
    final_quote = gross_sale * (1.0 - FEES[sell_ex])
    net = final_quote - n
    return {
        "pair": pair,
        "buy_exchange": buy_ex,
        "sell_exchange": sell_ex,
        "buy_ask": qb["ask"],
        "sell_bid": qs["bid"],
        "buy_ask_qty": qb["ask_qty"],
        "sell_bid_qty": qs["bid_qty"],
        "notional": n,
        "gross_spread_pct": (qs["bid"] / qb["ask"] - 1.0) * 100.0,
        "buy_fee": FEES[buy_ex],
        "sell_fee": FEES[sell_ex],
        "final": final_quote,
        "net": net,
        "skew_ms": abs(qb["t"] - qs["t"]) * 1000.0,
    }


def all_routes(balance):
    n = min(MAX_NOTIONAL, float(balance))
    out = []
    for pair in PAIRS:
        for buy_ex in FEES:
            for sell_ex in FEES:
                if buy_ex == sell_ex:
                    continue
                r = route_calc(pair, buy_ex, sell_ex, n)
                if r:
                    out.append(r)
    return out


async def wait_for_post_signal_quotes(pair, buy_ex, sell_ex, signal_t, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qb = quotes.get(buy_ex, {}).get(pair)
        qs = quotes.get(sell_ex, {}).get(pair)
        if qb and qs and qb["t"] > signal_t and qs["t"] > signal_t:
            return True
        await asyncio.sleep(0.02)
    return False


async def execute_candidate(state, candidate):
    async with execution_lock:
        signal_t = time.monotonic()
        state["signals"] += 1
        state["last_signal"] = {
            "time": utcnow(),
            "pair": candidate["pair"],
            "buy": candidate["buy_exchange"],
            "sell": candidate["sell_exchange"],
            "expected_net": candidate["net"],
        }
        await asyncio.sleep(EXEC_DELAY)
        fresh = await wait_for_post_signal_quotes(
            candidate["pair"], candidate["buy_exchange"], candidate["sell_exchange"], signal_t
        )
        fill = None
        if fresh:
            fill = route_calc(
                candidate["pair"], candidate["buy_exchange"], candidate["sell_exchange"],
                min(MAX_NOTIONAL, float(state["balance"])), require_after=signal_t,
            )

        route_name = f"{candidate['pair']} {candidate['buy_exchange']}→{candidate['sell_exchange']}"
        if fill and fill["net"] >= MIN_FILL:
            state["balance"] = float(state["balance"]) + fill["net"]
            state["executed"] += 1
            entry = {
                "time": utcnow(),
                "cycle": route_name,
                "legs": f"BUY {candidate['buy_exchange']} @ {fill['buy_ask']:.10g} | SELL {candidate['sell_exchange']} @ {fill['sell_bid']:.10g}",
                "signal": candidate["net"],
                "fill": fill["net"],
                "balance": state["balance"],
                "status": "EXECUTED",
                "pair": candidate["pair"],
                "buy_exchange": candidate["buy_exchange"],
                "sell_exchange": candidate["sell_exchange"],
                "gross_spread_pct": fill["gross_spread_pct"],
                "fees_pct": (fill["buy_fee"] + fill["sell_fee"]) * 100.0,
                "skew_ms": fill["skew_ms"],
                "notional": fill["notional"],
            }
        else:
            state["cancelled"] += 1
            entry = {
                "time": utcnow(),
                "cycle": route_name,
                "legs": f"BUY {candidate['buy_exchange']} | SELL {candidate['sell_exchange']}",
                "signal": candidate["net"],
                "fill": None if not fill else fill["net"],
                "balance": state["balance"],
                "status": "CANCELLED",
                "pair": candidate["pair"],
                "buy_exchange": candidate["buy_exchange"],
                "sell_exchange": candidate["sell_exchange"],
            }
        state.setdefault("history", []).insert(0, entry)
        state["history"] = state["history"][:300]
        await publish(state)


async def evaluator(state, started):
    next_save = time.monotonic() + SAVE_INTERVAL
    while time.monotonic() - started < RUNTIME_SECONDS:
        routes = all_routes(state["balance"])
        state["evaluations"] += len(routes)
        state["quote_updates"] = sum(feed_updates.values())
        state["feeds_online"] = {
            ex: bool(quotes[ex]) and any(time.monotonic() - q["t"] <= MAX_QUOTE_AGE for q in quotes[ex].values())
            for ex in FEES
        }

        current_keys = set()
        best = max(routes, key=lambda r: r["net"], default=None)
        if best:
            state["current_best"] = {
                "pair": best["pair"], "buy": best["buy_exchange"], "sell": best["sell_exchange"],
                "net": best["net"], "gross_spread_pct": best["gross_spread_pct"], "skew_ms": best["skew_ms"],
            }
            if state.get("best_net") is None or best["net"] > float(state["best_net"]):
                state["best_net"] = best["net"]
                state["best_cycle"] = f"{best['pair']} {best['buy_exchange']}→{best['sell_exchange']}"

        for r in routes:
            key = (r["pair"], r["buy_exchange"], r["sell_exchange"])
            current_keys.add(key)
            if r["net"] <= REARM_NET:
                armed[key] = True

        if not execution_lock.locked() and best and best["net"] >= MIN_SIGNAL:
            key = (best["pair"], best["buy_exchange"], best["sell_exchange"])
            if armed.get(key, True):
                armed[key] = False
                asyncio.create_task(execute_candidate(state, best))

        if time.monotonic() >= next_save:
            await publish(state)
            next_save = time.monotonic() + SAVE_INTERVAL
        await asyncio.sleep(0.10)


async def run():
    state = load_state()
    markets = await asyncio.to_thread(discover_markets)
    started = time.monotonic()
    state.update({
        "version": 3,
        "strategy": "cross_exchange_buy_low_sell_high",
        "math_model": "executable_bid_ask_qty_fee_latency_v1",
        "inventory_model": "synthetic_prefunded_two_leg",
        "rebalancing_cost_included": False,
        "run_started_at": utcnow(),
        "run_status": "running",
        "quote_updates": 0,
        "evaluations": 0,
        "signals": 0,
        "executed": 0,
        "cancelled": 0,
        "best_cycle": None,
        "best_net": None,
        "last_error": None,
        "execution_delay_ms": int(EXEC_DELAY * 1000),
        "max_quote_age_ms": int(MAX_QUOTE_AGE * 1000),
        "max_snapshot_skew_ms": int(MAX_SKEW * 1000),
        "max_notional_usdt": MAX_NOTIONAL,
        "min_signal_usdt": MIN_SIGNAL,
        "fee_assumptions": FEES,
        "markets": {k: sorted(v) for k, v in markets.items()},
        "pairs_requested": PAIRS,
    })
    await publish(state)

    tasks = [
        asyncio.create_task(binance_reader(markets["binance"])),
        asyncio.create_task(kraken_reader(markets["kraken"])),
        asyncio.create_task(coinbase_reader(markets["coinbase"])),
        asyncio.create_task(bitstamp_reader(markets["bitstamp"])),
    ]
    try:
        await evaluator(state, started)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state["run_status"] = "completed"
        await publish(state)


if __name__ == "__main__":
    asyncio.run(run())
