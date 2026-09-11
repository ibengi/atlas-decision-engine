# Astra v3 — the eleven findings the re-audit returned as REJECTED

**Branch:** `alpha/astra-candidate-feed-v3-remediation`
**Base:** `alpha/astra-candidate-feed-v2-remediation` @ `a304adbd03bb4e115ed5a86ca40cd45fce47204c` (untouched)
**Verdict sought:** `SAFE_FOR_INDEPENDENT_REAUDIT` — **not** production-ready, **not** CAPITAL-ready.

Astra independently re-audited `a304adb` and returned **REJECTED**. Eight
findings passed and are not revisited here: **AA-01, AA-04, AA-05, AA-06,
AA-07, AA-08, AA-09, AA-18**. Their tests are untouched, and a fix below that
weakened one of them would fail in `test_astra_aa01_aa18_remediation.py`.

This document does not defend `a304adb`. Where the v2 remediation was wrong
it is said plainly, including where a v2 *test* encoded the rejected
behaviour and had to be corrected rather than kept.

---

## 1. Protected areas

| Constraint | Result |
|---|---|
| `BROKER_WRITES` | **0** |
| `CAPITAL_CHANGES` | **0** |
| `MAIN_CHANGES` | **0** — `origin/main` at `5c4e789`, untouched |
| `PRODUCTION_DEPLOYS` | **0** |
| `HISTORICAL_LEDGER_REWRITES` | **0** |
| Rejected candidate modified | **0** — `a304adb` is still `a304adb` |
| Audited safety candidate modified | **0** |
| Railway variables touched | **0** |

No `ALLOW_ORDER_SUBMISSION`, `LIVE_BROKER_WRITES_AUTHORIZED`,
`MODEL_APPROVED`, `DAILY_RESEARCH_ORACLE_APPROVED`, `KILL_SWITCH`, risk
threshold, drawdown guard, order cap, credential or execution-mode value was
read, written or moved. `order_manager.py`, `kalshi_client.py`,
`risk_manager.py`, `position_sizer.py`, `market_validator.py` and
`execution_engine.py` are byte-identical to `a304adb`.

---

## 2. The eleven findings

### AA-02 — a numeric settlement-source member was coerced into a name

`{"name": 12345}` became the settlement source `"12345"`. `str(name).strip()`
ran after excluding only dicts, lists and booleans, so an integer — a
plausible internal identifier and a wholly implausible settlement authority —
became a string nothing downstream could tell from a name somebody published.
`{"url": 8080}` did the same. The `name or url` fallback also fired on the
empty string, reporting a URL as the authority's name.

**Closed by** type-checking before any coercion, in
`research_feed._settlement_source_name`. One malformed member taints the
collection: a settlement source list that is half readable is not half true.

### AA-03 — a contradiction was downgraded to an ordinary absence

`resolve_alias` correctly refused to choose between `event_ticker=A` and
`event_id=B`. The caller caught the error and wrote the field into
`unavailable_fields` — the same word for "the exchange did not publish this"
and "the exchange published two incompatible answers". The first is a quiet
market; the second is a source contradicting itself, and reporting it as the
first destroys the only signal an operator could act on. Worse, `event_id` is
optional, so a contradiction on it became a permitted absence and the record
was emitted.

**Closed by** a dedicated `AliasContradiction` carrying every conflicting
value, a `contradictory_fields` map that is **inside the record digest**, and
a contract rule that refuses any record carrying one — required field or
optional.

**A v2 test asserted the rejected behaviour.**
`AA03_..._makes_the_fact_absent_not_arbitrary` is superseded by
`AA03_..._is_preserved_as_a_contradiction`, which asserts strictly more: the
fact is still not chosen, *and* the disagreement is preserved by name and
refuses the record.

### AA-10 — research LOGGING was still synchronous in the engine cycle

The v2 fix moved `write`, `fsync` and `prune` onto a writer thread and left
`log.info`, `log.warning` and `log.debug` on the caller's. `logging.Handler.emit`
takes a lock and writes synchronously, and the research logger's handler
writes to the volume the fsync was moved off — so a stalled volume still
stalled the decision cycle, through the diagnostics instead of the data.

**Closed by** `ResearchWriter.note()`: diagnostics travel the same bounded
queue as records, `put_nowait`, dropped and counted under pressure, and the
`log` call happens on the writer's thread. A static test proves no `log.*`
call is reachable from the emit path.

### AA-11 — the spool bound did not account for partial writes

Three holes, all reachable from one crash. **Count**: `max_records` counted
complete files only, so N interrupted writes let the spool hold
`max_records + N`. **Recovery**: `recover_temp_files()` existed, nothing
called it, and it only removed partials older than six hours — through the
window in which the next crash happens. **Reserve**: `scan → decide → create`
had no lock, so two writers both took the last slot.

**Closed by** `capacity()` (complete + partial + bytes), ownership-tagged
partials (`<name>.<pid>.partial`) recovered at construction when their owner
is no longer running, and one advisory lock held across
prune → scan → decide → reserve → write → fsync → rename. Unknown files are
still never removed, at any age. Holding the lock across the fsync is safe
*here and only here* because AA-10 guarantees the engine never waits for this
function.

### AA-12 — the processed store never got the durable append protocol

`durable_append` was written for AA-12 and the ledger was taught to use it.
`ProcessedStore.mark` kept its own `os.open` + `write_all` + `fsync`, with no
torn-tail separation and no lock. A mark written after an interrupted one was
spliced onto the broken line and **both** were lost; the store then reported
the snapshot unprocessed and the service paid again for an analysis it may
already have committed. `_load()` compounded it by treating a torn last line
as a clean end-of-file.

**Closed by** routing every write through `serialized_append`, and by
reporting a torn row instead of swallowing it.

### AA-13 — readable bytes were treated as a durable commit

`prediction_is_committed()` answered by reading the ledger back and finding
the row. `write()` returning means the bytes are in the page cache, where
they read back perfectly while still being one power cut from never having
existed. When the `fsync` failed, `record_prediction` raised — and the same
bytes still read back, so the commit check said "safe" about a prediction the
caller had just been told was lost, and a TERMINAL acknowledgement was
published for it.

**Closed by** a `COMMIT` receipt row, appended after the prediction and
fsynced in its own right. It can only exist because the prediction's append
returned, which happens only after that append's own fsync succeeded.

Two further halves of AA-13:

* **Correction 7 — recover before re-dispatching.** A crash between the
  prediction commit and the processed mark is the ordinary case: they are two
  files. The next poll re-minted, called **every provider again**, and only
  then asked the ledger, which refused correctly — after the money was spent,
  and reporting a `prediction_id` naming a row that was never written.
  `committed_prediction()` is now consulted first, and returns the committed
  identity with zero provider calls.
* **Correction 8 — PREPARE is a precondition.** It was written, every
  exception caught, the failure called "not fatal", and dispatch proceeded.
  But the case PREPARE exists for is "the ledger is not writable", in which
  the prediction cannot be committed either — so the spend was guaranteed
  unrecoverable before it was incurred. No durable PREPARE, no dispatch, and
  the snapshot is DEFERRED. An acknowledgement also never names a
  `prediction_id` that was not durably committed; the attempted id is kept in
  `detail`, where it reads as a diagnostic rather than a row to join on.

### AA-14 — only two of the six appenders were serialized

`record_prediction` and `resolve` took the lock. `invalidate`,
`record_observation`, `record_costs` and `ProcessedStore.mark` did not — and
every one of them is a check-then-append. `append_line` reads the file's last
byte to decide whether a torn tail needs separating, so even a pure append is
a check-then-act. Being append-only does not make a writer safe; it makes the
damage permanent.

`ProcessedStore._cache` was the other half: filled once, never invalidated,
so one writer's marks stayed invisible to another for the life of the
process.

**Closed by** serializing every appender (the complete list is in
`docs/design/alpha-writer-model.md`, pinned by a static test) and keying the
cache on a generation — `(size, mtime_ns, inode)` — rebuilt whenever the file
moves underneath it.

### AA-15 — the "verified join" verified only what it was given

The v2 rule checked every binding field the settlement supplied and said so:
"fields the settlement does not supply are simply not checked". Written out,
that means a settlement supplying **no** binding at all passed every check
there was — and a `prediction_id` is an opaque token, so matching it proves
somebody quoted a token.

**Closed by** `REQUIRED_BINDING` (`contract_id`, `market_snapshot_id`,
`source_record_sha256`). Any of them missing on either side quarantines the
row with the missing names reported. Partial agreement is not partial proof.

Two further halves of AA-15:

* **Correction 12 — an unqualified source is refused, not trusted.**
  `trusted_sources` was off by default and off meant *accept anything*. The
  reason given for it being off — no authority has been qualified — is an
  argument for the opposite behaviour. Default is now REFUSE, and the
  operator's statement of which feed they verified is preserved into the
  resolution row and into `resolved()`, so a calibration number can be traced
  to the authority that produced its outcomes.
* **Correction 11 — the evidence outlives the spool.** The prediction carried
  `record_sha256` and nothing else. A digest is a claim *about some bytes*,
  and those bytes lived in the bounded, pruned spool. Six hours later the
  digest was a 64-character string nothing could check — exactly when a
  settlement arriving days later was compared against it. Comparing two
  copies of an unverifiable claim is not verification. The canonical source
  content is now persisted with the prediction and
  `alpha_ledger.verify_source_evidence` recomputes the digest from it.

### AA-16 — protection guarded a path the store does not use

The ledgers were protected by their real paths. The processed store was
protected by a reconstruction: `os.path.join(directory, CFG.ALPHA_STATE_FILE)`.
`ProcessedStore` resolves `_p(CFG.ALPHA_STATE_FILE)`, against `DATA_DIR`, and
the report directory need not be `DATA_DIR`. With a nondefault relative state
file, or a report published elsewhere, the guarded path was a file nobody
writes while the file the service appends every processed mark to was left
open — and a report published over it destroys the record of every analysis
already paid for, in one `os.replace`.

**Closed by** protecting the store's own path when one is supplied, plus the
configured path resolved the way the store resolves it, plus the old reading.

### AA-17 / M07P — a complete record whose quotes are declared derived

M07 asked what happens when the quote-observation check is deleted. M07P asks
the harder question: with the check intact, what happens to a record that is
complete, correctly checksummed, correctly attributed, and simply says —
truthfully — that its quotes were derived? That is not hypothetical:
`MarketValidator.normalize_book` derives a missing NO side for the order path
on purpose, and is right to.

**Asserted by effect, not by counter**: zero snapshots minted, zero
predictions, zero commit receipts, zero rows in the ledger, refused at the
producer and at the readiness gate as well. One derived quote out of four is
enough. `M07P` is in the mutation probe and is KILLED.

### NEW-01 — a malformed number escaped the contract as an exception

`yes_ask = 10**500`. `_check_book` called `float()` inside
`except (KeyError, TypeError, ValueError)`, and `float(10**500)` raises
`OverflowError`. So the one function whose entire contract is to return a
structured refusal **raised** — and the exception travelled out through
`SpoolConsumer.pending()`, so one hostile row stopped the whole batch. That
is the AA-09 failure returning through a different door.

**Closed by** using `strict_number` in the crossed-book check, catching
`OverflowError`/`RecursionError` in the digest path, and making
`validate_record` **total**: any unexpected exception becomes a REFUSAL
naming it. Failing closed on a value we do not understand is the only answer
a validator is allowed to give.

---

## 3. Evidence

Every finding above was reproduced as a failing test against `a304adb`
BEFORE its fix. The reproductions are kept rather than replaced, so a
regression reintroduces a named, dated counterexample.

| Requirement | Where |
|---|---|
| Every remaining counterexample reproduced | `tests/test_astra_v3_remediation.py` — 11 classes, each named for its finding |
| Crash / restart | `AA12_...`, `AA13_...`, `AA13b_...` (recovery without re-dispatch), `AA13c_...` (PREPARE precondition) |
| Mixed writers | `AA14_UnserializedAppendersAndStaleCaches` — 8-thread races on invalidations, observations, cost rows and processed marks |
| Spool partial / capacity | `AA11_PartialWritesEscapedTheBound` — count, byte, recovery and two-writer reservation |
| Settlement omission | `AA15_PartialBindingWasAcceptedAsVerified` — each required field individually |
| M07P | `M07P_CompleteRecordWithDerivedQuotes` |
| Zero surviving safety mutations | `tools/astra_mutation_probe.py` |

Mutations `M12`–`M25` were added, one per newly closed invariant, so the
"zero survivors" claim covers the v3 fixes and not only the v2 ones.

```
python tools/astra_mutation_probe.py     ->  26/26 killed, 0 survivor(s)
python run_tests.py                      ->  1916 tests, 0 failures,
                                             0 errors, 0 skipped
python -m unittest tests.test_astra_v3_remediation  ->  92 tests, OK
pytest -k "AA11 or AA12 or AA13 or AA13b or AA13c or AA14 or AA15 or M07P"
                                         ->  59 passed
```

### Three defects this work found in itself

Both are recorded because a remediation that only reports what it set out to
fix is a remediation nobody can calibrate.

* **The receipt poisoned a snapshot after one failed fsync.** With the
  `COMMIT` receipt required and the duplicate check unchanged, a prediction
  row whose fsync failed was still FOUND by the duplicate check — so every
  retry was refused as "already committed" while `prediction_is_committed`
  said False. The snapshot could never be committed and never be retried.
  Found by self-review before the commit. The recovery is to FINISH the
  commit rather than start another: the receipt's own append fsyncs the whole
  file, so the earlier row becomes durable at the same moment its receipt
  does, and the ORIGINAL prediction id survives.

* **A dead `except` around a generator-based context manager.**
  `exclusive_lock` is a `@contextlib.contextmanager`, so calling it runs
  nothing: every failure it can have — opening the sidecar, acquiring the
  lock, timing out — surfaces at `__enter__`. The `try/except OSError`
  wrapped around the *call* was therefore dead code, and an unopenable lock
  file would have escaped `BoundedSpool.write`, which promises never to raise
  for an expected condition. Found by re-reading the diff after the commit.

* **`M19` survived the first full probe run.** Removing the total wrapper
  from `validate_record` was NOT detected, because `_check_book` had also
  been fixed to use `strict_number` — so the hostile input no longer reached
  the wrapper at all. The wrapper was being exercised only through the one
  hole it was added to cover, which is a second test of the hole rather than
  a test of the wrapper. Closed by asserting totality directly: make an
  internal helper raise `OverflowError`, `RecursionError`, `MemoryError` or
  `ZeroDivisionError` and require a structured refusal anyway.

---

## 4. What this report does not say

It does not say production-ready. It does not say CAPITAL-ready. The external
blockers recorded in the AA-01..AA-18 report are **unchanged**: Railway `/data`
restart proof (R1), a real Astra identity (R3, `ASTRA_IDENTITY_UNPROVEN`),
live exchange schema capture (`LIVE_SCHEMA_UNPROVEN`), and settlement
authority qualification — which is now a refusal by default rather than a
caveat in prose.

A checksum proves byte integrity, never source authenticity. A completed,
fsynced append proves a durable sequence was attempted and confirmed; it does
not promise a device that lies about `fsync` is telling the truth.

The only positive endpoint sought is **`SAFE_FOR_INDEPENDENT_REAUDIT`**, and
that judgement belongs to the counter-audit, not to this document.
