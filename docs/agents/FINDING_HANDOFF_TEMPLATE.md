# Atlas Finding Handoff Template

Use one document per finding. Do not combine unrelated invariants.

## Identity

FINDING_ID:
TITLE:
SEVERITY:
RISK_LEVEL: L0 / L1 / L2 / L3
STATUS: OPEN / IMPLEMENTING / AUDIT / FAILED / INCONCLUSIVE / ELIGIBLE

## Scope

INVARIANT:
BASE_SHA:
AFFECTED_COMPONENTS:
OUT_OF_SCOPE:

## Reproduction

REPRODUCTION_COMMAND:
EXPECTED_BASE_RESULT:
ACTUAL_BASE_RESULT:
REPRODUCTION_EVIDENCE:

## Implementation handoff — Claude

ROOT_CAUSE:
DESIGN:
FILES_CHANGED:
TESTS_ADDED:
DIRECT_TEST_RESULTS:
RELEVANT_SUITE_RESULTS:
EDGE_CASES_CHECKED:
REMAINING_UNCERTAINTY:
CANDIDATE_SHA:

## Independent audit handoff — Gemini

AUDITED_SHA:
INDEPENDENT_TESTS:
COUNTEREXAMPLES:
REGRESSION_RESULTS:
NEW_RISKS:
AUDITOR_VERDICT: PASS / FAIL / INCONCLUSIVE
AUDITOR_LIMITATIONS:

## Release gate — GPT

SHA_IDENTITY_CONFIRMED: YES / NO
IMPLEMENTER_AND_AUDITOR_INDEPENDENT: YES / NO
REQUIRED_EVIDENCE_COMPLETE: YES / NO
CI_STATUS:
UNRESOLVED_COUNTEREXAMPLES:
UNRESOLVED_UNCERTAINTY:
DECISION: ELIGIBLE_FOR_NEXT_STAGE / REMEDIATION_REQUIRED / EVIDENCE_INCOMPLETE
NEXT_STAGE:

## Integrity rules

- If CANDIDATE_SHA changes after audit begins, invalidate the audit and start a new audit on the new SHA.
- Never edit evidence to hide an earlier failure; append corrected evidence with provenance.
- PASS is scoped to the stated invariant and tested evidence, not an absolute correctness claim for the whole repository.
