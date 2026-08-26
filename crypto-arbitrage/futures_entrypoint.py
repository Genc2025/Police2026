import asyncio
import copy
import json
import os
import time

import futures_runner as fr

CONTRARIAN_MIN_SCORE = float(os.getenv("FUTURES_CONTRARIAN_MIN_SCORE", "0.28"))
CONTRARIAN_MIN_CONF = float(os.getenv("FUTURES_CONTRARIAN_MIN_CONFIDENCE", "0.50"))
CONTRARIAN_MIN_EDGE = float(os.getenv("FUTURES_CONTRARIAN_MIN_EDGE", "0.000025"))
CONTRARIAN_EQUITY_FRACTION = float(os.getenv("FUTURES_CONTRARIAN_EQUITY_FRACTION", "0.1666667"))
CONTRARIAN_REVERSAL_MIN_HOLD = float(os.getenv("FUTURES_CONTRARIAN_REVERSAL_MIN_HOLD", "12"))
POLICY = "strict_plus_contrarian_v3"


def qualifies_strict(a):
    hurdle = 2 * fr.TAKER_FEE + a["spread"] + fr.EDGE_BUFFER
    return (
        abs(a["score"]) >= fr.MIN_SCORE
        and a["confidence"] >= fr.MIN_CONF
        and a["predicted_edge"] > hurdle
    )


def qualifies_contrarian(a):
    return (
        abs(a["score"]) >= CONTRARIAN_MIN_SCORE
        and a["confidence"] >= CONTRARIAN_MIN_CONF
        and a["predicted_edge"] >= CONTRARIAN_MIN_EDGE
    )


def prior_policy():
    try:
        raw = json.loads(fr.STATE_PATH.read_text())
        return raw.get("entry_policy")
    except Exception:
        return None


async def open_pos_v3(state, symbol, a):
    if symbol in state["positions"] or time.monotonic() < fr.cooldown_until[symbol]:
        return
    if len(state["positions"]) >= 3:
        return

    strict = qualifies_strict(a)
    contrarian = qualifies_contrarian(a)
    if not strict and not contrarian:
        return

    mode = "STRICT" if strict else "CONTRARIAN"
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
    contrarian2 = qualifies_contrarian(a2)
    if mode == "STRICT" and not strict2:
        if contrarian2:
            mode = "CONTRARIAN"
        else:
            return
    elif mode == "CONTRARIAN" and not contrarian2:
        return

    # STRICT follows the model. CONTRARIAN intentionally executes the opposite side.
    if mode == "STRICT":
        side = "LONG" if a2["score"] > 0 else "SHORT"
    else:
        side = "SHORT" if a2["score"] > 0 else "LONG"

    eq = max(1.0, fr.equity(state))
    notional = eq / 3.0 if mode == "STRICT" else eq * CONTRARIAN_EQUITY_FRACTION

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
        # Target sits above modeled round-trip fee + spread + buffer.
        # Wider hold/less churn gives the opposite-direction hypothesis time to prove itself.
        target_pct = max(hurdle2 * 1.12, min(0.0045, max(a2["vol"] * 4.0, 0.00135)))
        stop_pct = max(0.0010, min(0.0030, max(a2["vol"] * 3.0, 0.0010)))

    state["last_entry_signal"] = {
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "model_side": "LONG" if a2["score"] > 0 else "SHORT",
        "score": a2["score"],
        "confidence": a2["confidence"],
        "predicted_edge_pct": a2["predicted_edge"] * 100.0,
        "cost_hurdle_pct": hurdle2 * 100.0,
    }

    state["positions"][symbol] = {
        "side": side,
        "entry_mode": mode,
        "model_side": "LONG" if a2["score"] > 0 else "SHORT",
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


async def close_pos_v3(state, symbol, reason):
    pos = state["positions"].get(symbol)
    if not pos:
        return
    signal_t = time.monotonic()
    await asyncio.sleep(fr.EXIT_LATENCY)
    b = await fr.fresh_book(symbol, signal_t)
    if not b:
        return

    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    exit_px = b["bid"] if pos["side"] == "LONG" else b["ask"]
    gross = qty * (exit_px - entry) if pos["side"] == "LONG" else qty * (entry - exit_px)
    exit_fee = qty * exit_px * fr.TAKER_FEE
    net = gross - float(pos["entry_fee"]) - exit_fee
    state["balance"] = float(state["balance"]) + net
    state["closed"] = int(state.get("closed", 0)) + 1

    mode = pos.get("entry_mode", "STRICT")
    if net > 0:
        state["wins"] = int(state.get("wins", 0)) + 1
        state[f"{mode.lower()}_wins"] = int(state.get(f"{mode.lower()}_wins", 0)) + 1
    else:
        state["losses"] = int(state.get("losses", 0)) + 1
        state[f"{mode.lower()}_losses"] = int(state.get(f"{mode.lower()}_losses", 0)) + 1

    # Do not let contrarian outcomes mutate the base model into a moving target.
    if mode == "STRICT":
        outcome = 1.0 if net > 0 else -1.0
        direction = 1.0 if pos["side"] == "LONG" else -1.0
        for key in fr.FEATURES:
            signed_feature = float(pos["features"].get(key, 0.0)) * direction
            fr.weights[symbol][key] = fr.clamp(
                fr.weights[symbol][key] + fr.LEARNING_RATE * outcome * signed_feature,
                -2.0,
                2.0,
            )

    held = max(0.0, time.monotonic() - float(pos["opened_mono"]))
    state["history"].insert(0, {
        "time": fr.utcnow(),
        "symbol": symbol,
        "side": pos["side"],
        "model_side": pos.get("model_side"),
        "entry_mode": mode,
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
    fr.cooldown_until[symbol] = time.monotonic() + fr.COOLDOWN


async def manage_v3(state, symbol, a, now):
    pos = state["positions"].get(symbol)
    if not pos:
        await open_pos_v3(state, symbol, a)
        return

    b = fr.books.get(symbol)
    if not b:
        return
    entry = float(pos["entry_price"])
    side = pos["side"]
    mode = pos.get("entry_mode", "STRICT")

    if side == "LONG":
        move = b["bid"] / entry - 1.0
    else:
        move = entry / b["ask"] - 1.0

    held = now - float(pos["opened_mono"])

    if mode == "CONTRARIAN":
        # Model signal is intentionally opposite to our position. A sign flip toward our side
        # is therefore the contrarian thesis ending, but do not churn immediately on noise.
        if side == "LONG":
            reversal = a["score"] > 0.45
        else:
            reversal = a["score"] < -0.45
    else:
        if side == "LONG":
            reversal = a["score"] < -0.45
        else:
            reversal = a["score"] > 0.45

    if move >= float(pos["target_pct"]):
        await close_pos_v3(state, symbol, "TARGET")
    elif move <= -float(pos["stop_pct"]):
        await close_pos_v3(state, symbol, "STOP")
    elif mode == "CONTRARIAN" and held >= CONTRARIAN_REVERSAL_MIN_HOLD and reversal:
        await close_pos_v3(state, symbol, "CONTRARIAN_REVERSAL")
    elif mode != "CONTRARIAN" and held >= fr.MIN_HOLD and reversal:
        await close_pos_v3(state, symbol, "SIGNAL_REVERSAL")
    elif held >= fr.MAX_HOLD:
        await close_pos_v3(state, symbol, "TIME_STOP")


fr.open_pos = open_pos_v3
fr.close_pos = close_pos_v3
fr.manage = manage_v3


async def run():
    previous = prior_policy()
    state = fr.load_state()

    # The old experiment learned from the losing same-direction policy.
    # Start the contrarian epoch from clean default feature weights once, while preserving wallet/history.
    if previous != POLICY:
        for symbol in fr.SYMBOLS:
            fr.weights[symbol] = dict(fr.DEFAULT_W)
        state["contrarian_epoch_started_at"] = fr.utcnow()
        state["contrarian_epoch_start_balance"] = float(state["balance"])
    else:
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
        "entry_policy": POLICY,
        "contrarian_min_score": CONTRARIAN_MIN_SCORE,
        "contrarian_min_confidence": CONTRARIAN_MIN_CONF,
        "contrarian_min_edge_pct": CONTRARIAN_MIN_EDGE * 100.0,
        "last_error": None,
    })
    started = time.monotonic()
    await fr.publish(state)

    reader = asyncio.create_task(fr.market_reader())
    try:
        await asyncio.gather(fr.evaluator(state, started), fr.saver(state, started))
        for symbol in list(state["positions"]):
            try:
                await close_pos_v3(state, symbol, "RUN_END")
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
