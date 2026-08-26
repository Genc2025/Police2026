import asyncio
import copy
import os
import time

import futures_runner as fr

EXPLORE_MIN_SCORE = float(os.getenv("FUTURES_EXPLORE_MIN_SCORE", "0.28"))
EXPLORE_MIN_CONF = float(os.getenv("FUTURES_EXPLORE_MIN_CONFIDENCE", "0.50"))
EXPLORE_MIN_EDGE = float(os.getenv("FUTURES_EXPLORE_MIN_EDGE", "0.000025"))
EXPLORE_EQUITY_FRACTION = float(os.getenv("FUTURES_EXPLORE_EQUITY_FRACTION", "0.1666667"))


def qualifies_strict(a):
    hurdle = 2 * fr.TAKER_FEE + a["spread"] + fr.EDGE_BUFFER
    return (
        abs(a["score"]) >= fr.MIN_SCORE
        and a["confidence"] >= fr.MIN_CONF
        and a["predicted_edge"] > hurdle
    )


def qualifies_explore(a):
    return (
        abs(a["score"]) >= EXPLORE_MIN_SCORE
        and a["confidence"] >= EXPLORE_MIN_CONF
        and a["predicted_edge"] >= EXPLORE_MIN_EDGE
    )


async def open_pos_v2(state, symbol, a):
    if symbol in state["positions"] or time.monotonic() < fr.cooldown_until[symbol]:
        return
    if len(state["positions"]) >= 3:
        return

    strict = qualifies_strict(a)
    explore = qualifies_explore(a)
    if not strict and not explore:
        return

    mode = "STRICT" if strict else "EXPLORE"
    state["signals"] = int(state.get("signals", 0)) + 1
    state[f"{mode.lower()}_signals"] = int(state.get(f"{mode.lower()}_signals", 0)) + 1

    signal_t = time.monotonic()
    await asyncio.sleep(fr.ENTRY_LATENCY)
    b = await fr.fresh_book(symbol, signal_t)
    if not b:
        return

    a2 = fr.analysis(symbol, time.monotonic())
    if not a2 or a2["score"] * a["score"] <= 0:
        return

    strict2 = qualifies_strict(a2)
    explore2 = qualifies_explore(a2)
    if mode == "STRICT" and not strict2:
        if explore2:
            mode = "EXPLORE"
        else:
            return
    elif mode == "EXPLORE" and not explore2:
        return

    side = "LONG" if a2["score"] > 0 else "SHORT"
    eq = max(1.0, fr.equity(state))
    notional = eq / 3.0 if mode == "STRICT" else eq * EXPLORE_EQUITY_FRACTION

    entry_px = b["ask"] if side == "LONG" else b["bid"]
    available = b["ask_qty"] if side == "LONG" else b["bid_qty"]
    qty = notional / entry_px
    if qty > available:
        return

    hurdle2 = 2 * fr.TAKER_FEE + a2["spread"] + fr.EDGE_BUFFER
    if mode == "STRICT":
        target_pct = max(hurdle2 * 1.25, min(0.0050, a2["predicted_edge"] * 0.85))
        stop_pct = max(hurdle2 * 0.85, min(0.0035, a2["vol"] * 2.3))
    else:
        # Exploration still has to beat real modeled round-trip costs to finish net-positive.
        target_pct = max(hurdle2 * 1.10, min(0.0040, max(a2["vol"] * 3.5, 0.0012)))
        stop_pct = max(0.0008, min(0.0025, max(a2["vol"] * 2.3, 0.0008)))

    fr.state_entry = {
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "score": a2["score"],
        "confidence": a2["confidence"],
        "predicted_edge_pct": a2["predicted_edge"] * 100.0,
        "cost_hurdle_pct": hurdle2 * 100.0,
    }
    state["last_entry_signal"] = dict(fr.state_entry)

    state["positions"][symbol] = {
        "side": side,
        "entry_mode": mode,
        "entry_price": entry_px,
        "qty": qty,
        "notional": notional,
        "entry_fee": qty * entry_px * fr.TAKER_FEE,
        "opened_at": fr.utcnow(),
        "opened_mono": time.monotonic(),
        "entry_score": a2["score"],
        "entry_confidence": a2["confidence"],
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "regime": a2["regime"],
        "features": a2["features"],
    }
    state["opened"] = int(state.get("opened", 0)) + 1
    state[f"{mode.lower()}_opened"] = int(state.get(f"{mode.lower()}_opened", 0)) + 1


# manage() in futures_runner resolves this global dynamically, so replacing it here
# enables research sampling without changing the core market-data/math implementation.
fr.open_pos = open_pos_v2


async def run():
    state = fr.load_state()
    saved = state.get("model_weights", {}) if isinstance(state.get("model_weights"), dict) else {}
    for symbol in fr.SYMBOLS:
        if isinstance(saved.get(symbol), dict):
            for key in fr.FEATURES:
                try:
                    fr.weights[symbol][key] = float(saved[symbol].get(key, fr.weights[symbol][key]))
                except Exception:
                    pass

    state.update({
        "run_started_at": fr.utcnow(),
        "run_status": "running",
        "symbols": fr.SYMBOLS,
        "paper_leverage": 1.0,
        "max_concurrent_positions": 3,
        "taker_fee_assumption": fr.TAKER_FEE,
        "entry_latency_ms": int(fr.ENTRY_LATENCY * 1000),
        "exit_latency_ms": int(fr.EXIT_LATENCY * 1000),
        "max_hold_seconds": fr.MAX_HOLD,
        "entry_policy": "strict_plus_exploration_v2",
        "explore_min_score": EXPLORE_MIN_SCORE,
        "explore_min_confidence": EXPLORE_MIN_CONF,
        "explore_min_edge_pct": EXPLORE_MIN_EDGE * 100.0,
        "last_error": None,
    })
    started = time.monotonic()
    await fr.publish(state)

    reader = asyncio.create_task(fr.market_reader())
    try:
        await asyncio.gather(fr.evaluator(state, started), fr.saver(state, started))

        # Keep live market data running while settling every remaining paper position.
        for symbol in list(state["positions"]):
            try:
                await fr.close_pos(state, symbol, "RUN_END")
            except Exception as exc:
                state["last_error"] = f"run-end close {symbol}: {exc}"
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    state["model_weights"] = copy.deepcopy(fr.weights)
    state["equity"] = fr.equity(state)
    state["unrealized_pnl"] = state["equity"] - float(state["balance"])
    state["run_status"] = "completed"
    await fr.publish(state)


if __name__ == "__main__":
    asyncio.run(run())
