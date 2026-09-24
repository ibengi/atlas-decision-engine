"""Synthetic, explicit provider-bound closed candles; never production evidence."""
import math
import time


def candles(n=30, now=None, provider="kraken", drift=0.0005):
    now = time.time() if now is None else now
    base = math.floor(now / 60) * 60 - n * 60
    origins = {
        "binance": ("BTCUSDT", "https://api.binance.com/api/v3/klines"),
        "kraken": ("XBTUSD", "https://api.kraken.com/0/public/OHLC"),
        "coinbase": ("BTC-USD", "https://api.exchange.coinbase.com/products/BTC-USD/candles"),
    }
    product, endpoint = origins[provider]
    rows, price = [], 65000.0
    for i in range(n):
        close = price * math.exp(drift * (-1) ** i)
        ts = base + i * 60
        rows.append({"ts": ts, "open": price, "high": max(price, close),
                     "low": min(price, close), "close": close, "volume": 5.0,
                     "close_ts": ts + 60, "closed": True,
                     "provenance": {"provider": provider, "product": product,
                                    "endpoint": endpoint, "interval_s": 60,
                                    "timestamp_semantics": "open_utc_seconds",
                                    "observed_ts": now}})
        price = close
    return rows
