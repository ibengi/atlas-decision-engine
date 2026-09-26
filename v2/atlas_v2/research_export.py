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
import gzip
import io
from collections import Counter
from urllib.parse import urlsplit, parse_qs

from .domain import Refused, canonical, digest, strict_json, utc
from .data import observation

MAX_EVENTS = 50000
MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 60000
PUBLIC_KINDS = frozenset({"RAW_HTTP", "SCAN", "OBSERVATION", "REJECTED_OBSERVATION", "SCAN_FAILED"})


def public_research_only(snapshot):
    """Reject mixed ledgers in full; never drop private rows and claim completeness."""
    for event in snapshot["events"]:
        value = event["payload"]
        if event["kind"] not in PUBLIC_KINDS:
            raise Refused("export contains non-public research event")
        if event["kind"] == "RAW_HTTP":
            url = urlsplit(value["url"])
            query = parse_qs(url.query)
            if (url.scheme != "https" or url.netloc != "external-api.kalshi.com"
                    or url.path != "/trade-api/v2/markets" or url.fragment
                    or value.get("method") != "GET" or value.get("series") != "KXBTC15M"
                    or query.get("series_ticker") != ["KXBTC15M"]
                    or query.get("status") != ["open"]
                    or set(query) - {"series_ticker", "status", "limit", "cursor"}):
                raise Refused("export outside fixed V2 public collection scope")
        if event["kind"] == "OBSERVATION":
            if value.get("schema") != "atlas-v2-observation/1" or not value["ticker"].startswith("KXBTC15M-"):
                raise Refused("non-V2 observation")


def dataset_metadata(snapshot):
    public_research_only(snapshot)
    rows = [e["payload"] for e in snapshot["events"] if e["kind"] == "OBSERVATION"]
    times = sorted((r["observed_at"] for r in rows), key=utc)
    return {"schema_version":"atlas-v2-research-export/1", "dataset_sha256":digest(snapshot),
            "event_count":len(snapshot["events"]), "observation_count":len(rows),
            "distinct_markets":len({r["ticker"] for r in rows}),
            "first_observation_at":times[0] if times else None,
            "last_observation_at":times[-1] if times else None,
            "event_kind_counts":dict(sorted(Counter(e["kind"] for e in snapshot["events"]).items())),
            "anchor":snapshot["anchor"], "events_hash":snapshot["events_hash"],
            "filtering":"NONE: complete V2 public ledger including raw pages and rejected/failed scans"}


def write_bundle(database, directory):
    """Private volume artifact only; no public endpoint and no database write."""
    snapshot = export_database(database)
    metadata = dataset_metadata(snapshot)
    raw = canonical(snapshot)
    encoded = base64.b64encode(gzip.compress(raw, mtime=0))
    chunks = [encoded[i:i+CHUNK_BYTES] for i in range(0,len(encoded),CHUNK_BYTES)]
    manifest = {**metadata, "encoding":"gzip+base64", "uncompressed_bytes":len(raw),
                "chunks":[{"name":f"part-{i:04d}.txt", "bytes":len(chunk),
                           "sha256":hashlib.sha256(chunk).hexdigest()} for i,chunk in enumerate(chunks)]}
    destination = Path(directory)/metadata["dataset_sha256"]
    destination.mkdir(parents=True, exist_ok=True)
    # A retry can reuse identical artifacts but can never replace different bytes.
    for name, content in [(c["name"],b) for c,b in zip(manifest["chunks"],chunks)] + [("manifest.json",canonical(manifest))]:
        path = destination/name
        try:
            with path.open("xb") as file: file.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise Refused("existing export artifact differs")
    return {"manifest_path":str(destination/"manifest.json"),"manifest_sha256":digest(manifest),**metadata}


def read_bundle(directory, expected_manifest_hash, external_anchor):
    directory = Path(directory)
    manifest = strict_json((directory/"manifest.json").read_bytes())
    if digest(manifest) != expected_manifest_hash:
        raise Refused("native export manifest mismatch")
    if not 0 < manifest["uncompressed_bytes"] <= MAX_BYTES or len(manifest["chunks"]) > 2*MAX_BYTES//CHUNK_BYTES+2:
        raise Refused("bundle bounds")
    encoded = []
    for i, part in enumerate(manifest["chunks"]):
        if part["name"] != f"part-{i:04d}.txt" or not 0 < part["bytes"] <= CHUNK_BYTES:
            raise Refused("chunk sequence/bounds")
        raw = (directory/part["name"]).read_bytes()
        if len(raw) != part["bytes"] or hashlib.sha256(raw).hexdigest() != part["sha256"]:
            raise Refused("export chunk incomplete/corrupt")
        encoded.append(raw)
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(b"".join(encoded),validate=True))) as file:
        raw = file.read(MAX_BYTES+1)
    if len(raw) != manifest["uncompressed_bytes"] or len(raw) > MAX_BYTES:
        raise Refused("decoded export size")
    snapshot = strict_json(raw)
    verify_snapshot(snapshot,external_anchor)
    metadata = dataset_metadata(snapshot)
    if any(manifest.get(k) != v for k,v in metadata.items()):
        raise Refused("dataset metadata/hash mismatch")
    return snapshot


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
