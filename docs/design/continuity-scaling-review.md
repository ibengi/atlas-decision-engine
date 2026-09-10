# A32: continuity scaling review

Status: **NEEDS_REVIEW — explicit scalability limitation retained.**

This review inspected commit `90e460f091438534f50e4963cf04383d7532779c`.
No chain format, historical evidence, rollback guard, root hash check, or
production code was changed for A32. No compaction was performed. CAPITAL
remained OFF; the benchmark imported no broker client and made no network calls.

## Measured behavior

`ContinuityChain.append` deliberately discards its parsed cache, validates the
complete chain before appending, writes and fsyncs the new record, validates the
complete chain again for readback, and rereads the complete file to register its
digest in the root manifest. `Transaction.__enter__`, commit readbacks, and
`recovery_problem` also hash authoritative files. `evidence_floor` and
`consumed_tokens` scan the parsed history even when parsing is cached.

Let H be the continuity file's bytes and R be all authoritative file bytes in the
root. An append performs O(H + R) byte work and O(number of records) parsing and
validation. With approximately fixed-size records and an ever-growing history,
N successive appends perform O(N²) cumulative work. The parsed cache does not
make authoritative append incremental. The manifest itself grows primarily
with the number of state files, not the number of history records.

The following local synthetic measurements use the exact base code, three fresh
temporary roots per size, median elapsed wall time, and a chain containing only
synthetic evidence records. Fixture generation is excluded. The cold read
includes full record validation. The append includes its ordinary local durable
transaction, fsyncs, and readbacks. The root-hash measurement includes only this
one authoritative history file. An external continuity provider is absent, so
these results do **not** include external proof latency or capital operation.

| Existing records | History bytes | Cold validation (ms) | One append (ms) | Root hash check (ms) |
|---:|---:|---:|---:|---:|
| 100 | 37,084 | 2.160 | 5.401 | 0.110 |
| 1,000 | 372,786 | 25.022 | 54.787 | 0.507 |
| 5,000 | 1,872,786 | 115.389 | 253.740 | 2.345 |
| 10,000 | 3,747,788 | 266.640 | 489.506 | 4.598 |

These are diagnostic measurements on this scratch filesystem, not a production
latency guarantee. Cache state, CPU scheduling, storage durability behavior,
record size, and the rest of the economic state affect results. The measurements
confirm the growth visible in the code; they do not establish a supported
production history size or an acceptable trading latency budget.

## Decision for this remediation

Retain the full validation path. A tail-only parsed cache would remove only part
of the cost while leaving full-root hashing in place. Trusting file size or
mtime would weaken the existing content-based check. Merely signing a hash of
old bytes does not establish that unread mutable local files still contain those
bytes. An arbitrary changed byte in an unread historical segment cannot be
detected by checking only a new tail or a cached digest.

Consequently, an incremental redesign that preserves the current detection
timing requires either reading historical bytes or an additional independently
qualified immutable-storage integrity boundary. That boundary has not been
selected or demonstrated here. Introducing a new history format, archive
recovery, and trust boundary during blocker remediation is not justified by
these small measurements alone.

The limitation is operationally visible: history growth increases time holding
the root writer lock and increases restart validation time. There is no hidden
retention cap, record dropping, token-set truncation, background compaction, or
fallback that makes a long history appear empty. Capacity qualification and an
explicit operating history budget remain necessary before production operation.

## Requirements for a future incremental design

A follow-up design can explore sealed immutable segments plus one active tail,
but must satisfy all of the following before replacing the existing checks:

1. Preserve every historical record and its order. Retain the existing chain or
   a lossless, explicitly versioned mapping verified against it. Migration is a
   reviewed durable transaction; no silent compaction or archive deletion.
2. Bind each segment's byte length, record interval, digest, preceding segment
   commitment, final record hash, and schema version into an ordered commitment.
   A manifest or Merkle root by itself is a commitment, not proof that the
   underlying current files were inspected or retained.
3. Bind the aggregate commitment, active-tail boundary, complete economic-state
   digest, account identity, environment, local generation, fresh challenge,
   issuance time, and monotonic authority checkpoint into independently
   authenticated evidence. Verify it with pinned trust established outside the
   rollback domain. The verifier must reject echo, replay, stale generation,
   unknown authority, and mismatched account/environment proofs.
4. Retain independent authority state outside the economic volume and require a
   current proof on restart. Restoring an entire older local volume must remain
   distinguishable from the current checkpoint. Historical archive checkpoints
   may verify archived state, but cannot authorize it as the current live state.
5. Specify and qualify how sealed segment bytes remain immutable and available.
   Local POSIX permissions, cached stat fields, or a filename containing a hash
   are insufficient evidence. Without a stronger storage integrity guarantee,
   retain full content verification at the same authority boundaries used today.
6. Independently verify all old state needed by recovery, risk floors, consumed
   token checks, corrections, and audit export. Authenticate any incremental
   summaries against their complete input interval and retain a full-history
   rebuild that detects omissions, reordered records, or altered summaries.
7. Define crash points for segment seal, new-tail creation, root manifest update,
   authority advancement, and publication. An interrupted seal or archive loss
   must stay fail-closed and must never discard a consumed token or adverse
   economic event. Readers and stale writers retain generation fencing.
8. Benchmark complete engine workloads and restart, not only chain append.
   Compare lock occupancy, memory, provider latency, and historical verification
   costs. Require independent review of the changed integrity assumptions.

No segment/checkpoint optimization is claimed implemented by this document.

## Reproducing the local measurement

Run from a disposable checkout of the exact base commit with the repository's
`tools/astra_regressions/run_isolated.py` launcher. Save the Python below outside
the repository as `a32_benchmark.py`. Invoke:

```sh
python tools/astra_regressions/run_isolated.py "$PWD" /tmp/atlas-a32-results.json /absolute/path/a32_benchmark.py
```

The launcher removes real credentials, sets temporary DATA_DIR, and installs its
network-denial audit hook. No dependency installation is necessary. The script
creates and removes separate temporary roots and does not write repository state.
The execution environment used for the table ran the same five base modules
(`continuity.py`, `strict_data.py`, `state_authority.py`, `persistence.py`,
`config.py`) copied with `git show <base-sha>:<path>` into a scratch snapshot;
this prevented parallel remediation edits from changing the benchmarked code.

```python
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from continuity import ContinuityChain, KIND_EVIDENCE, GENESIS
from state_authority import remember_file, recovery_problem
from strict_data import dumps

def seed_records(count):
    previous = GENESIS
    lines = []
    for sequence in range(1, count + 1):
        record = {
            "version": 1, "seq": sequence, "prev": previous,
            "kind": KIND_EVIDENCE, "at": "2026-09-10T00:00:00Z",
            "payload": {
                "settled_count": sequence,
                "digest": hashlib.sha256(str(sequence).encode()).hexdigest(),
                "strategy_equity": 100.0, "realized_pnl_cum": 0.0,
            },
        }
        record["hash"] = ContinuityChain.record_hash(record)
        previous = record["hash"]
        lines.append(dumps(record, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()

results = []
for size in (100, 1000, 5000, 10000):
    raw = seed_records(size)
    cold, append, root = [], [], []
    for repeat in range(3):
        with tempfile.TemporaryDirectory(prefix="atlas-a32-") as directory:
            target = Path(directory) / "equity_continuity.log"
            target.write_bytes(raw)
            remember_file(str(target), raw)
            started = time.perf_counter()
            assert len(ContinuityChain(str(target)).records()) == size
            cold.append(time.perf_counter() - started)
            started = time.perf_counter()
            assert recovery_problem(str(target)) is None
            root.append(time.perf_counter() - started)
            chain = ContinuityChain(str(target))
            started = time.perf_counter()
            added = chain.append(KIND_EVIDENCE, {
                "settled_count": size + 1, "digest": "a" * 64,
                "strategy_equity": 100.0, "realized_pnl_cum": 0.0,
            }, "2026-09-10T00:00:01Z")
            append.append(time.perf_counter() - started)
            assert added["seq"] == size + 1
            assert len(ContinuityChain(str(target)).records()) == size + 1
            assert recovery_problem(str(target)) is None
    results.append({
        "records": size, "bytes": len(raw),
        "cold_validate_ms": round(statistics.median(cold) * 1000, 3),
        "append_ms": round(statistics.median(append) * 1000, 3),
        "root_hash_ms": round(statistics.median(root) * 1000, 3),
    })
print(json.dumps({
    "base_sha": "90e460f091438534f50e4963cf04383d7532779c",
    "repetitions": 3, "statistics": "median", "network_calls": 0,
    "broker_mutations": 0, "external_authority_configured": False,
    "results": results,
}, indent=2))
```
