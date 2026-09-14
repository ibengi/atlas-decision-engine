# Settlement evidence binding remediation review

Base: `6f0accc551e06debfa1cef99ff72ddc01c6ccfaa`.
Scope: Alpha settlement evidence integrity only. This change does not qualify
a real settlement authority, establish Astra identity, resolve accounting
uncertainty, change execution authority, or deploy a service.

## Root cause and before evidence

The former ingester checked a nonempty settlement evidence ID and an allowed
source label, then discarded supplied settlement response digest/preimage
fields. Replay therefore could not verify that the named evidence described
the outcome it admitted. Three independent invalid inputs—a different
well-formed evidence ID, altered response digest, and altered response
preimage—each produced one generic learning/calibration sample. Zero Astra
samples did not make that admission safe.

The original retained-capture witness reproduced one positive control,
eleven refusals and all three unsafe acceptances against an isolated archive
of the exact base. `tools/settlement_evidence_before_probe.py` permanently
reproduces the three unsafe cases using a complete synthetic source fixture
from that same baseline. It needs no retained Railway file or real outcome.
It pins affected baseline file hashes, uses a deterministic clock, writes only
temporary synthetic ledgers, and fails if its positive control or original
counterexamples do not reproduce.

From the candidate checkout, with dependencies already installed if required:

```sh
SEB_CANDIDATE_ROOT="$PWD"
SEB_BASE_ROOT="$(mktemp -d)"
git archive 6f0accc551e06debfa1cef99ff72ddc01c6ccfaa | tar -x -C "$SEB_BASE_ROOT"
python "$SEB_BASE_ROOT/tools/audit_isolation/run_isolated.py" \
  "$SEB_BASE_ROOT" /tmp/atlas-settlement-before.log \
  "$SEB_CANDIDATE_ROOT/tools/settlement_evidence_before_probe.py" \
  --repository "$SEB_BASE_ROOT" --output /tmp/atlas-settlement-before.json
```

The expected historical result is `original_defect_reproduced=true`: one
positive control and three unsafe variants accepted by the old code. This is
a reproduction receipt, never a passing safety verdict for the baseline.

## Complete retained evidence contract

`alpha_settlement_validation.settlement_qualification` is the shared admission
and replay gate. It independently verifies the prediction's canonical source
preimage, source/snapshot economic agreement, complete settlement binding,
and strict chronology, then calls the pure retained-response verifier in
`alpha_settlement_evidence.py`.

A new qualified resolution retains:

- The complete prediction/contract/snapshot/source-digest/environment/schema
  join in a versioned evidence object.
- Exact authority identity and a snapshot of the caller-installed authority
  policy; feed rows cannot supply their own policy override.
- Canonical JSON response text, its independently recomputed SHA-256, and an
  evidence ID content-addressed over the full evidence object and join.
- The authority's response record ID, exact supported outcome, and exact
  resolution timestamp in that response preimage.
- Optional original response bytes only when their canonical base64 decoding
  exactly matches the retained UTF-8 preimage.

No supplied response evidence is silently dropped. Unsupported fields,
noncanonical JSON, duplicate response members, malformed identities, invalid
digest encodings, contradictory aliases and nonboolean qualification flags
refuse qualification. Comparing two copies of a supplied digest is not the
digest check: the verifier hashes retained response bytes itself.

`AlphaLedger.resolve` freezes nested metadata and repeats qualification under
the writer lock before it may append a `binding_verified=true` row. Intake
also freezes caller data. Same-outcome receipts with different evidence are
conflicts, not outcome-only idempotent duplicates.

## Independent review findings and correction

Independent review found a second boundary in the same defect family: ordinary
JSON decoding in the settlement JSONL CLI and ledger replay discarded earlier
duplicate evidence ID/digest/preimage members. All six synthetic intake/replay
variants initially admitted one learning and calibration sample each.

Both boundaries now use `alpha_evidence_json.py`, rejecting duplicate members
at every object level, including equivalent escaped keys, and nonfinite
numbers. Invalid historical bytes remain untouched; malformed rows are
reported and excluded using the existing append-only reader policy. Permanent
JSON boundary regressions preserve the positive control and the new witnesses.

The same six independent witnesses now each produce zero qualified rows,
zero learning samples and zero calibration samples. Twenty-three additional
independent neighboring witnesses pass, including seventeen invalid intake
exclusions, two valid controls, mutable proof/policy aliases across reads, a
second coherent receipt conflict, and concurrent independent ingesters
leaving exactly one resolution. Before receipts remain retained separately
from corrected results.

## Replay and learning guarantees tested

Permanent regressions exercise valid intake, exact retained bytes, a new
Python process reopening the ledger, one qualified sample after report
reconstruction, and idempotent reingestion without a duplicate resolution.
They alter only disposable history copies and verify that evidence tampering
and legacy unbound resolutions are excluded from qualified learning and
calibration. Original synthetic history prefixes remain unchanged.

```sh
python tools/audit_isolation/run_isolated.py "$PWD" \
  /tmp/atlas-settlement-regressions.log -m pytest -q \
  tests/test_settlement_evidence_binding.py \
  tests/test_settlement_evidence_json_boundary.py
```

The isolated launcher denies external networking and provides dummy broker
environment values. No test installs a real provider/settlement authority.
The final remediation package records exact candidate SHA, complete canonical
suite and CI/Docker receipts, mutation classifications, and artifact hashes.
Diagnostic or setup failures are not behavioral mutation kills.

## Authority and operational limits

Cryptographic consistency is not remote authenticity. A content digest and a
stored policy hash cannot authenticate a new exchange statement by themselves.
The caller must independently qualify the read-only settlement channel and
install its authority policy; this remediation installs none. A future adapter
must preserve or independently verify the original authoritative response
before it constructs the versioned canonical response. Its actual outcome,
resolution timestamp, authority record and provenance require independent
qualification. Rewriting an entire coherent local evidence chain and trust
policy is outside a checksum's protection and is not claimed to be detected.

`LI-04=OPEN`, `SETTLEMENT_AUTHORITY=UNQUALIFIED`, `LI-05=OPEN`, and
`ASTRA_IDENTITY=UNPROVEN` remain operational evidence tasks. The unresolved
Gemini reservation `2ebc3cf99a9a479b8049ed969f757a6d` remains untouched and
`ACCOUNTING_UNCERTAINTY=OPEN`. All synthetic authority/response material is
explicitly test-only; no live-promotion recommendation follows from these
tests.
