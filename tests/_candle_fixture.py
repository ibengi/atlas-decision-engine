"""Fresh synthetic candle evidence for tests of downstream, unrelated gates."""
import math
import statistics
import time
import btc_context as bc


def attach_context(ctx):
    now = time.time()
    end = int(now) // 60 * 60 - 60
    rows = [{"ts": float(end-(29-i)*60), "open": 65000., "high": 65100.,
             "low": 64900., "close": 65000.+(26. if i%2 else -26.), "volume": 1.}
            for i in range(30)]
    proof = bc._klines_provenance(rows, "fresh:kraken", now)
    proof["valid_until"] = min(now+90, end+60+bc.MAX_KLINE_CLOSE_AGE_S)
    logs = [math.log(row["close"]) for row in rows]
    ctx.realized_vol_1m = statistics.pstdev(b-a for a,b in zip(logs, logs[1:]))
    ctx.returns = {"5m": logs[-1]-logs[-6]}
    ctx.klines_provenance = proof
    return ctx


def model_output():
    from types import SimpleNamespace
    ctx = attach_context(SimpleNamespace())
    return {"valid": True, "features": {
        "sigma_1m": ctx.realized_vol_1m, "ret_5m": ctx.returns["5m"],
        "candle_provenance": ctx.klines_provenance}}
