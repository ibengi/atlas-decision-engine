"""
ExecutionResult — Résultat d'exécution d'ordre Kalshi. Extrait de kalshi_alpha_bot.py (P3.7).
"""

class ExecutionResult:
    def __init__(self, order_id, requested, filled, avg_price, status, state):
        self.order_id, self.requested, self.filled = order_id, requested, filled
        self.avg_price, self.status, self.state = avg_price, status, state
        # state: "filled" | "partial" | "cancelled" | "rejected" | "unknown"
