"""Small deterministic Kalshi client double for offline tests.

This intentionally models only the client surface used by the engine tests;
it is not an API simulator.  The signatures mirror :class:`KalshiClient`, in
particular ``get_fills(order_id, *, strict=False)``.
"""


class MockKalshiClient:
    """In-memory Kalshi client with fill, partial, and resting order modes."""

    env = "demo"
    base_url = "mock://demo"
    cred_src = "mock"
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, market=None, order_scenario="fill"):
        self.market = market or self._default_market()
        self.scenario = order_scenario
        self.created_orders = []
        self.cancelled = []
        self._orders = {}
        self.last_http_status = 200

    @staticmethod
    def _default_market():
        return {
            "ticker": "KXBTCD-26JUL2017-T64999.99",
            "series_ticker": "KXBTCD",
            "status": "open",
            "close_time": "2099-01-01T00:00:00+00:00",
            "floor_strike": 64999.99,
            "title": "BTC above 65,000 today?",
            "yes_bid": 46,
            "yes_ask": 48,
            "no_bid": 52,
            "no_ask": 54,
            "volume_24h": 5000,
        }

    # Market/account endpoints -------------------------------------------------
    def get_balance(self):
        return 93.26

    def get_markets(self, series, status="open", limit=50):
        return [dict(self.market)] if series == self.market.get("series_ticker", "KXBTCD") else []

    def get_market(self, ticker):
        return dict(self.market) if ticker == self.market.get("ticker") else {}

    def get_positions(self):
        positions = []
        for order in self._orders.values():
            count = int(order.get("taker_fill_count") or 0)
            if count:
                positions.append({
                    "ticker": order["ticker"],
                    "position": count if order["side"] == "yes" else -count,
                    "market_exposure": count * order["price"],
                    "realized_pnl": 0,
                    "fees_paid": 2,
                })
        return positions

    # Order endpoints ----------------------------------------------------------
    def create_order(self, ticker, side, count, price_cents, client_order_id=None):
        oid = f"o{len(self.created_orders) + 1}"
        count = int(count)
        scenario = self.scenario
        if scenario not in {"fill", "partial", "none"}:
            raise ValueError(f"unknown mock order scenario: {scenario}")
        filled = {"fill": count, "partial": max(0, count - 1), "none": 0}[scenario]
        status = "executed" if scenario == "fill" else ("canceled" if scenario == "partial" else "resting")
        order = {
            "order_id": oid, "ticker": ticker, "side": side,
            "count": count, "price": int(price_cents),
            "client_order_id": client_order_id, "status": status,
            "taker_fill_count": filled, "remaining_count": count - filled,
        }
        self.last_http_status = 201
        self.created_orders.append(dict(order))
        self._orders[oid] = order
        return dict(order)

    def get_order(self, order_id):
        return dict(self._orders.get(order_id, {}))

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        order = self._orders.get(order_id)
        if order and order["status"] != "executed":
            order["status"] = "canceled"
        return dict(order or {})

    def get_fills(self, order_id, *, strict=False):
        """Return fills for an order; ``strict`` is accepted like KalshiClient."""
        order = self._orders.get(order_id) or {}
        count = int(order.get("taker_fill_count") or 0)
        if count <= 0:
            return []
        return [{"fill_id": f"f-{order_id}", "count": count,
                 f"{order['side']}_price": order["price"], "fees": "0.02"}]
