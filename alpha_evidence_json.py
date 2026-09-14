"""Strict JSON decoding for immutable Alpha evidence boundaries.

SHADOW_ONLY and pure: no network, files, provider or execution imports.
Duplicate members have no unambiguous identity, even when the final value
would pass a later validator. Reject them before Python constructs a dict.
"""

import json
import math


def _unique_members(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("duplicate JSON member: " + key)
        value[key] = member
    return value


def _reject_constant(value):
    raise ValueError("non-finite JSON constant: " + value)


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite range")
    return number


def strict_json_loads(value):
    """Decode JSON without discarding duplicates or accepting non-finite data.

    The pairs hook applies recursively to every object, including objects
    inside arrays. ``parse_float`` additionally refuses overflow such as
    ``1e999``, which Python's constant hook alone does not reject.
    """
    return json.loads(value, object_pairs_hook=_unique_members,
                      parse_constant=_reject_constant,
                      parse_float=_finite_float)
