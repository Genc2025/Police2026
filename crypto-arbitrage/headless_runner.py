import asyncio
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, getcontext
from pathlib import Path

import websockets

getcontext().prec = 40

STATE_PATH = Path(__file__).with_name("headless_state.json")
BRANCH = os.getenv("GITHUB_REF_NAME", "claude/html-demo-rn-app-jz85o7")
RUNTIME_SECONDS = int(os.getenv("PAPER_RUNTIME_SECONDS", "20400"))  # 5h40m
SAVE_INTERVAL = int(os.getenv("PAPER_SAVE_INTERVAL_SECONDS", "900"))  # 15m
TAKER_FEE = Decimal(os.getenv("BIN_TAKER_FEE", "0.001"))
MIN_SIGNAL = Decimal(os.getenv("PAPER_MIN_SIGNAL", "0.01"))
MIN_FILL = Decimal(os.getenv("PAPER_MIN_FILL", "0.005"))
EXEC_DELAY = float(os.getenv("PAPER_EXEC_DELAY", "0.350"))
LEG_DELAY = float(os.getenv("PAPER_LEG_DELAY", "0.075"))
MAX_QUOTE_AGE = float(os.getenv("PAPER_MAX_QUOTE_AGE", "0.75"))
MAX_SNAPSHOT_SKEW = float(os.getenv("PAPER_MAX_SNAPSHOT_SKEW", "0.35"))
COOLDOWN = float(os.getenv("PAPER_COOLDOWN", "15"))
DEPTH_LEVELS = 20
WS_CHUNK = 40

# Assets we deliberately allow in a 3-leg cycle. USDT is the wallet asset.
# Bridge assets are included because they materially increase valid triangle coverage.
INTERMEDIATES = [
    "BTC", "ETH", "BNB", "FDUSD", "USDC",
    "SOL", "XRP", "ADA", "LINK", "DOGE", "AVAX", "BCH", "LTC",
    "DOT", "UNI", "AAVE", "NEAR", "ATOM", "SUI", "SHIB", "HBAR",
]


def D(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def as_float(value):
    return None if value is None else float(value)


def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            if isinstance(data, dict) and float(data.get("balance", 0)) > 0:
                data.setdefault("history", [])
                return data
        except Exception:
            pass
    return {"version": 2, "balance": 100.0, "history": []}


def write_state(state):
    state["updated_at"] = utcnow()
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def git_publish_state(state):
    write_state(state)
    try:
        subprocess.run(["git", "add", str(STATE_PATH)], check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return
        subprocess.run(
            ["git", "commit", "-m", "Update persistent paper state [skip ci]"],
            check=True,
        )
        subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"], check=True)
    except Exception as exc:
        state["last_error"] = f"state publish failed: {exc}"
        write_state(state)


def _filter_map(symbol_info):
    return {f.get("filterType"): f for f in symbol_info.get("filters", [])}


def _positive_decimal(value):
    try:
        x = D(value)
        return x if x > 0 else None
    except Exception:
        return None


def _parse_symbol_meta(s):
    filters = _filter_map(s)
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})

    # MARKET_LOT_SIZE is the correct first choice for a market-style paper fill.
    # Some symbols expose zeros there, so fall back to LOT_SIZE when needed.
    market_step = _positive_decimal(market_lot.get("stepSize"))
    lot_step = _positive_decimal(lot.get("stepSize"))
    use_lot = market_lot if market_step is not None else lot
    step = market_step or lot_step or D("0.00000001")

    min_qty = _positive_decimal(use_lot.get("minQty")) or D("0")
    max_qty = _positive_decimal(use_lot.get("maxQty"))

    min_notional = None
    max_notional = None
    mn = filters.get("MIN_NOTIONAL")
    if mn and mn.get("applyToMarket", True):
        min_notional = _positive_decimal(mn.get("minNotional"))
    nt = filters.get("NOTIONAL")
    if nt:
        if nt.get("applyMinToMarket", True):
            n = _positive_decimal(nt.get("minNotional"))
            if n is not None:
                min_notional = max(min_notional or D("0"), n)
        if nt.get("applyMaxToMarket", False):
            max_notional = _positive_decimal(nt.get("maxNotional"))

    return {
        "symbol": s["symbol"],
        "base": s["baseAsset"],
        "quote": s["quoteAsset"],
        "step": step,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "min_notional": min_notional,
        "max_notional": max_notional,
    }


def fetch_exchange_info():
    req = urllib.request.Request(
        "https://data-api.binance.vision/api/v3/exchangeInfo",
        headers={"User-Agent": "paper-arbitrage-research/2.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    metas = {}
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING" or not s.get("isSpotTradingAllowed", True):
            continue
        try:
            metas[s["symbol"]] = _parse_symbol_meta(s)
        except Exception:
            continue
    return metas


def build_cycles(metas):
    by_pair = {(m["base"], m["quote"]): symbol for symbol, m in metas.items()}

    def conversion(src, dst):
        # Spend src to receive dst.
        buy_symbol = by_pair.get((dst, src))
        if buy_symbol:
            return (buy_symbol, "buy", src, dst)
        sell_symbol = by_pair.get((src, dst))
        if sell_symbol:
            return (sell_symbol, "sell", src, dst)
        return None

    cycles = []
    seen = set()
    for a in INTERMEDIATES:
        if a == "USDT":
            continue
        leg1 = conversion("USDT", a)
        if not leg1:
            continue
        for b in INTERMEDIATES:
            if b in ("USDT", a):
                continue
            leg2 = conversion(a, b)
            leg3 = conversion(b, "USDT")
            if not leg2 or not leg3:
                continue
            key = tuple((x[0], x[1]) for x in (leg1, leg2, leg3))
            if key in seen:
                continue
            seen.add(key)
            cycles.append({
                "name": f"USDT→{a}→{b}→USDT",
                "legs": [leg1, leg2, leg3],
            })
    return cycles


def floor_step(qty, step):
    qty = D(qty)
    step = D(step)
    if qty <= 0 or step <= 0:
        return D("0")
    units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def buy_cost(qty, asks):
    remaining = D(qty)
    cost = D("0")
    for price, level_qty in asks:
        if remaining <= 0:
            break
        take = min(remaining, level_qty)
        cost += take * price
        remaining -= take
    return None if remaining > 0 else cost


def sell_proceeds(qty, bids):
    remaining = D(qty)
    proceeds = D("0")
    for price, level_qty in bids:
        if remaining <= 0:
            break
        take = min(remaining, level_qty)
        proceeds += take * price
        remaining -= take
    return None if remaining > 0 else proceeds


def affordable_base(quote_amount, asks):
    remaining_quote = D(quote_amount)
    qty = D("0")
    for price, level_qty in asks:
        if remaining_quote <= 0:
            break
        full_cost = price * level_qty
        if full_cost <= remaining_quote:
            qty += level_qty
            remaining_quote -= full_cost
        else:
            qty += remaining_quote / price
            remaining_quote = D("0")
            break
    return qty


def valid_order(meta, qty, notional):
    if qty <= 0 or qty < meta["min_qty"]:
        return False
    if meta["max_qty"] is not None and qty > meta["max_qty"]:
        return False
    if meta["min_notional"] is not None and notional < meta["min_notional"]:
        return False
    if meta["max_notional"] is not None and notional > meta["max_notional"]:
        return False
    return True


def apply_leg(portfolio, leg, quote, meta, fee=TAKER_FEE):
    symbol, direction, src, dst = leg
    if meta["symbol"] != symbol:
        return None

    if direction == "buy":
        # symbol is dst/src (base=dst, quote=src)
        if meta["base"] != dst or meta["quote"] != src:
            return None
        available = portfolio.get(src, D("0"))
        if available <= 0:
            return None
        raw_qty = affordable_base(available, quote["asks"])
        if meta["max_qty"] is not None:
            raw_qty = min(raw_qty, meta["max_qty"])
        order_qty = floor_step(raw_qty, meta["step"])
        cost = buy_cost(order_qty, quote["asks"])
        if cost is None or not valid_order(meta, order_qty, cost) or cost > available:
            return None
        received = order_qty * (D("1") - fee)
        portfolio[src] = available - cost
        portfolio[dst] = portfolio.get(dst, D("0")) + received
        return {
            "symbol": symbol,
            "direction": direction,
            "qty": order_qty,
            "vwap": cost / order_qty,
            "notional": cost,
            "fee": order_qty * fee,
            "fee_asset": dst,
            "received": received,
        }

    # sell: symbol is src/dst (base=src, quote=dst)
    if meta["base"] != src or meta["quote"] != dst:
        return None
    available = portfolio.get(src, D("0"))
    if available <= 0:
        return None
    raw_qty = available
    if meta["max_qty"] is not None:
        raw_qty = min(raw_qty, meta["max_qty"])
    order_qty = floor_step(raw_qty, meta["step"])
    proceeds = sell_proceeds(order_qty, quote["bids"])
    if proceeds is None or not valid_order(meta, order_qty, proceeds):
        return None
    received = proceeds * (D("1") - fee)
    portfolio[src] = available - order_qty
    portfolio[dst] = portfolio.get(dst, D("0")) + received
    return {
        "symbol": symbol,
        "direction": direction,
        "qty": order_qty,
        "vwap": proceeds / order_qty,
        "notional": proceeds,
        "fee": proceeds * fee,
        "fee_asset": dst,
        "received": received,
    }


def coherent_quotes(cycle, quotes, max_age=MAX_QUOTE_AGE, max_skew=MAX_SNAPSHOT_SKEW, min_time=None):
    now = time.monotonic()
    selected = []
    for symbol, _direction, _src, _dst in cycle["legs"]:
        q = quotes.get(symbol)
        if not q:
            return None
        if now - q["t"] > max_age:
            return None
        if min_time is not None and q["t"] < min_time:
            return None
        selected.append(q)
    times = [q["t"] for q in selected]
    if max(times) - min(times) > max_skew:
        return None
    return selected


def cycle_calc(cycle, start, quotes, metas, fee=TAKER_FEE, min_time=None):
    selected = coherent_quotes(cycle, quotes, min_time=min_time)
    if selected is None:
        return None

    portfolio = {"USDT": D(start)}
    details = []
    for leg, q in zip(cycle["legs"], selected):
        detail = apply_leg(portfolio, leg, q, metas[leg[0]], fee=fee)
        if detail is None:
            return None
        details.append(detail)

    # Only realized USDT is credited to the paper wallet. Any sub-step residual
    # in intermediate assets is conservatively treated as dust and not booked as profit.
    end = portfolio.get("USDT", D("0"))
    start_d = D(start)
    net = end - start_d
    times = [q["t"] for q in selected]
    return {
        "cycle": cycle,
        "start": start_d,
        "end": end,
        "net": net,
        "net_pct": (net / start_d) * D("100"),
        "snapshot_skew_ms": (max(times) - min(times)) * 1000.0,
        "details": details,
        "dust": {k: v for k, v in portfolio.items() if k != "USDT" and v > 0},
    }


def best_cycle(cycles, start, quotes, metas):
    best = None
    for cycle in cycles:
        r = cycle_calc(cycle, start, quotes, metas)
        if r is not None and (best is None or r["net"] > best["net"]):
            best = r
    return best


async def wait_fresh_quote(symbol, quotes, not_before, timeout=0.40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        q = quotes.get(symbol)
        if q and q["t"] >= not_before and time.monotonic() - q["t"] <= MAX_QUOTE_AGE:
            return q
        await asyncio.sleep(0.01)
    return None


async def execute_cycle_sequential(cycle, start, quotes, metas):
    portfolio = {"USDT": D(start)}
    details = []
    not_before = time.monotonic() - 0.001

    for idx, leg in enumerate(cycle["legs"]):
        symbol = leg[0]
        q = await wait_fresh_quote(symbol, quotes, not_before)
        if q is None:
            return None
        detail = apply_leg(portfolio, leg, q, metas[symbol], fee=TAKER_FEE)
        if detail is None:
            return None
        detail["book_age_ms"] = (time.monotonic() - q["t"]) * 1000.0
        details.append(detail)
        not_before = time.monotonic()
        if idx < len(cycle["legs"]) - 1:
            await asyncio.sleep(LEG_DELAY)

    end = portfolio.get("USDT", D("0"))
    start_d = D(start)
    return {
        "cycle": cycle,
        "start": start_d,
        "end": end,
        "net": end - start_d,
        "net_pct": ((end - start_d) / start_d) * D("100"),
        "details": details,
        "dust": {k: v for k, v in portfolio.items() if k != "USDT" and v > 0},
    }


def serialize_details(details):
    out = []
    for d in details:
        out.append({
            "symbol": d["symbol"],
            "direction": d["direction"],
            "qty": str(d["qty"]),
            "vwap": str(d["vwap"]),
            "notional": str(d["notional"]),
            "fee": str(d["fee"]),
            "fee_asset": d["fee_asset"],
            "received": str(d["received"]),
            "book_age_ms": round(float(d.get("book_age_ms", 0.0)), 3),
        })
    return out


async def market_reader(symbols, quotes, state, update_event, stop_at, reader_id):
    streams = "/".join(f"{s.lower()}@depth{DEPTH_LEVELS}@100ms" for s in symbols)
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"

    while time.monotonic() < stop_at:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=4096,
            ) as ws:
                async for raw in ws:
                    if time.monotonic() >= stop_at:
                        return
                    msg = json.loads(raw)
                    stream = str(msg.get("stream", ""))
                    data = msg.get("data", msg)
                    symbol = stream.split("@", 1)[0].upper()
                    if not symbol or symbol not in symbols:
                        continue
                    try:
                        bids = [(D(p), D(q)) for p, q in data.get("bids", []) if D(q) > 0]
                        asks = [(D(p), D(q)) for p, q in data.get("asks", []) if D(q) > 0]
                    except Exception:
                        continue
                    if not bids or not asks:
                        continue
                    quotes[symbol] = {
                        "bids": bids,
                        "asks": asks,
                        "t": time.monotonic(),
                        "update_id": data.get("lastUpdateId"),
                    }
                    state["quote_updates"] = int(state.get("quote_updates", 0)) + 1
                    update_event.set()
        except Exception as exc:
            state["last_error"] = f"depth reader {reader_id}: {exc}"
            write_state(state)
            await asyncio.sleep(2)


async def evaluate_loop(cycles, quotes, metas, state, update_event, stop_at):
    cooldowns = {}
    next_save = time.monotonic() + SAVE_INTERVAL

    while time.monotonic() < stop_at:
        timeout = min(0.50, max(0.05, stop_at - time.monotonic()))
        try:
            await asyncio.wait_for(update_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        update_event.clear()

        best = best_cycle(cycles, D(state["balance"]), quotes, metas)
        if best is not None:
            state["evaluations"] = int(state.get("evaluations", 0)) + 1
            if state.get("best_net") is None or best["net"] > D(state["best_net"]):
                state["best_net"] = as_float(best["net"])
                state["best_cycle"] = best["cycle"]["name"]
                state["best_snapshot_skew_ms"] = round(float(best["snapshot_skew_ms"]), 3)

            name = best["cycle"]["name"]
            last = cooldowns.get(name, 0.0)
            if best["net"] >= MIN_SIGNAL and time.monotonic() - last >= COOLDOWN:
                cooldowns[name] = time.monotonic()
                state["signals"] = int(state.get("signals", 0)) + 1
                signal_net = best["net"]
                signal_pct = best["net_pct"]
                signal_at = time.monotonic()

                # Critical: market_reader tasks continue consuming the 100 ms depth
                # streams while this evaluator sleeps. The fill therefore uses NEW books.
                await asyncio.sleep(EXEC_DELAY)

                # Require all 3 symbols to have received a depth snapshot after signal time.
                fresh = coherent_quotes(
                    best["cycle"], quotes,
                    max_age=MAX_QUOTE_AGE,
                    max_skew=MAX_SNAPSHOT_SKEW,
                    min_time=signal_at,
                )
                fill = None
                if fresh is not None:
                    fill = await execute_cycle_sequential(
                        best["cycle"], D(state["balance"]), quotes, metas
                    )

                if fill is not None and fill["net"] >= MIN_FILL:
                    state["balance"] = as_float(fill["end"])
                    state["executed"] = int(state.get("executed", 0)) + 1
                    entry = {
                        "time": utcnow(),
                        "cycle": name,
                        "legs": " → ".join(x[0] for x in best["cycle"]["legs"]),
                        "signal": as_float(signal_net),
                        "signal_pct": as_float(signal_pct),
                        "fill": as_float(fill["net"]),
                        "fill_pct": as_float(fill["net_pct"]),
                        "balance": state["balance"],
                        "status": "EXECUTED",
                        "execution_model": "depth20_vwap_sequential",
                        "fills": serialize_details(fill["details"]),
                    }
                    # Publish profitable executions immediately so the phone sees them.
                    state.setdefault("history", []).insert(0, entry)
                    state["history"] = state["history"][:300]
                    git_publish_state(state)
                    next_save = time.monotonic() + SAVE_INTERVAL
                else:
                    state["cancelled"] = int(state.get("cancelled", 0)) + 1
                    entry = {
                        "time": utcnow(),
                        "cycle": name,
                        "legs": " → ".join(x[0] for x in best["cycle"]["legs"]),
                        "signal": as_float(signal_net),
                        "signal_pct": as_float(signal_pct),
                        "fill": None if fill is None else as_float(fill["net"]),
                        "fill_pct": None if fill is None else as_float(fill["net_pct"]),
                        "balance": state["balance"],
                        "status": "CANCELLED",
                        "execution_model": "depth20_vwap_sequential",
                    }
                    state.setdefault("history", []).insert(0, entry)
                    state["history"] = state["history"][:300]

        if time.monotonic() >= next_save:
            state["run_status"] = "running"
            git_publish_state(state)
            next_save = time.monotonic() + SAVE_INTERVAL


async def run():
    state = load_state()
    state.update({
        "version": 2,
        "math_model": "decimal_depth20_vwap_filters_coherent_sequential_v2",
        "run_started_at": utcnow(),
        "run_status": "starting",
        "quote_updates": 0,
        "evaluations": 0,
        "signals": 0,
        "executed": 0,
        "cancelled": 0,
        "best_cycle": None,
        "best_net": None,
        "best_snapshot_skew_ms": None,
        "taker_fee_assumption": float(TAKER_FEE),
        "depth_levels": DEPTH_LEVELS,
        "depth_update_ms": 100,
        "execution_delay_ms": int(EXEC_DELAY * 1000),
        "leg_delay_ms": int(LEG_DELAY * 1000),
        "last_error": None,
    })
    write_state(state)

    try:
        metas = fetch_exchange_info()
        cycles = build_cycles(metas)
    except Exception as exc:
        state["run_status"] = "failed"
        state["last_error"] = f"exchangeInfo failed: {exc}"
        git_publish_state(state)
        raise

    if not cycles:
        state["run_status"] = "failed"
        state["last_error"] = "No valid 3-leg USDT cycles found"
        git_publish_state(state)
        return

    symbols = sorted({leg[0] for c in cycles for leg in c["legs"]})
    state["cycles_available"] = len(cycles)
    state["symbols_streamed"] = len(symbols)
    state["run_status"] = "running"
    git_publish_state(state)

    quotes = {}
    update_event = asyncio.Event()
    started = time.monotonic()
    stop_at = started + RUNTIME_SECONDS

    chunks = [symbols[i:i + WS_CHUNK] for i in range(0, len(symbols), WS_CHUNK)]
    readers = [
        asyncio.create_task(
            market_reader(chunk, quotes, state, update_event, stop_at, idx + 1)
        )
        for idx, chunk in enumerate(chunks)
    ]
    evaluator = asyncio.create_task(
        evaluate_loop(cycles, quotes, metas, state, update_event, stop_at)
    )

    try:
        await evaluator
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    state["run_status"] = "completed"
    git_publish_state(state)


if __name__ == "__main__":
    asyncio.run(run())
