import asyncio
import time

import cross_exchange_runner as m


async def evaluator_v2(state, started):
    next_save = time.monotonic() + m.SAVE_INTERVAL
    while time.monotonic() - started < m.RUNTIME_SECONDS:
        routes = m.all_routes(state["balance"])
        state["evaluations"] += len(routes)
        state["quote_updates"] = sum(m.feed_updates.values())
        state["feeds_online"] = {
            ex: bool(m.quotes[ex]) and any(
                time.monotonic() - q["t"] <= m.MAX_QUOTE_AGE
                for q in m.quotes[ex].values()
            )
            for ex in m.FEES
        }

        best = max(routes, key=lambda r: r["net"], default=None)
        if best:
            state["current_best"] = {
                "pair": best["pair"],
                "buy": best["buy_exchange"],
                "sell": best["sell_exchange"],
                "net": best["net"],
                "gross_spread_pct": best["gross_spread_pct"],
                "skew_ms": best["skew_ms"],
            }
            if state.get("best_net") is None or best["net"] > float(state["best_net"]):
                state["best_net"] = best["net"]
                state["best_cycle"] = (
                    f"{best['pair']} {best['buy_exchange']}→{best['sell_exchange']}"
                )

        eligible = []
        for r in routes:
            key = (r["pair"], r["buy_exchange"], r["sell_exchange"])
            if r["net"] <= m.REARM_NET:
                m.armed[key] = True
            if r["net"] >= m.MIN_SIGNAL and m.armed.get(key, True):
                eligible.append(r)

        if not m.execution_lock.locked() and eligible:
            candidate = max(eligible, key=lambda r: r["net"])
            key = (
                candidate["pair"],
                candidate["buy_exchange"],
                candidate["sell_exchange"],
            )
            m.armed[key] = False
            asyncio.create_task(m.execute_candidate(state, candidate))

        if time.monotonic() >= next_save:
            await m.publish(state)
            next_save = time.monotonic() + m.SAVE_INTERVAL

        await asyncio.sleep(0.10)


m.evaluator = evaluator_v2

if __name__ == "__main__":
    asyncio.run(m.run())
