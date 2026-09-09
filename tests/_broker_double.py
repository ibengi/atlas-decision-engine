# -*- coding: utf-8 -*-
"""Broker test doubles that honour the completeness contract (audit A02).

`PositionManager` may only conclude MATCH from an enumeration it can show
is complete, so a double standing in for the client has to answer the same
question the real client answers: "is this every position, or only the ones
I managed to read?".

`BrokerMock` is a MagicMock whose CLASS implements `get_positions_proof`, so
production code takes the proof path exactly as it does with a real
`KalshiClient`. The proof is derived from whatever `get_positions` is
configured to return, which keeps every existing `return_value` /
`side_effect` setup working unchanged:

  * rows          -> complete enumeration of those rows
  * None          -> unreadable, no proof (fail-closed)
  * side_effect   -> the exception propagates, as with a real transport

A double that wants to model an INCOMPLETE read sets `positions_complete =
False`; that is the pagination-truncated case, and it must never produce
MATCH.
"""
from unittest.mock import MagicMock


class BrokerMock(MagicMock):
    """MagicMock + the position-collection completeness contract."""

    #: Set to False to model a listing the broker did not finish.
    positions_complete = True

    def get_positions_proof(self, **_kw):
        rows = self.get_positions()
        if rows is None:
            return {"rows": None, "complete": False,
                    "reason": "get_positions() -> None"}
        rows = list(rows)
        return {"rows": rows, "complete": bool(self.positions_complete),
                "pages": 1, "cursors": [],
                "reason": None if self.positions_complete
                else "pagination truncated (test double)"}


def complete_proof(rows) -> dict:
    """A proof that `rows` is the WHOLE enumeration."""
    return {"rows": list(rows), "complete": True, "pages": 1, "cursors": [],
            "reason": None}


class CompletePositionsProof:
    """Mixin for hand-written doubles whose `get_positions()` already returns
    the entire portfolio in one call. Inheriting it is the double saying so
    explicitly, which is the whole point of the contract."""

    def get_positions_proof(self, **_kw):
        rows = self.get_positions()
        if rows is None:
            return {"rows": None, "complete": False,
                    "reason": "get_positions() -> None"}
        return complete_proof(rows)
