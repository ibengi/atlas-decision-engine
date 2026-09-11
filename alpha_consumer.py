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

TWO TRANSPORTS, ONE AUTHORITY RULE
    On a single host the spool is a directory and the consumer reads it
    directly (`LocalSpoolSource`). On Railway the engine and this service
    are SEPARATE services with SEPARATE volumes, and a Railway volume is
    mounted into exactly one service -- so the directory the engine writes
    is not visible here at all. Across services the consumer pulls the same
    records over the engine's read-only research API instead
    (`HttpSpoolSource`).

    The authority rule is identical either way, which is the point: the
    producer owns the spool and prunes it, the consumer only reads. HTTP
    makes that structural rather than merely observed -- the transport
    offers no way to write, so "the consumer cannot mutate engine state" is
    true by construction and not just by inspection of which paths we open.

    The dependency direction is also preserved. The engine publishes and
    never learns whether anyone read it; if this service is down, stopped or
    was never deployed, the engine is unaffected. The reverse is not
    symmetric and must not be: this service simply gets nothing to analyse.

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

from candidate_contract import (ContractError, FEED_SCHEMA,
                                      LEGACY_FEED_SCHEMAS, REQUIRED_FIELDS,
                                      validate_record, verify_checksum)
from alpha_snapshot import SnapshotError, build_snapshot
from config import CFG, _p
from research_feed import spool_dir
from research_spool import write_all

log = logging.getLogger("ALPHA")

STATE_SCHEMA = "atlas-alpha-processed-v1"

#: A cursor that never advances would page forever. The cap RAISES the
#: paging to a halt and logs it rather than looping, because a silent
#: infinite pull is far harder to notice than a truncated one.
MAX_FEED_PAGES = 50

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
                # AA-12: `os.write` may write fewer bytes than it was given.
                # A short write here leaves a torn line that later reads as a
                # crash-truncated tail, silently losing a processed mark.
                write_all(fd, line.encode("utf-8"))
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


class FeedUnavailable(RuntimeError):
    """The feed could not be read this poll. Not a malformed record."""


class LocalSpoolSource:
    """Same-host transport: read the producer's directory directly.

    Opens files for reading only. Nothing here writes, renames or unlinks
    inside `spool_dir()` -- the producer owns those bytes.
    """

    kind = "local"

    def __init__(self, directory: str = None):
        self.directory = directory or spool_dir()

    def describe(self) -> dict:
        return {"transport": self.kind, "directory": self.directory}

    def records(self) -> list:
        """Every candidate currently spooled, oldest first."""
        try:
            names = sorted(n for n in os.listdir(self.directory)
                           if n.endswith(".json"))
        except OSError:
            return []
        out = []
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError) as e:
                log.debug(f"[ALPHA_CONSUMER] unreadable spool record "
                          f"{name}: {e}")
                out.append(None)
        return out


class HttpSpoolSource:
    """Cross-service transport: pull from the engine's research API.

    GET only, bearer-authenticated with `ALPHA_RESEARCH_API_TOKEN`. There is
    no method here that writes, so the consumer's inability to mutate engine
    state is a property of the transport rather than a convention.

    A feed that cannot be reached raises `FeedUnavailable`. It is NOT an
    empty page: "the engine published nothing" and "we could not ask" are
    different facts, and reporting the second as the first would make an
    outage look like a quiet market.
    """

    kind = "http"

    def __init__(self, base_url: str = None, token: str = None,
                 session=None, page_limit: int = 100):
        self.base_url = (base_url or CFG.ALPHA_RESEARCH_FEED_URL).rstrip("/")
        self._token = token if token is not None else \
            os.getenv("ALPHA_RESEARCH_API_TOKEN", "")
        self.session = session
        self.page_limit = int(page_limit)

    def configured(self) -> bool:
        return bool(self.base_url and self._token)

    def describe(self) -> dict:
        """Never includes the token."""
        return {"transport": self.kind, "url": self.base_url,
                "token_configured": bool(self._token)}

    def records(self) -> list:
        if not self.base_url:
            raise FeedUnavailable("ALPHA_RESEARCH_FEED_URL is not set")
        if not self._token:
            raise FeedUnavailable("ALPHA_RESEARCH_API_TOKEN is not set")
        session = self.session
        if session is None:
            import requests
            session = requests.Session()
        out, cursor, pages = [], "", 0
        while True:
            pages += 1
            if pages > MAX_FEED_PAGES:
                # A cursor that never advances would otherwise spin forever.
                log.warning(f"[ALPHA_CONSUMER] feed paging stopped at "
                            f"{MAX_FEED_PAGES} pages")
                break
            params = {"limit": self.page_limit}
            if cursor:
                params["cursor"] = cursor
            try:
                response = session.get(
                    f"{self.base_url}/api/research/v1/candidates",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params=params, timeout=CFG.ALPHA_RESEARCH_FEED_TIMEOUT_S)
            except Exception as e:                        # noqa: BLE001
                raise FeedUnavailable(
                    f"{type(e).__name__}: {_no_token(e, self._token)}")
            status = getattr(response, "status_code", 0)
            if status != 200:
                raise FeedUnavailable(f"HTTP {status} from the research feed")
            try:
                body = response.json()
            except Exception as e:                        # noqa: BLE001
                raise FeedUnavailable(
                    f"unreadable feed body: {type(e).__name__}")
            rows = body.get("rows") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise FeedUnavailable("feed response carried no rows")
            out.extend(rows)
            nxt = body.get("next_cursor") or ""
            if not body.get("has_more") or not nxt or nxt == cursor:
                break
            cursor = nxt
        return out


def _no_token(value, token: str) -> str:
    """A transport exception can quote the request, headers included."""
    text = str(value)
    if token and len(token) >= 8:
        text = text.replace(token, "<redacted:ALPHA_RESEARCH_API_TOKEN>")
    return text


def default_source():
    """`ALPHA_FEED_TRANSPORT` selects the transport. Explicit, not guessed.

    Defaulting to `local` keeps every single-host deployment working exactly
    as before; a two-service deployment sets `http` and supplies the URL and
    token.
    """
    if str(CFG.ALPHA_FEED_TRANSPORT).strip().lower() == "http":
        return HttpSpoolSource()
    return LocalSpoolSource()


class SpoolConsumer:
    """Reads research candidates and mints immutable snapshots from them.

    Read-only with respect to the spool under either transport.
    """

    def __init__(self, directory: str = None, store: ProcessedStore = None,
                 source=None):
        self.source = source or (LocalSpoolSource(directory) if directory
                                 else default_source())
        self.directory = getattr(self.source, "directory", None)
        self.store = store or ProcessedStore()
        self.stats = {"records_read": 0, "malformed": 0, "duplicates": 0,
                      "minted": 0, "feed_errors": 0,
                      # A record refused because the SOURCE was incomplete or
                      # unattributed, told apart from one that was malformed.
                      "unattributed": 0, "legacy_schema": 0,
                      # AA-04 / AA-01 / AA-09: each refusal reason is its own
                      # counter, because "the feed is quiet", "the feed is
                      # corrupt" and "the feed is deriving quotes" call for
                      # three different operator responses.
                      "checksum_failures": 0, "derived_quotes": 0,
                      "record_errors": 0}
        self.feed_error = None
        #: Why the most recent record was refused, so the caller can record it
        #: instead of silently re-reading the same bad bytes forever.
        self.last_refusal = ""
        #: digest -> contract_id for every record whose checksum this
        #: consumer verified itself this session (AA-15).
        self.verified_digests = {}

    def _valid(self, record):
        """A record this consumer is allowed to mint a snapshot from.

        Delegates to the SHARED contract (`candidate_contract`). AA-02
        found three diverging validators; there is now one, and this method's
        job is to classify the refusal for telemetry, not to re-decide it.

        The contract checks, among the rest:

        SCHEMA      only the current feed schema. A legacy record was
                    permitted to carry a substituted settlement source, a
                    question parsed from a ticker, a "0.0" that meant absent,
                    or a NO side derived from the YES side; it cannot be
                    re-labelled truthful, so it is refused and counted rather
                    than migrated.
        CHECKSUM    AA-04: the digest is RECOMPUTED here and compared. The
                    producer is not trusted, the transport is not trusted, and
                    the bytes on the spool are not trusted.
        OBSERVED    AA-01: every quote must be flagged as directly observed.
                    A quote derived by execution normalization is refused.
        ATTRIBUTED  AA-05: provenance must name an ALLOWED EXACT source path
                    for every required fact -- not merely be a non-empty
                    string.
        TIMED       AA-06: `emitted_at_utc` must be present and valid. This
                    consumer never substitutes its own clock.
        """
        self.last_refusal = ""
        if not isinstance(record, dict):
            self.last_refusal = "record is not an object"
            return None
        if record.get("schema") in LEGACY_FEED_SCHEMAS:
            self.stats["legacy_schema"] += 1
            self.last_refusal = f"legacy schema {record.get('schema')!r}"
            log.warning("[ALPHA_CONSUMER] refusing legacy feed record "
                        f"{record.get('schema')!r}: it may carry substituted "
                        f"market facts and must be re-observed")
            return None
        errors = validate_record(record)
        if errors:
            self.last_refusal = "; ".join(errors)[:300]
            if any("record_sha256" in e for e in errors):
                self.stats["checksum_failures"] += 1
                log.warning(f"[ALPHA_CONSUMER] refusing "
                            f"{record.get('contract_id')}: {errors[0]}")
            elif any("not directly observed" in e for e in errors):
                self.stats["derived_quotes"] += 1
                log.warning(f"[ALPHA_CONSUMER] refusing "
                            f"{record.get('contract_id')}: a quote was "
                            f"derived, not observed -- {errors}")
            else:
                self.stats["unattributed"] += 1
                log.warning(f"[ALPHA_CONSUMER] refusing "
                            f"{record.get('contract_id')}: {errors}")
            return None
        # AA-04 / AA-15: retain the VERIFIED digest so it can be carried into
        # the prediction row and re-checked at settlement.
        self.verified_digests[record["record_sha256"]] = record["contract_id"]
        return record

    def mint(self, record: dict):
        """Record -> immutable `atlas-alpha-v2` snapshot, or None.

        The record's own hash is carried into the snapshot's resolution
        source provenance, so a spool record edited between write and ingest
        changes the snapshot id and cannot masquerade as the original.
        """
        return build_snapshot(
            contract_id=record["contract_id"],
            # No `.get(..., default)` on any market fact: `_valid` has
            # already proved each one is present and attributed, so a KeyError
            # here would mean the guard was bypassed, and crashing is the
            # correct response to that. `event_id` is genuinely optional.
            event_id=record.get("event_id", ""),
            question=record["question"],
            resolution_rules=record["resolution_rules"],
            resolution_source=record["resolution_source"],
            yes_bid=record["yes_bid"], yes_ask=record["yes_ask"],
            no_bid=record["no_bid"], no_ask=record["no_ask"],
            volume=record["volume"],
            open_interest=record["open_interest"],
            # AA-06: passed explicitly and already proved present and
            # well-formed by the contract. `build_snapshot` would otherwise
            # fall back to `datetime.now()`, which makes the SAME evidence
            # mint a DIFFERENT snapshot identity every time it is replayed.
            snapshot_time_utc=record["emitted_at_utc"],
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
        self.feed_error = None
        try:
            raw_records = self.source.records()
        except FeedUnavailable as e:
            # An unreachable feed is reported, never reported as "no
            # candidates": an outage must not be indistinguishable from a
            # quiet market.
            self.stats["feed_errors"] += 1
            self.feed_error = str(e)
            log.warning(f"[ALPHA_CONSUMER] research feed unavailable: {e}")
            return []
        for raw in raw_records:
            # AA-09: ONE malformed row must not starve the batch. Every
            # anticipated per-record failure -- a contract violation, an
            # unparseable timestamp, a number that overflows, a snapshot that
            # cannot be built -- is counted, logged and stepped over. The poll
            # continues to the next record.
            #
            # Deliberately NOT a bare `except Exception`: a programmer error
            # (an AttributeError, a name that does not exist) is not a data
            # problem and must still fail loudly rather than be logged once
            # per record forever.
            try:
                processed = self._consume_one(raw, seen_now)
            except (ContractError, SnapshotError, ValueError, TypeError,
                    OverflowError, ArithmeticError, KeyError) as e:
                self.stats["record_errors"] += 1
                self.stats["malformed"] += 1
                log.warning(f"[ALPHA_CONSUMER] record skipped: "
                            f"{type(e).__name__}: {e}")
                continue
            if processed is None:
                continue
            out.append(processed)
            if limit and len(out) >= limit:
                break
        return out

    def _consume_one(self, raw, seen_now: set):
        """`(snapshot, record)` for one raw row, or None when it is refused.

        Split out of `pending()` so that the per-record containment above has
        exactly one expression to guard, rather than a loop body in which a
        new statement could later be added outside the protected region.
        """
        record = self._valid(raw)
        if record is None:
            self.stats["malformed"] += 1
            # Mark it REJECTED so a permanently invalid record is not re-read,
            # re-validated and re-logged on every poll for the life of the
            # spool. The id is derived from the record's CLAIMED digest, which
            # is fine as a dedup key even though it failed verification -- it
            # identifies the bytes we refused, which is exactly what we want
            # not to look at again.
            if isinstance(raw, dict):
                self._safe_mark(raw, STATUS_REJECTED,
                                getattr(self, "last_refusal", "refused"))
            return None
        self.stats["records_read"] += 1
        try:
            snapshot = self.mint(record)
        except SnapshotError as e:
            # A candidate we cannot turn into a valid snapshot is recorded as
            # rejected so it is not retried every poll.
            self.stats["malformed"] += 1
            self._safe_mark(record, STATUS_REJECTED, str(e))
            return None
        sid = snapshot.market_snapshot_id
        if sid in seen_now or self.store.seen(sid):
            self.stats["duplicates"] += 1
            return None
        seen_now.add(sid)
        self.stats["minted"] += 1
        return (snapshot, record)

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
