# Atlas AI Engineering Factory v1

## Purpose

This control plane coordinates three independent AI roles around Atlas Decision Engine while preserving evidence, audit independence, and human control over production capital.

## Roles

### GPT — Lead Orchestrator / Release Gate
GPT owns decomposition, invariant definition, risk classification, evidence review, arbitration, and release eligibility. GPT should not normally implement the primary remediation it later judges.

### Claude — Senior Remediation Engineer
Claude owns defect reproduction, root-cause analysis, invariant-level remediation, regression tests, and candidate commits. Claude may provide evidence but may not declare its own work accepted or production-ready.

### Gemini — Independent Adversarial Auditor
Gemini receives the pinned base SHA, candidate SHA, finding, invariant, and acceptance criteria. It independently attempts to falsify the claimed invariant and returns PASS, FAIL, or INCONCLUSIVE. It must not modify the candidate while auditing it.

## Non-negotiable rules

1. Every code decision is pinned to an exact commit SHA.
2. Every defect is reproduced before remediation when reproducible evidence exists.
3. A regression test must fail on the vulnerable/base revision and pass on the candidate revision.
4. The implementer never issues the final acceptance verdict.
5. PASS requires independent evidence; existing-suite green alone is insufficient.
6. INCONCLUSIVE never silently becomes PASS.
7. Safety guards may not be weakened merely to make tests pass.
8. Ambiguous execution state must fail closed.
9. No AI may independently perform merge + deploy + enable live orders as one action.
10. Human authorization remains required before production capital is enabled or materially increased.

## Risk levels

- L0: documentation/comments only.
- L1: non-critical implementation change.
- L2: state, persistence, auth, integrity, migrations, reconciliation.
- L3: money, orders, execution, positions, equity, risk limits, credentials, capital allocation, live trading.

L2 requires implementer + independent auditor. L3 requires all three roles plus staged runtime evidence.

## Per-finding lifecycle

ORCHESTRATOR -> REPRODUCTION -> REMEDIATION -> REGRESSION -> ADVERSARIAL AUDIT -> RELEASE GATE

FAIL returns to remediation. INCONCLUSIVE returns to evidence collection. PASS proceeds only to the next deployment stage, not automatically to live capital.

## Deployment ladder for L3

CODE ELIGIBLE -> SHADOW -> DEMO -> CANARY -> LIMITED LIVE -> CONTROLLED SCALE

Each stage must have its own explicit evidence and rollback/kill-switch criteria.

## Evidence package

Every finding must record:

- finding_id
- severity
- risk_level
- invariant
- base_sha
- candidate_sha
- reproduction command/test
- old result
- files changed
- regression tests
- relevant suite results
- adversarial tests
- auditor verdict
- remaining uncertainty
- orchestrator decision

## Forbidden shortcuts

- No acceptance based only on developer explanation.
- No audit against an unpinned moving branch.
- No undocumented force-push during audit.
- No changing the candidate after audit without invalidating the verdict.
- No production enablement because a code audit passed.
- No release record that conflates code audit evidence with deployment evidence.
