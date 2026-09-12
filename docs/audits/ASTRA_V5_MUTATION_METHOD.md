# V5 mutation evidence contract

The mutation runner works only in disposable repository copies. An unmodified
positive baseline and the mutated run execute the same selectors. All cases
use synthetic data; the audit launcher denies external socket operations.

A failing pytest summary does not prove that a safety invariant was tested.
`tools/astra_mutation_pytest.py` records collection, call, setup and teardown
phases, exception types, and the actual traceback statement. It recognizes
unittest setup and teardown frames even though pytest labels them `call`.
`SEMANTIC_WITNESSES` in `tools/astra_mutation_probe.py` separately names the
reviewed test selector, semantic operation and violated invariant. A matching
traceback establishes a kill; a matching diagnostic message alone does not.
All phase receipts and the selected semantic witnesses appear in JSON output.

| Classification | Evidence |
| --- | --- |
| `KILLED_BEHAVIORALLY` | Healthy unmodified baseline, actual failed test-body semantic witness, no setup or teardown failure. |
| `DIAGNOSTIC_ONLY` | Assertions fail without an approved semantic witness, or a declared diagnostic mutation preserves its tested refusal semantics. This does not count as a kill. |
| `INCONCLUSIVE_SETUP` | Any fixture, unittest setup/teardown, or cleanup failure. Mixed semantic and fixture failures remain inconclusive. |
| `INCONCLUSIVE_COLLECTION` | Collection failed or no tests were collected. |
| `INCONCLUSIVE_IMPORT` | Collection failed because of an import or syntax error. |
| `INCONCLUSIVE_INFRASTRUCTURE` | Missing/malformed phase receipt, unhealthy baseline, timeout, inconsistent exit evidence, or unclassified runtime failure. |
| `SURVIVED` | The applied mutation's tests executed and passed. |
| `NOT_APPLIED` | The mutation anchor was absent or ambiguous. |

M06 and M17 deliberately remove redundant diagnostic refusal branches. Other
validation still rejects the legacy schema or unqualified authority. Their
consumer/ingestion semantic controls are retained, and they are reported as
diagnostic when only wording or counters change. An effective mutation whose
only failures are diagnostic cannot pass the evidence gate.

`surviving_effective_safety_mutations` counts applied effective survivors.
`unresolved_effective_safety_mutations` also includes effective mutations with
only diagnostic or inconclusive results. CI requires both counts to be zero,
plus zero unapplied or inconclusive experiments. It does not require an
ineffective diagnostic mutation to acquire a fabricated behavioral kill.

The original M01–M40 and M07P controls remain. Ports explicitly target the
current invariant when v5 moved its enforcement into a shared validator. M26P
retains directory-barrier deletion. Additional v5 controls challenge source
extensions, transient missing metadata, chronology, observer contention,
economic source/snapshot equality, classification itself and the transaction
boundary between paid usage and prediction publication.

Run the committed isolated test launcher with
`tools/astra_mutation_probe.py --json` to produce a fresh evidence package.

The exact original **M01** is also retained. Its late substituted source no
longer defeats the independent retained-preimage guard, so its original
zero-mint/zero-prediction witness and complete-observation positive control both
pass. The runner labels that experiment `SURVIVED`, records the two executed
control node IDs and explanation, and excludes it only from the **effective**
survivor count. A missing/skipped control makes the result inconclusive. The
separate **M01P** variant invents the default authority before source and
provenance capture; it must produce an actual behavioral kill. The final table
therefore distinguishes the ineffective original survivor from the effective
strengthened mutation.

For complete per-experiment stdout, stderr and phase receipts, pass
`--evidence-dir /absolute/disposable/evidence-directory` alongside `--json`.
