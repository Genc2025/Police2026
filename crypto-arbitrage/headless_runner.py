import asyncio
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
RUNTIME_SECONDS = int(os.getenv("PAPER_RUNTIME_SECONDS", "20400"))  # 5h40m
SAVE_INTERVAL = int(os.getenv("PAPER_SAVE_INTERVAL_SECONDS", "900"))  # 15m
BIN_FEE = 0.001
MIN_SIGNAL = 0.01
MIN_FILL = 0.005
EXEC_DELAY = 0.350
MAX_QUOTE_AGE = 2.5
COOLDOWN = 30.0
ASSETS = [
    "ETH", "SOL", "XRP", "ADA", "LINK", "DOGE", "AVAX", "BCH", "LTC",
    "DOT", "UNI", "AAVE", "NEAR", "ATOM", "SUI", "SHIB", "HBAR"
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            if isinstance(data, dict) and float(data.get("balance", 0)) > 0:
                data.setdefault("history", [])
                return data
        except Exception:
            pass
    return {"version": 1, "balance": 100.0, "history": []}


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
        subprocess.run(["git", "commit", "-m", "Update persistent paper state [skip ci]"], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"], check=True)
    except Exception as exc:
        state["last_error"] = f"state publish failed: {exc}"
        write_state(state)


def fetch_active_symbols():
    req = urllib.request.Request(
        "https://data-api.binance.vision/api/v3/exchangeInfo",
        headers={"User-Agent": "paper-arbitrage-research/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    return {
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
    }


def build_cycles(active):
    cycles = []
    if "BTCUSDT" not in active:
        return cycles
    for asset in ASSETS:
        usdt = f"{asset}USDT"
        btc = f"{asset}BTC"
        if usdt in active and btc in active:
            cycles.append({
                "name": f"USDT→{asset}→BTC→USDT",
                "legs": [(usdt, "buy"), (btc, "sell"), ("BTCUSDT", "sell")],
            })
            cycles.append({
                "name": f"USDT→BTC→{asset}→USDT",
                "legs": [("BTCUSDT", "buy"), (btc, "buy"), (usdt, "sell")],
            })
    return cycles


def cycle_calc(cycle, start, quotes):
    amount = float(start)
    now = time.monotonic()
    max_age = 0.0
    for symbol, direction in cycle["legs"]:
        q = quotes.get(symbol)
        if not q:
            return None
        age = now - q["t"]
        if age > MAX_QUOTE_AGE:
            return None
        max_age = max(max_age, age)
        if direction == "buy":
            qty = amount / q["ask"]
            if qty > q["ask_qty"]:
                return None
            amount = qty * (1.0 - BIN_FEE)
        else:
            if amount > q["bid_qty"]:
                return None
            amount = amount * q["bid"] * (1.0 - BIN_FEE)
    net = amount - float(start)
    return {
        "cycle": cycle,
        "start": float(start),
        "end": amount,
        "net": net,
        "gross_pct": (amount / float(start) - 1.0) * 100.0,
        "max_age_ms": max_age * 1000.0,
    }


def best_cycle(cycles, start, quotes):
    candidates = []
    for cycle in cycles:
        r = cycle_calc(cycle, start, quotes)
        if r:
            candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x["net"])


async def run():
    state = load_state()
    state.update({
        "run_started_at": utcnow(),
        "run_status": "starting",
        "quote_updates": 0,
        "signals": 0,
        "executed": 0,
        "cancelled": 0,
        "best_cycle": None,
        "best_net": None,
        "last_error": None,
    })
    write_state(state)

    try:
        active = fetch_active_symbols()
        cycles = build_cycles(active)
    except Exception as exc:
        state["run_status"] = "failed"
        state["last_error"] = f"exchangeInfo failed: {exc}"
        git_publish_state(state)
        raise

    state["cycles_available"] = len(cycles)
    if not cycles:
        state["run_status"] = "failed"
        state["last_error"] = "No valid BTC-bridge triangular cycles found"
        git_publish_state(state)
        return

    symbols = sorted({symbol for c in cycles for symbol, _ in c["legs"]})
    streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    quotes = {}
    cooldowns = {}
    started = time.monotonic()
    next_save = started + SAVE_INTERVAL
    state["run_status"] = "running"
    git_publish_state(state)

    while time.monotonic() - started < RUNTIME_SECONDS:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                state["last_error"] = None
                while time.monotonic() - started < RUNTIME_SECONDS:
                    timeout = min(30.0, max(1.0, RUNTIME_SECONDS - (time.monotonic() - started)))
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    msg = json.loads(raw)
                    d = msg.get("data", msg)
                    symbol = str(d.get("s", "")).upper()
                    if not symbol:
                        continue
                    try:
                        bid = float(d["b"]); bid_qty = float(d["B"])
                        ask = float(d["a"]); ask_qty = float(d["A"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0 or bid_qty <= 0 or ask_qty <= 0:
                        continue
                    quotes[symbol] = {
                        "bid": bid, "bid_qty": bid_qty,
                        "ask": ask, "ask_qty": ask_qty,
                        "t": time.monotonic(),
                    }
                    state["quote_updates"] = int(state.get("quote_updates", 0)) + 1

                    best = best_cycle(cycles, state["balance"], quotes)
                    if best:
                        if state.get("best_net") is None or best["net"] > float(state["best_net"]):
                            state["best_net"] = best["net"]
                            state["best_cycle"] = best["cycle"]["name"]
                        name = best["cycle"]["name"]
                        last = cooldowns.get(name, 0.0)
                        if best["net"] >= MIN_SIGNAL and time.monotonic() - last >= COOLDOWN:
                            cooldowns[name] = time.monotonic()
                            state["signals"] = int(state.get("signals", 0)) + 1
                            signal_net = best["net"]
                            await asyncio.sleep(EXEC_DELAY)
                            fill = cycle_calc(best["cycle"], state["balance"], quotes)
                            if fill and fill["net"] >= MIN_FILL:
                                state["balance"] = fill["end"]
                                state["executed"] = int(state.get("executed", 0)) + 1
                                entry = {
                                    "time": utcnow(),
                                    "cycle": name,
                                    "legs": " → ".join(x[0] for x in best["cycle"]["legs"]),
                                    "signal": signal_net,
                                    "fill": fill["net"],
                                    "balance": state["balance"],
                                    "status": "EXECUTED",
                                }
                            else:
                                state["cancelled"] = int(state.get("cancelled", 0)) + 1
                                entry = {
                                    "time": utcnow(),
                                    "cycle": name,
                                    "legs": " → ".join(x[0] for x in best["cycle"]["legs"]),
                                    "signal": signal_net,
                                    "fill": None,
                                    "balance": state["balance"],
                                    "status": "CANCELLED",
                                }
                            state.setdefault("history", []).insert(0, entry)
                            state["history"] = state["history"][:300]

                    if time.monotonic() >= next_save:
                        state["run_status"] = "running"
                        git_publish_state(state)
                        next_save = time.monotonic() + SAVE_INTERVAL

        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            state["last_error"] = f"websocket: {exc}"
            write_state(state)
            await asyncio.sleep(3)

    state["run_status"] = "completed"
    git_publish_state(state)


if __name__ == "__main__":
    asyncio.run(run())
