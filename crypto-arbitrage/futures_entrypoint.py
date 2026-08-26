import asyncio
import copy
import time

import futures_runner as fr


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
