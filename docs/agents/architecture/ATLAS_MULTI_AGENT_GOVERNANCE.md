# Atlas Multi-Agent Governance v2

Status: ACTIVE GOVERNANCE

## Purpose

This document defines the permanent engineering governance for Atlas Decision Engine. It separates implementation, independent verification, orchestration, evidence retention, and release gating so that no single agent can implement a change and self-authorize it.

## Roles

### GPT / Astra — Orchestrator and final arbiter

Responsibilities:
- define jobs, scope, invariants, accepted parent SHA and risk level;
- decompose work into independently verifiable batches;
- prepare Claude remediation handoffs;
- prepare Gemini/Antigravity independent audit handoffs;
- compare evidence from implementation and independent audit;
- return ACCEPT / REJECT / INCONCLUSIVE for each batch;
- maintain the accepted SHA chain and release-gate record.

Astra does not self-validate code it implemented.

### Claude — Remediation engineer

Responsibilities:
- reproduce each defect on the accepted parent before remediation when practical;
- state the violated invariant and root cause;
- correct the invariant, not only the supplied witness;
- add regression and adjacent counterexample coverage;
- preserve previously accepted controls;
- produce one candidate SHA and evidence report.

Claude must not declare its own remediation PASS.

### Gemini / Antigravity — Independent adversarial auditor

Responsibilities:
- audit a pinned candidate SHA in a separate working tree;
- treat implementer explanations as untrusted claims;
- reproduce original witnesses where practical;
- create independent witnesses not copied from implementer tests;
- probe cross-finding, restart, retry, malformed-state, concurrency and persistence interactions;
- return PASS / FAIL / INCONCLUSIVE.

Gemini does not repair the candidate it audits.

### GitHub — Source of truth

GitHub records:
- accepted parent SHA;
- candidate SHA;
- branch lineage;
- CI evidence;
- job specifications;
- independent audit evidence;
- release-gate decisions.

## Risk levels

| Level | Typical scope | Required gate |
|---|---|---|
| L0 | documentation/comments | implementation + CI |
| L1 | non-critical code | implementation + regression tests |
| L2 | persistence, authentication, state, data integrity | implementation + independent audit + Astra gate |
| L3 | orders, execution, risk, equity, credentials, capital | L2 plus SHADOW -> DEMO -> CANARY -> LIMITED LIVE validation |

No L3 change may move directly from code completion to live capital authority.

## Required job fields

Every engineering job must define:
- JOB_ID
- RISK_LEVEL
- ACCEPTED_PARENT_SHA
- TARGET_BRANCH
- FINDINGS / OBJECTIVE
- INVARIANTS
- KNOWN_WITNESSES
- ALLOWED_SCOPE
- FORBIDDEN_ACTIONS
- CLAUDE_ACCEPTANCE_CRITERIA
- GEMINI_AUDIT_CRITERIA
- REQUIRED_EVIDENCE
- FINAL_GATE

## Evidence rule

A finding is not closed solely because tests are green. Minimum closure evidence is:

accepted parent SHA
+ reproduction before fix
+ violated invariant
+ root cause
+ regression witness
+ candidate SHA
+ post-fix validation
+ independent audit
+ explicit verdict

## Verdict semantics

PASS: no reproducible counterexample remains within the tested invariant and required evidence is sufficient.

FAIL: at least one reproducible counterexample remains.

INCONCLUSIVE: evidence or environment is insufficient to establish the property reliably.

INCONCLUSIVE must never be promoted to PASS.

## Batch release gate

A batch may become the next accepted parent only when all are true:
- zero FAIL;
- zero unresolved INCONCLUSIVE;
- candidate SHA exactly pinned;
- working tree clean;
- required regression suites green;
- no safety guard weakened;
- independent audit completed;
- Astra arbitration recorded.

## Operational release ladder

Code acceptance does not authorize live capital.

CODE GATE
-> SHADOW
-> DEMO
-> CANARY
-> LIMITED LIVE
-> CONTROLLED SCALE

Each transition requires its own evidence and GO/NO-GO decision.

## Permanent authority boundaries

- Claude may implement and test; it does not authorize release.
- Gemini may inspect and test; it does not modify or release the audited candidate.
- Astra may arbitrate; it does not automatically enable live capital.
- The local Control Plane may orchestrate branches/tests; it must not merge main, deploy Railway, change credentials, enable broker authority, or enable CAPITAL.

## External evidence separation

Code correctness and operational evidence are separate. Real Railway restart durability, authenticated live exchange schema, real settlement authority, physical power-loss behavior and distributed-filesystem assumptions must be tracked separately from code-level findings.
