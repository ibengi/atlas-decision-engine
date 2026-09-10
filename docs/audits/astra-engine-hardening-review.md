# Atlas engine remediation handoff

Rejected base: `3af848e6aebc8769ea59f878d68674f89e04af0d`.
Branch: `astra/engine-hardening-a01-a20`, created directly from that base.

The candidate is for Claude's independent counter-review. CAPITAL remains OFF;
real broker writes and broker network mutations during this work are zero. No
deployment, production configuration change, risk-threshold relaxation or merge
is part of this remediation. Synthetic transport callbacks are exercised in memory.

## Reading the verdicts

PASS means the stated conservative invariant is enforced by the inspected code
and exercised by the local regression battery. It does not certify production
operation or an external service that does not exist. FAIL means a reproduced
violation. NEEDS_REVIEW identifies an unproved integration or an unavailable
validation environment. A restart that preserves an explicit recovery block is
consistent; automatic return to eligibility is not required or implied.

The exact committed SHA, tree SHA, execution counts and output hashes are in the
final validation receipt delivered with this handoff. The counts below describe
the retained and added test inventory; the branch is published only after that
exact commit has completed the battery. Earlier dirty-worktree reports are not
used as evidence of tests on the final commit.

## Shared architectural correction

The common defect was fragmented authority: generation checks, file writes,
continuity updates and mutable manager publication had separate boundaries.
`state_authority.py` now defines one root lock, monotonic manifest, pending
transaction marker, durable write/readback protocol and commit point. The engine
also owns a process-lifetime writer lease. Component transaction modules prepare
private state and publish only after the root transaction commits. Ledger, journal,
positions/fill IDs, risk claims and order adoption use this protocol.

The complete per-operation read sets, write sets, preconditions, locks, prepared
state, commit/publication points and crash/restart semantics are documented in
[`engine-transaction-model.md`](../design/engine-transaction-model.md).

## A01–A20 disposition

The regression column includes the original scenarios and the additional
interaction tests. Relevant groups are named below to make independent reruns
possible. The limitations column uses PASS for no remaining limitation identified
in that finding's tested local contract; it is not an exhaustive absence proof.

| Finding | Safety invariant | Regression | Fail closed | Restart consistency | Engineering limitation / evidence |
|---|---|---|---|---|---|
| A01 continuity / rollback | PASS | PASS | PASS | PASS | NEEDS_REVIEW M1/M2. Full-volume proof requires independent authority; absent proof blocks. Root marker/hash/generation, full-chain checks, restore and actual crash cases |
| A02 position completeness | PASS | PASS | PASS | PASS | PASS. Duplicate/conflicting ticker, ID, side, quantity, page or envelope cannot produce MATCH; 28 historical parser and 14 new conflict cases |
| A03 stale rebase | PASS | PASS | PASS | PASS | NEEDS_REVIEW M1. Rebase requires a verifiable freeze, local read-set fences and final validation; no production broker-freeze provider exists |
| A04 economic event identity | PASS | PASS | PASS | PASS | PASS. Immutable broker order identity, retained fill IDs, idempotent settlement and linked canonical corrections; replay cannot add positive evidence |
| A05 accounting mode | PASS | PASS | PASS | PASS | PASS. Unknown modes remain blocked through load, observation and restart |
| A06 unexplained movements | PASS | PASS | PASS | PASS | PASS. Adverse busy observations persist; rebounds do not erase them; fixed cumulative rounding budget; pending observations block |
| A07 migration atomicity | PASS | PASS | PASS | PASS | NEEDS_REVIEW M2 for general legacy/history reconstruction. Exact proposal/source fences and private preparation; late journal change refuses without publishing seed |
| A08 gatekeeper evidence | PASS | PASS | PASS | PASS | PASS. Strict JSON/numerics, complete reviewed criteria and consistent counts; malformed input yields REJECTED. Stateless reread on restart |
| A09 intent before transport | PASS | PASS | PASS | PASS | NEEDS_REVIEW M3. Complete identity-bound intent, payload digest and durable readback under root exclusion; unknown outcome remains pending |
| A10 transactional rebase publication | PASS | PASS | PASS | PASS | PASS. Prepared ledger/continuity remain private; old published HWM survives failure; partial durable work requires recovery |
| A11 flow idempotency | PASS | PASS | PASS | PASS | PASS. Stable observation/economic evidence identity; two equal withdrawals remain distinct and repeated evidence remains one event |
| A12 read-only tools | PASS | PASS | PASS | PASS | PASS. Byte-level snapshots across diagnostic commands and failure states; no lock creation, repair, rotation, continuity append or transport mutation |
| A13 concurrent writers | PASS | PASS | PASS | PASS | NEEDS_REVIEW M4 for filesystem qualification. OS lock encloses generation validation and commit; second engine lease is refused; real process races have one winner |
| A14 premature publication | PASS | PASS | PASS | PASS | PASS. Private candidate objects; observer thread/callback sees old authority until commit completes |
| A15 operator transactions | PASS | PASS | PASS | PASS | PASS. Stale/failed hold release, attestation, rebase and migration cannot publish authority; ambiguous token consumption stays blocked |
| A16 finite numerics | PASS | PASS | PASS | PASS | PASS. Central strict parser/number validator at reviewed configuration, broker and persistence boundaries; NaN, infinity, bool quantities and invalid ranges refused |
| A17 intent recovery | PASS | PASS | PASS | PASS | NEEDS_REVIEW M2/M3. Missing expected, malformed or unknown intent is recovery-required, never proven empty; complete broker adoption and intent closure commit together |
| A18 durability | PASS | PASS | PASS | PASS | NEEDS_REVIEW M4. Write-all handles short writes/EINTR; fsync, rename, directory fsync, readback and storage faults cannot return durable success |
| A19 stale reader | PASS | PASS | PASS | PASS | PASS. Capital-relevant reads check current durable generation, fingerprints, root health, identity and independent checkpoint; stale instances block and reload |
| A20 account / environment | PASS | PASS | PASS | PASS | PASS. Canonical non-secret broker/environment/account tuple plus SHA-256; demo/prod and account mismatch block; same-account credential rotation preserves identity |

The historical harnesses retain the identifiers for each A01–A12 scenario and
the A13–A20 controls (`N13` through `N20`). The 96 new repository cases are in
`tests/test_engine_authority.py` and `tests/test_authority_cross_component.py`.

## Additional reliability findings addressed

Severity describes the unsafe behavior, before its correction. Four entries have
an additional identical-script comparison against the rejected base. The other
entries are failures/risks found while reviewing the interacting components and
are covered by focused synthetic regressions in the remediation tree. They are
not presented as nine independently reproduced baseline scripts.

| ID | Severity | Result | Reliability defect and correction | Evidence |
|---|---|---|---|---|
| A21 | CRITICAL | PASS | Incomplete/aged settlement or missing original trade could release exposure using invented economic completion. Retain full exposure and UNKNOWN until complete, attributable evidence exists | `incomplete_settlement` before/after; old incomplete outcome and missing-original-trade tests |
| A22 | HIGH | PASS | Unknown journal schema could be replaced by an empty journal. Preserve raw history, flag integrity failure and block authoritative use | `unknown_journal` identical-script before/after; journal malformed-state regressions |
| A23 | HIGH | PASS | Subcent settlement loss was rounded to zero. Preserve settlement PnL to six decimal places and keep adverse movement accounting | `subcent_settlement` before/after: -0.0001 is retained; fixed-noise-budget test |
| A24 | CRITICAL | PASS | Journal settlement, exposure removal and replay IDs had separate commit boundaries; order adoption could lose the pending intention. Commit each cross-component change together; never truncate fill replay IDs | Settlement write failure, silent flush, adoption failure, missing order identity, restart with retained fill-ID history |
| A25 | HIGH | PASS | Multiple risk managers could consume the same half-open attempt, and date rollover could erase a claim. Fence the risk transaction and preserve the claim until new settlement evidence | `concurrent_risk_claims` before/after; failed release and date rollover regressions |
| A26 | HIGH | PASS | Old, unrelated or repeated corrections could be treated as evidence for a later equal adverse residual. Require new unused negative correction, linked original trade and matching amount | Unrelated/existing correction classification tests plus historical correction/replay cases |
| A27 | HIGH | PASS | A final external-authority callback could alter a read-set file after initial validation. Revalidate manifest and every authoritative file after callbacks, before removing the pending marker | External commit callback read-evidence mutation test |
| A28 | HIGH | PASS | Forked children could inherit lock bookkeeping or manager authority. Reset child lock state/descriptors and reject inherited lease/ledger ownership | Actual `os.fork` owner test; independent process generation race |
| A29 | HIGH | PASS | Generic mutation retries/ambiguous replies lacked conservative durable outcome handling; authentication kwargs could enter the stored request. Disable mutation retry, retain unresolved intent, block subsequent mutation and reject authentication kwargs before persistence | Actual decorated transport with a synthetic callback: retry disabled, pending outcome retained; authentication-kwargs storage refusal |

## Before/after evidence and test accounting

Baseline files are retained under `docs/audits/astra-hardening-evidence/`.
The original baseline repository suite passed 1177 tests despite the reproduced
defects. The 108 historical Astra cases yielded 97 PASS, 9 FAIL and 2 errors
(classified NEEDS_REVIEW, never counted as safety passes). The 192 additional
controls yielded 141 PASS, 46 FAIL and 5 NEEDS_REVIEW. Four new identical-script
comparisons each failed on the rejected base. This is **59 confirmed unsafe
cases**, plus 7 unresolved baseline executions; errors are not counted as
confirmed unsafe behavior.

| Battery | Cases after remediation | Relationship |
|---|---:|---|
| Original repository inventory | 1177 | All retained; stronger fixtures/assertions explained below |
| New repository interactions | 96 | 66 authority + 30 cross-component cases |
| Historical Astra | 108 | All original scenarios retained |
| Previous additional reliability cases | 190 | All retained |
| Previous global controls | 2 | NaN balance and failed stale hold release through global gates |
| New same-script before/after controls | 4 | Identical code run against both versions |
| Total | 1577 | 1273 repository + 304 standalone controls |

The 96 new repository cases and 4 additional comparisons make **100 new cases**.
Three controls use actual process concurrency/fork for authority; eight use actual
process termination at persistence boundaries. These are subsets of the total,
not additional cases. Eight new explicit filesystem failure injections complement
26 persistence-tagged historical reliability cases (tags overlap crash/restart).
The short-write/EINTR test verifies the exact resulting bytes.

| Identical-script control | Rejected base | Corrected behavior |
|---|---|---|
| Incomplete settlement | Exposure released: 0 | Full synthetic exposure retained: 3 |
| Unknown journal | Raw history not preserved | Raw history preserved and blocked |
| Subcent settlement | Loss persisted as -0.0 | Loss persisted as -0.0001 |
| Concurrent risk claims | Both claims accepted | Exactly the first claim accepted |

## Test integrity and isolation

The first independent 108-case harness and its earlier completeness-contract
adaptation are preserved verbatim alongside the current harness. Current fixture
changes declare non-secret account identity, an independent synthetic checkpoint,
a controlled broker freeze and explicit durable empty collections. Other changes
respect detached/private returned objects, set historical timestamps at ingestion,
and explicitly complete verified recovery. Assertions still require the original
economic safety properties; an exception or a stale fixture is not a PASS.

Some old repository expectations encoded unsafe behavior and were strengthened:
automatic older-backup healing, aged orphan release, silent failed writes and
erasing persisted intentions now require a block, retained exposure or unchanged
published state. Completed rows cannot be rewritten through the legitimate save
API; filesystem corruption tests write fault bytes directly in temporary roots.
No negative cases were removed, no test-name branches were added to production
code, and no production promotion/submission setting was enabled.

The portable launcher in `tools/astra_regressions/run_isolated.py` starts a clean
environment with dummy credentials and fresh temporary DATA_DIR. Its inherited
Python audit hook blocks external DNS, connect and sendto; only the original
dashboard tests may use loopback. Network namespaces were unavailable, so this is
application/runtime isolation, not an OS network-sandbox claim. Synthetic adapters
and callback spies exercise the transport boundary without contacting a broker.
No dependency download or real credential use is needed for the recorded runs.

The existing gatekeeper returns **REJECTED** with default promotion blocks and
the unchanged, unapproved `model_validation.json`. That is the required closed
result, not an authorization to trade. AST parsing, in-memory compilation and
`git diff --check` are the static checks. Docker is unavailable in this runtime;
no container build is claimed.

## Remaining engineering limits

| ID | Disposition | Scope and effect |
|---|---|---|
| M1 | NEEDS_REVIEW | No deployed independent continuity provider or verifiable broker-freeze provider. The interfaces and local validation exist; absent/currently unproved authority blocks CAPITAL and rebase. These are critical production enablement prerequisites, not fabricated infrastructure |
| M2 | NEEDS_REVIEW | Recovery completion accepts only an exact, complete, independently CURRENT checkpoint and appends a receipt. General broker-history reconstruction, incomplete legacy-state migration and cross-account reconciliation remain unavailable; affected state stays blocked |
| M3 | NEEDS_REVIEW | Generic transport-outcome reconciliation is not implemented. An unresolved mutation intent blocks every further mutation even after an HTTP acknowledgement. This intentionally sacrifices execution availability until independently reviewed resolution is supplied |
| M4 | NEEDS_REVIEW | POSIX flock/rename/fsync semantics need qualification on the deployment filesystem; Docker could not be built here. Advisory locking does not constrain an arbitrary privileged filesystem editor |

No known reproduced CRITICAL or HIGH unsafe path remains in the completed local
battery. M1–M4 remain explicit operational/validation gaps; passing synthetic tests
does not resolve them. Neither this report nor the implementer grants CAPITAL
approval. The only release disposition is readiness for independent Claude
counter-review, with CAPITAL OFF.
