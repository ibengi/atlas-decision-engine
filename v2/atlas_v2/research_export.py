"""Offline read-only SQLite snapshot. No service, HTTP endpoint or migration.

Run on the host with the mounted V2 volume. Never copy a live SQLite main file
without its WAL. A single read transaction includes committed WAL contents.
An independently retained runtime anchor is required when importing evidence.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sqlite3

from .domain import Refused, canonical, digest, strict_json
from .data import observation

MAX_EVENTS = 10000
MAX_BYTES = 32 * 1024 * 1024


def verify_snapshot(snapshot, external_anchor):
    if set(snapshot) != {"schema", "events", "anchor", "events_hash"} or snapshot["schema"] != 1:
        raise Refused("snapshot schema")
    events = snapshot["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise Refused("snapshot bound")
    previous = "0" * 64
    identities = set()
    for seq, entry in enumerate(events, 1):
        body = {k: v for k, v in entry.items() if k != "hash"}
        if (entry["seq"] != seq or entry["previous_hash"] != previous
                or entry["hash"] != digest(body) or entry["event_id"] in identities):
            raise Refused("snapshot chain")
        previous = entry["hash"]
        identities.add(entry["event_id"])
    tail = {"schema": 1, "seq": len(events), "hash": previous}
    if snapshot["anchor"] != tail or snapshot["events_hash"] != digest(events):
        raise Refused("snapshot tail/hash")
    if not isinstance(external_anchor, dict) or external_anchor.get("schema") != 1:
        raise Refused("independent anchor required")
    n = external_anchor.get("seq")
    if type(n) is not int or not 0 < n <= len(events) or events[n - 1]["hash"] != external_anchor.get("hash"):
        raise Refused("independent anchor missing; truncation/replacement")
    # The anchor establishes a prefix only; a separately retained final anchor
    # is required to establish that an exported tail has not been omitted.
    return tail


def export_database(database):
    path = Path(database).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise Refused("unknown database schema")
        events, size = [], 0
        for row in db.execute("SELECT * FROM events ORDER BY seq LIMIT ?", (MAX_EVENTS + 1,)):
            entry = dict(row)
            size += len(entry["payload"].encode())
            if len(events) >= MAX_EVENTS or size > MAX_BYTES:
                raise Refused("export bound exceeded; no partial export admitted")
            entry["payload"] = strict_json(entry["payload"])
            events.append(entry)
        anchor = {"schema": 1, "seq": len(events), "hash": events[-1]["hash"] if events else "0" * 64}
        result = {"schema": 1, "events": events, "anchor": anchor, "events_hash": digest(events)}
        if events:
            verify_snapshot(result, anchor)  # internal integrity, NOT external attribution
        if len(canonical(result)) > MAX_BYTES:
            raise Refused("serialized export bound exceeded")
        return result
    finally:
        db.close()


def observations_from_snapshot(snapshot, external_anchor):
    verify_snapshot(snapshot, external_anchor)
    if external_anchor != snapshot["anchor"]:
        raise Refused("final runtime anchor required for complete research cohort")
    by_hash = {e["hash"]: e for e in snapshot["events"]}
    result = []
    for event in snapshot["events"]:
        if event["kind"] != "OBSERVATION":
            continue
        value = event["payload"]
        receipt, scan = by_hash.get(value["receipt_hash"]), by_hash.get(value["scan_hash"])
        if (not receipt or not scan or receipt["kind"] != "RAW_HTTP" or scan["kind"] != "SCAN"
                or not receipt["seq"] < scan["seq"] < event["seq"]
                or scan["payload"].get("complete") is not True
                or scan["payload"].get("terminal_cursor") != ""
                or receipt["hash"] not in scan["payload"].get("pages", [])):
            raise Refused("observation provenance")
        raw = base64.b64decode(receipt["payload"]["body_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != receipt["payload"]["body_sha256"]:
            raise Refused("raw receipt hash")
        data = strict_json(raw)
        matches = [r for r in data["markets"] if digest(r) == value["raw_market_hash"]]
        if len(matches) != 1 or observation(matches[0], receipt, scan) != value:
            raise Refused("observation differs from native raw receipt")
        result.append({"hash": event["hash"], **value})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("output")
    args = parser.parse_args()
    result = export_database(args.database)
    with open(args.output, "xb") as file:
        file.write(canonical(result))
    print(json.dumps({"anchor": result["anchor"], "events_hash": result["events_hash"]}))


if __name__ == "__main__":
    main()
