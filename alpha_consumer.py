"""Consumer side of the research boundary. SHADOW ONLY.

Alpha Gateway phase 2, section 1.

THE AUTHORITY BOUNDARY, STATED AS A RULE ABOUT DIRECTORIES
    The producer (`research_feed`, running in the ENGINE process) owns the
    spool directory: it writes records there and prunes them. The consumer
    (this module, running in the ALPHA SERVICE process) owns its own state
    file and NEVER writes into the spool.

    That is what "the consumer must not have authority to mutate
    scanner/execution state" means concretely. It is not a promise about
    intent, it is a property of which paths each side opens for writing, and
    `tests/test_alpha_automatic_feed.py` asserts the spool's bytes are
    unchanged by a full consume cycle.

    The cost of that rule is that the consumer cannot delete what it has
    processed, so the producer prunes by age and count instead. That is the
    right trade: a consumer that can delete producer files is a consumer
    that can destroy evidence the engine has not finished writing.

DEDUPLICATION IS BY SNAPSHOT IDENTITY, NOT BY FILE
    `market_snapshot_id` is derived from the snapshot's content, so two
    spool records describing the same market at the same prices at the same
    second mint the same id and the second is a duplicate. A record that
    differs in any bound field -- a moved price, a later timestamp -- is a
    genuinely new observation and is analysed.

    The processed set is persisted append-only, so a restart does not
    re-analyse (and re-pay for) work already done. `TERMINAL` states are
    final; a transient state can be retried.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from alpha_snapshot import SnapshotError, build_snapshot
from config import CFG, _p
from research_feed import FEED_SCHEMA, spool_dir

log = logging.getLogger("ALPHA")

STATE_SCHEMA = "atlas-alpha-processed-v1"

#: Outcomes that end a snapshot's life. Anything else may be retried on a
#: later poll (a budget refusal, for instance, is worth retrying tomorrow).
STATUS_ANALYZED = "ANALYZED"
STATUS_REJECTED = "REJECTED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_DEFERRED = "DEFERRED"
TERMINAL_STATUSES = (STATUS_ANALYZED, STATUS_REJECTED, STATUS_DUPLICATE)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProcessedStore:
    """Append-only record of which snapshots this service has handled."""

    def __init__(self, path: str = None):
        self.path = path or _p(CFG.ALPHA_STATE_FILE)
        self._cache = None

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        out = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except OSError as e:
                raise RuntimeError(f"processed store unreadable: {e}")
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    if i == len(lines) - 1:
                        break              # torn tail, crash mid-append
                    log.error(f"[ALPHA_CONSUMER] unparsable state row at line "
                              f"{i + 1} -- skipped, NOT treated as end of file")
                    continue
                if isinstance(row, dict) and row.get("market_snapshot_id"):
                    # Later rows supersede earlier ones for the same id: a
                    # DEFERRED snapshot that is later ANALYZED must read as
                    # analysed.
                    out[row["market_snapshot_id"]] = row
        self._cache = out
        return out

    def status(self, snapshot_id: str):
        return (self._load().get(snapshot_id) or {}).get("status")

    def seen(self, snapshot_id: str) -> bool:
        return self.status(snapshot_id) in TERMINAL_STATUSES

    def mark(self, snapshot_id: str, status: str, *, contract_id: str = "",
             detail: str = "", prediction_id: str = "") -> dict:
        row = {"schema": STATE_SCHEMA, "at": _iso(), "ts": time.time(),
               "market_snapshot_id": snapshot_id, "contract_id": contract_id,
               "status": status, "detail": str(detail)[:300],
               "prediction_id": prediction_id}
        line = json.dumps(row, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str) + "\n"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o644)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            # If we cannot remember that we processed this, a restart will
            # process it again and pay for it again. Loud, and the caller
            # stops consuming this cycle.
            raise RuntimeError(f"processed status not durable: {e}")
        if self._cache is not None:
            self._cache[snapshot_id] = row
        return row

    def counts(self) -> dict:
        out = {}
        for row in self._load().values():
            out[row.get("status")] = out.get(row.get("status"), 0) + 1
        return out


class SpoolConsumer:
    """Reads research candidates and mints immutable snapshots from them.

    Read-only with respect to the spool: `pending()` opens files for reading
    and nothing here ever writes, renames or unlinks inside `spool_dir()`.
    """

    def __init__(self, directory: str = None, store: ProcessedStore = None):
        self.directory = directory or spool_dir()
        self.store = store or ProcessedStore()
        self.stats = {"records_read": 0, "malformed": 0, "duplicates": 0,
                      "minted": 0}

    def _record_paths(self) -> list:
        try:
            return [os.path.join(self.directory, n)
                    for n in sorted(os.listdir(self.directory))
                    if n.endswith(".json")]
        except OSError:
            return []

    def _read(self, path: str):
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError) as e:
            log.debug(f"[ALPHA_CONSUMER] unreadable spool record "
                      f"{os.path.basename(path)}: {e}")
            return None
        if not isinstance(record, dict) or record.get("schema") != FEED_SCHEMA:
            return None
        return record

    def mint(self, record: dict):
        """Record -> immutable `atlas-alpha-v2` snapshot, or None.

        The record's own hash is carried into the snapshot's resolution
        source provenance, so a spool record edited between write and ingest
        changes the snapshot id and cannot masquerade as the original.
        """
        return build_snapshot(
            contract_id=record["contract_id"],
            event_id=record.get("event_id", ""),
            question=record["question"],
            resolution_rules=record.get("resolution_rules", ""),
            resolution_source=record.get("resolution_source", "kalshi"),
            yes_bid=record["yes_bid"], yes_ask=record["yes_ask"],
            no_bid=record["no_bid"], no_ask=record["no_ask"],
            volume=record.get("volume", 0.0),
            open_interest=record.get("open_interest", 0.0),
            snapshot_time_utc=record.get("emitted_at_utc"),
            market_close_time_utc=record["market_close_time_utc"],
            expected_resolution_time_utc=record["expected_resolution_time_utc"],
            catalyst_name=record.get("catalyst_name", ""),
            catalyst_time_utc=record.get("catalyst_time_utc"))

    def pending(self, limit: int = None) -> list:
        """`[(snapshot, record), ...]` not yet processed, oldest first.

        Deduplicated within the batch as well as against the store: the
        producer can legitimately emit the same market twice in one window
        and paying two vendors twice for it would be a straightforward waste.
        """
        out, seen_now = [], set()
        for path in self._record_paths():
            record = self._read(path)
            if record is None:
                self.stats["malformed"] += 1
                continue
            self.stats["records_read"] += 1
            try:
                snapshot = self.mint(record)
            except SnapshotError as e:
                # A candidate we cannot turn into a valid snapshot is
                # recorded as rejected so it is not retried every poll.
                self.stats["malformed"] += 1
                self._safe_mark(record, STATUS_REJECTED, str(e))
                continue
            sid = snapshot.market_snapshot_id
            if sid in seen_now or self.store.seen(sid):
                self.stats["duplicates"] += 1
                continue
            seen_now.add(sid)
            self.stats["minted"] += 1
            out.append((snapshot, record))
            if limit and len(out) >= limit:
                break
        return out

    def _safe_mark(self, record: dict, status: str, detail: str) -> None:
        """Mark a record we could not mint. Its id is derived from the
        record hash, since there is no snapshot id to use."""
        try:
            self.store.mark("bad-" + str(record.get("record_sha256", ""))[:24],
                            status,
                            contract_id=str(record.get("contract_id", "")),
                            detail=detail)
        except RuntimeError as e:
            log.error(f"[ALPHA_CONSUMER] {e}")

    def snapshot_stats(self) -> dict:
        return dict(self.stats)
