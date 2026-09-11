"""Exact JSON and finite economic values at trust boundaries."""
import json
import math


def finite_number(value, name="number", *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: finite number required")
    try:
        valid = math.isfinite(value)
    except (OverflowError, TypeError):
        valid = False
    if not valid or (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum):
        raise ValueError(f"{name}: invalid finite range")
    return value


def integer(value, name="integer", *, minimum=0):
    finite_number(value, name, minimum=minimum)
    if not isinstance(value, int):
        raise ValueError(f"{name}: integer required")
    return value


def validate_tree(value):
    if isinstance(value, float):
        finite_number(value)
    elif isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            validate_tree(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_tree(item)


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def loads(raw):
    def reject(value):
        raise ValueError(f"non-finite JSON constant: {value}")
    result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject)
    validate_tree(result)
    return result


def dumps(value, **kwargs):
    validate_tree(value)
    return json.dumps(value, allow_nan=False, **kwargs)
