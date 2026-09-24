"""Explicit complete synthetic broker responses for existing offline tests.

A bare list is deliberately no longer proof of completeness. Existing tests
that model complete broker observations use this helper; None/errors retain
their original unavailable behavior. It never touches a real client.
"""
import json
from kalshi_client import PositionSnapshot


def complete_positions(rows):
    if rows is None:
        return None
    return PositionSnapshot(json.dumps(rows), ("",), (0,), (0,), 0)
