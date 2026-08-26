import asyncio
import copy
import json
import math
import os
import statistics
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import websockets

STATE_PATH = Path(__file__).with_name("futures_state.json")
BRANCH = os.getenv("GITHUB_REF_NAME", "claude/html-demo-rn-app-jz85o7")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

RUNTIME_SECONDS = int(os.getenv("FUTURES_RUNTIME_SECONDS", "20400"))
SAVE_INTERVAL = int(os.getenv("FUTURES_SAVE_INTERVAL_SECONDS", "120"))
TAKER_FEE = float(os.getenv("FUTURES_TAKER_FEE", "0.0005"))
ENTRY_LATENCY = float(os.getenv("FUTURES_ENTRY_LATENCY", "0.120"))
EXIT_LATENCY = float(os.getenv("FUTURES_EXIT_LATENCY", "0.100"))
MAX_HOLD = float(os.getenv("FUTURES_MAX_HOLD_SECONDS", "20"))
MIN_HOLD = float(os.getenv("FUTURES_MIN_HOLD_SECONDS", "1.2"))
COOLDOWN = float(os.getenv("FUTURES_COOLDOWN_SECONDS", "3"))
MIN_SCORE = float(os.getenv("FUTURES_MIN_SCORE", "0.56"))
MIN_CONF = float(os.getenv("FUTURES_MIN_CONFIDENCE", "0.68"))
EDGE_BUFFER = float(os.getenv("FUTURES_EDGE_BUFFER", "0.00020"))
MAX_SPREAD = float(os.getenv("FUTURES_MAX_SPREAD", "0.00040"))
WARMUP = float(os.getenv("FUTURES_WARMUP_SECONDS", "15"))
LEARNING_RATE = 0.03

FEATURES = ["book", "micro", "flow1", "flow5", "mom05", "mom2", "mom5", "basis"]
DEFAULT_W = {
    "book": 0.95,
    "micro": 0.90,
    "flow1": 1.15,
    "flow5": 0.90,
    "mom05": 0.80,
    "mom2": 1.00,
    "mom5": 0.75,
    "basis": -0.30,
}

books = {}
marks = {}
funding = {}
mid_hist = {s: deque(maxlen=6000) for s in SYMBOLS}
trade_hist = {s: deque(maxlen=10000) for s in SYMBOLS}
weights = {s: dict(DEFAULT_W) for s in SYMBOLS}
cooldown_until = {s: 0.0 for s in SYMBOLS}
publish_lock = asyncio.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def load_state():
    state = {
        "version": 1,
        "strategy": "adaptive_binance_futures_microstructure",
        "math_model": "online_ensemble_microprice_flow_regime_v1",
        "initial_balance": 100.0,
        "balance": 100.0,
        "history": [],
        "positions": {},
        "opened": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "signals": 0,
    }
    if STATE_PATH.exists():
        try:
            old = json.loads(STATE_PATH.read_text())
            for k in state:
                if k in old:
                    state[k] = old[k]
            if isinstance(old.get("model_weights"), dict):
                state["model_weights"] = old["model_weights"]
        except Exception:
            pass
    if float(state.get("balance", 0)) <= 0:
        state["balance"] = 100.0
    if not isinstance(state.get("history"), list):
        state["history"] = []
    state["positions"] = {}
    return state


def write_state(snapshot):
    snapshot["updated_at"] = utcnow()
    STATE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def publish_sync(snapshot):
    write_state(snapshot)
    subprocess.run(["git", "add", str(STATE_PATH)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", "Update futures paper state [skip ci]"], check=True)
    for _ in range(3):
        if subprocess.run(["git", "pull", "--rebase", "origin", BRANCH]).returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], check=False)
            time.sleep(1)
            continue
        if subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"]).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("futures state publish failed")


async def publish(state):
    async with publish_lock:
        snap = copy.deepcopy(state)
        try:
            await asyncio.to_thread(publish_sync, snap)
        except Exception as exc:
            state["last_error"] = f"publish: {exc}"


def prune(symbol, now):
    while mid_hist[symbol] and now - mid_hist[symbol][0][0] > 30:
        mid_hist[symbol].popleft()
    while trade_hist[symbol] and now - trade_hist[symbol][0][0] > 12:
        trade_hist[symbol].popleft()


def old_price(symbol, seconds, now):
    target = now - seconds
    hist = mid_hist[symbol]
    if not hist:
        return None
    result = hist[0][1]
    for t, p in hist:
        if t <= target:
            result = p
        else:
            break
    return result


def ret(symbol, seconds, now, mid):
    p = old_price(symbol, seconds, now)
    return 0.0 if not p or p <= 0 else mid / p - 1.0


def flow(symbol, seconds, now):
    signed = total = 0.0
    for t, x in reversed(trade_hist[symbol]):
        if now - t > seconds:
            break
        signed += x
        total += abs(x)
    return signed / total if total else 0.0


def realized_vol(symbol, now):
    rows = [(t, p) for t, p in mid_hist[symbol] if now - t <= 10]
    sample = []
    last_t = -1e9
    for t, p in rows:
        if t - last_t >= 0.40:
            sample.append((t, p))
            last_t = t
    if len(sample) < 5:
        return 0.00030
    rs = [math.log(sample[i][1] / sample[i-1][1]) for i in range(1, len(sample))]
    return max(0.00007, statistics.pstdev(rs))


def analysis(symbol, now):
    b = books.get(symbol)
    if not b:
        return None
    bid, ask = b["bid"], b["ask"]
    bq, aq = b["bid_qty"], b["ask_qty"]
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid
    if mid <= 0 or spread <= 0 or spread > MAX_SPREAD:
        return None

    imbalance = (bq - aq) / (bq + aq) if bq + aq else 0.0
    micro = (ask * bq + bid * aq) / (bq + aq) if bq + aq else mid
    halfspread = max((ask - bid) / 2.0, mid * 1e-8)
    micro_feature = clamp((micro - mid) / halfspread)
    vol = realized_vol(symbol, now)
    basis_raw = (mid / marks.get(symbol, mid) - 1.0) if marks.get(symbol, mid) > 0 else 0.0

    f = {
        "book": clamp(imbalance),
        "micro": micro_feature,
        "flow1": clamp(flow(symbol, 1.0, now)),
        "flow5": clamp(flow(symbol, 5.0, now)),
        "mom05": math.tanh(ret(symbol, 0.5, now, mid) / max(vol * 0.9, 0.00008)),
        "mom2": math.tanh(ret(symbol, 2.0, now, mid) / max(vol * 1.8, 0.00016)),
        "mom5": math.tanh(ret(symbol, 5.0, now, mid) / max(vol * 3.0, 0.00028)),
        "basis": math.tanh(basis_raw / 0.00045),
    }

    trend = f["mom2"] * f["flow1"] > 0 and abs(f["mom2"]) > 0.25
    shock = abs(ret(symbol, 0.5, now, mid)) > max(vol * 2.8, 0.00040) and abs(f["flow1"]) < 0.30
    regime = "trend" if trend else ("mean_revert" if shock else "mixed")
    if regime == "mean_revert":
        f["mom05"] *= -0.60
        f["mom2"] *= -0.30

    w = weights[symbol]
    denom = sum(abs(w[k]) for k in FEATURES) or 1.0
    score = clamp(sum(w[k] * f[k] for k in FEATURES) / denom)
    confidence = sigmoid((abs(score) - 0.38) * 8.0)
    r05 = abs(ret(symbol, 0.5, now, mid))
    r2 = abs(ret(symbol, 2.0, now, mid))
    predicted_edge = abs(score) * max(vol * 2.7, r05 * 0.90, r2 * 0.55)

    return {
        "features": f,
        "score": score,
        "confidence": confidence,
        "predicted_edge": predicted_edge,
        "spread": spread,
        "vol": vol,
        "mid": mid,
        "regime": regime,
    }


def unrealized(symbol, pos):
    b = books.get(symbol)
    if not b:
        return 0.0
    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    if pos["side"] == "LONG":
        px = b["bid"]
        gross = qty * (px - entry)
    else:
        px = b["ask"]
        gross = qty * (entry - px)
    return gross - float(pos["entry_fee"]) - qty * px * TAKER_FEE


def equity(state):
    return float(state["balance"]) + sum(unrealized(s, p) for s, p in state["positions"].items())


async def fresh_book(symbol, after, timeout=0.7):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        b = books.get(symbol)
        if b and b["t"] > after:
            return b
        await asyncio.sleep(0.01)
    return None


async def open_pos(state, symbol, a):
    if symbol in state["positions"] or time.monotonic() < cooldown_until[symbol]:
        return
    if len(state["positions"]) >= 3:
        return

    hurdle = 2 * TAKER_FEE + a["spread"] + EDGE_BUFFER
    if abs(a["score"]) < MIN_SCORE or a["confidence"] < MIN_CONF or a["predicted_edge"] <= hurdle:
        return

    state["signals"] += 1
    signal_t = time.monotonic()
    await asyncio.sleep(ENTRY_LATENCY)
    b = await fresh_book(symbol, signal_t)
    if not b:
        return
    a2 = analysis(symbol, time.monotonic())
    if not a2 or a2["score"] * a["score"] <= 0:
        return
    hurdle2 = 2 * TAKER_FEE + a2["spread"] + EDGE_BUFFER
    if abs(a2["score"]) < MIN_SCORE or a2["confidence"] < MIN_CONF or a2["predicted_edge"] <= hurdle2:
        return

    side = "LONG" if a2["score"] > 0 else "SHORT"
    eq = max(1.0, equity(state))
    notional = eq / 3.0
    entry_px = b["ask"] if side == "LONG" else b["bid"]
    available = b["ask_qty"] if side == "LONG" else b["bid_qty"]
    qty = notional / entry_px
    if qty > available:
        return

    target_pct = max(hurdle2 * 1.25, min(0.0050, a2["predicted_edge"] * 0.85))
    stop_pct = max(hurdle2 * 0.85, min(0.0035, a2["vol"] * 2.3))
    state["positions"][symbol] = {
        "side": side,
        "entry_price": entry_px,
        "qty": qty,
        "notional": notional,
        "entry_fee": qty * entry_px * TAKER_FEE,
        "opened_at": utcnow(),
        "opened_mono": time.monotonic(),
        "entry_score": a2["score"],
        "entry_confidence": a2["confidence"],
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "regime": a2["regime"],
        "features": a2["features"],
    }
    state["opened"] += 1


async def close_pos(state, symbol, reason):
    pos = state["positions"].get(symbol)
    if not pos:
        return
    signal_t = time.monotonic()
    await asyncio.sleep(EXIT_LATENCY)
    b = await fresh_book(symbol, signal_t)
    if not b:
        return

    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    exit_px = b["bid"] if pos["side"] == "LONG" else b["ask"]
    gross = qty * (exit_px - entry) if pos["side"] == "LONG" else qty * (entry - exit_px)
    exit_fee = qty * exit_px * TAKER_FEE
    net = gross - float(pos["entry_fee"]) - exit_fee
    state["balance"] = float(state["balance"]) + net
    state["closed"] += 1
    outcome = 1.0 if net > 0 else -1.0
    if net > 0:
        state["wins"] += 1
    else:
        state["losses"] += 1

    direction = 1.0 if pos["side"] == "LONG" else -1.0
    for k in FEATURES:
        signed_feature = float(pos["features"].get(k, 0.0)) * direction
        weights[symbol][k] = clamp(weights[symbol][k] + LEARNING_RATE * outcome * signed_feature, -2.0, 2.0)

    held = max(0.0, time.monotonic() - float(pos["opened_mono"]))
    state["history"].insert(0, {
        "time": utcnow(),
        "symbol": symbol,
        "side": pos["side"],
        "entry_price": entry,
        "exit_price": exit_px,
        "notional": pos["notional"],
        "gross_pnl": gross,
        "fees": float(pos["entry_fee"]) + exit_fee,
        "net_pnl": net,
        "balance": state["balance"],
        "held_seconds": held,
        "reason": reason,
        "entry_score": pos["entry_score"],
        "entry_confidence": pos["entry_confidence"],
        "regime": pos["regime"],
        "status": "WIN" if net > 0 else "LOSS",
    })
    state["history"] = state["history"][:400]
    del state["positions"][symbol]
    cooldown_until[symbol] = time.monotonic() + COOLDOWN


async def manage(state, symbol, a, now):
    pos = state["positions"].get(symbol)
    if not pos:
        await open_pos(state, symbol, a)
        return
    b = books.get(symbol)
    if not b:
        return
    entry = float(pos["entry_price"])
    if pos["side"] == "LONG":
        move = b["bid"] / entry - 1.0
        reversed_signal = a["score"] < -0.45
    else:
        move = entry / b["ask"] - 1.0
        reversed_signal = a["score"] > 0.45
    held = now - float(pos["opened_mono"])
    if move >= float(pos["target_pct"]):
        await close_pos(state, symbol, "TARGET")
    elif move <= -float(pos["stop_pct"]):
        await close_pos(state, symbol, "STOP")
    elif held >= MIN_HOLD and reversed_signal:
        await close_pos(state, symbol, "SIGNAL_REVERSAL")
    elif held >= MAX_HOLD:
        await close_pos(state, symbol, "TIME_STOP")


async def market_reader():
    streams = []
    for s in SYMBOLS:
        x = s.lower()
        streams += [f"{x}@bookTicker", f"{x}@aggTrade", f"{x}@markPrice@1s"]
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=4096) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    stream = str(msg.get("stream", ""))
                    d = msg.get("data", msg)
                    symbol = str(d.get("s", "")).upper()
                    if symbol not in SYMBOLS:
                        continue
                    now = time.monotonic()
                    if stream.endswith("@bookTicker"):
                        try:
                            bid, bq, ask, aq = float(d["b"]), float(d["B"]), float(d["a"]), float(d["A"])
                        except Exception:
                            continue
                        if bid <= 0 or ask <= bid or bq <= 0 or aq <= 0:
                            continue
                        books[symbol] = {"bid": bid, "bid_qty": bq, "ask": ask, "ask_qty": aq, "t": now}
                        mid_hist[symbol].append((now, (bid + ask) / 2.0))
                    elif stream.endswith("@aggTrade"):
                        try:
                            price, qty, maker = float(d["p"]), float(d["q"]), bool(d["m"])
                        except Exception:
                            continue
                        trade_hist[symbol].append((now, price * qty * (-1.0 if maker else 1.0)))
                    elif "@markPrice" in stream:
                        try:
                            marks[symbol] = float(d["p"])
                            funding[symbol] = float(d.get("r", 0.0))
                        except Exception:
                            pass
                    prune(symbol, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


async def evaluator(state, started):
    while time.monotonic() - started < RUNTIME_SECONDS:
        now = time.monotonic()
        for symbol in SYMBOLS:
            hist = mid_hist[symbol]
            if not hist or now - hist[0][0] < WARMUP:
                continue
            a = analysis(symbol, now)
            if not a:
                continue
            state.setdefault("latest", {})[symbol] = {
                "time": utcnow(),
                "score": a["score"],
                "confidence": a["confidence"],
                "predicted_edge_pct": a["predicted_edge"] * 100.0,
                "spread_pct": a["spread"] * 100.0,
                "regime": a["regime"],
                "mid": a["mid"],
                "funding": funding.get(symbol),
            }
            await manage(state, symbol, a, now)
        state["equity"] = equity(state)
        state["unrealized_pnl"] = state["equity"] - float(state["balance"])
        await asyncio.sleep(0.05)


async def saver(state, started):
    next_save = time.monotonic() + SAVE_INTERVAL
    while time.monotonic() - started < RUNTIME_SECONDS:
        if time.monotonic() >= next_save:
            state["model_weights"] = copy.deepcopy(weights)
            state["run_status"] = "running"
            await publish(state)
            next_save = time.monotonic() + SAVE_INTERVAL
        await asyncio.sleep(1)


async def run():
    state = load_state()
    saved = state.get("model_weights", {}) if isinstance(state.get("model_weights"), dict) else {}
    for s in SYMBOLS:
        if isinstance(saved.get(s), dict):
            for k in FEATURES:
                try:
                    weights[s][k] = float(saved[s].get(k, weights[s][k]))
                except Exception:
                    pass

    state.update({
        "run_started_at": utcnow(),
        "run_status": "running",
        "symbols": SYMBOLS,
        "paper_leverage": 1.0,
        "max_concurrent_positions": 3,
        "taker_fee_assumption": TAKER_FEE,
        "entry_latency_ms": int(ENTRY_LATENCY * 1000),
        "exit_latency_ms": int(EXIT_LATENCY * 1000),
        "max_hold_seconds": MAX_HOLD,
        "last_error": None,
    })
    started = time.monotonic()
    await publish(state)

    reader = asyncio.create_task(market_reader())
    try:
        await asyncio.gather(evaluator(state, started), saver(state, started))
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    # Close any remaining paper positions before ending the 6h runner window.
    for symbol in list(state["positions"]):
        try:
            await close_pos(state, symbol, "RUN_END")
        except Exception:
            pass
    state["model_weights"] = copy.deepcopy(weights)
    state["equity"] = equity(state)
    state["unrealized_pnl"] = state["equity"] - float(state["balance"])
    state["run_status"] = "completed"
    await publish(state)


if __name__ == "__main__":
    asyncio.run(run())
