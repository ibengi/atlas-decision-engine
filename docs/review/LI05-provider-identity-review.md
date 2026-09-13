# LI05 provider identity: separate Alpha review candidate

Status: **review-only; undeployed; Astra identity remains UNPROVEN.**

Base: `2dead89fde8098b06efa07976ffb56c0c8f8c460`.
Local branch: `alpha/astra-provider-identity-review`.
No source in the running audited Alpha service or production money service
was changed by this patch. No provider request, broker write, credential
operation, CAPITAL change, deployment, merge or historical ledger rewrite
was performed while developing or testing it.

## Problem and resulting behavior

The accepted Alpha release retained generated model labels but dropped
response-level model and response identities. Renaming the configured model,
or an answer naming itself Astra, could therefore determine attribution.
Repairing account quota alone would not create auditable automated identity.

The patch captures an immutable `atlas-provider-identity-receipt-v1` from
the actual adapter response, independently replays its retained response and
request preimages, and carries it through dispatch, signal, ensemble, cost
row and committed prediction. Generated text cannot choose model attribution.
The qualified model key is `provider/resolved_model`, so different providers
cannot overwrite one another through the same model string. Duplicate generic
model keys make the ensemble refuse a probability.

## Explicit trust contract

This receipt is a **controlled HTTPS transport observation, not a remote
provider signature**. A local SHA-256 detects inconsistent bytes; it cannot
authenticate arbitrary ledger bytes against an external authority. Replay
depends on the trusted, reviewed capture adapter and protected append-only
history. The code does not claim stronger cryptographic provenance.

The runtime boundary admits identity evidence only from the exact built-in
OpenAI/Grok Responses API adapters, with their original capture/parser methods,
no injected session, and the capability created for that dispatcher invocation.
An ordinary custom provider, subclass, replaced method or copied metadata dict
cannot self-authenticate a receipt. Calls use explicit TLS verification, no
redirect following and no environment proxy inheritance. URL credentials,
query authentication and non-HTTPS endpoints are refused before a request.
Final response origin/path must exactly match an approved endpoint for proof.

Receipts retain the provider; requested and response-resolved model; response
ID; allowlisted transport request ID; locally generated request identity;
request/receipt timestamps; HTTP status and transport policy; exact snapshot,
contract and environment binding; canonical non-secret request body and digest;
exact response bytes and digest; output-text digest; and versioned trust policy.
Headers are never dumped. Known credential echoes, named secret fields and
bounded nested JSON/Unicode-escape variants are refused before retention.

`REVIEWED_MODEL_MAPPINGS` is empty. No current model, label, alias or endpoint
configuration automatically gains the Astra role. A future authority decision
must review the exact provider, endpoint, requested alias, resolved model,
role and policy version. Unknown mappings remain unqualified.

Unsupported endpoints/providers, including Gemini's different response schema,
remain valid generic SHADOW research when their forecast passes the existing
validator. Their captures are explicitly unqualified adapter diagnostics and
are not advertised or persisted as qualified identity receipts. Supporting
their durable identity proof needs a separate reviewed adapter contract.

## Persistence and learning

The existing PREPARE/PREDICTION/COMMIT protocol and actual synchronization
barriers are unchanged. No extra durability receipt was introduced. The
prediction contains a binding from its ID to the provider receipt digest.
The ledger verifies that binding and the forecast projection under its existing
writer lock. Reusing a response ID or request identity within a batch or across
predictions is refused, including when the earlier row is readable but its
durability is still uncertain. Cost rows preserve successful capture evidence
before prediction commit where the existing gateway records those costs.

Restart qualification replays the exact snapshot, environment, request body,
response bytes, model, probabilities, interval, confidence and provider. It
does not trust a stored qualification boolean. A changed prediction binding,
forecast field, source snapshot or environment fails qualification. New
receipt-backed predictions preserve timestamp microseconds and must follow
receipt capture; future or backdated prediction binding is refused. Legacy
prediction timestamps and bytes remain unchanged. This pure
semantic replay is not a synchronization barrier; the existing ledger barrier
still decides whether a prediction is durably committed.

Historical rows are not rewritten. A generated historical label cannot enter
the reserved provider/model calibration namespace without qualified replay.
Every Astra-labelled report path filters by qualified Astra role independently
of its configurable selector, preventing aliases or substring settings from
restoring label-only attribution. Generic historical research queries remain
available and make no Astra qualification claim.

## Regression evidence and review scope

The original seven counterexamples are retained unchanged in
`li05_original_identity_counterexamples.py`; they pass on the original base
when the documented limitation is reproduced. Their fixed invariants and new
neighbors are asserted in `tests/test_alpha_provider_identity.py`.

New cases include malformed receipts and outputs; duplicate JSON keys;
cross-snapshot/environment/request reuse; two providers with the same model
name; forged custom/subclass metadata; renamed report selectors; Unicode
secret echoes in nested forecast JSON; strict boolean-versus-numeric fields;
immutable restart reconstruction; and six consecutive failed fsync barriers.
A positive test joins real repository settlement qualification with synthetic
reviewed provider identity, ensuring the stricter rule is not vacuous.

Existing arithmetic test fixtures now include explicit synthetic transport
receipts under a scoped synthetic policy. The old settlement-only positive
test still proves generic calibration; its Astra count now correctly remains
zero without provider identity. No production allowlist was added for tests.

`tools/li05_identity_mutations.py` runs ten disposable semantic mutations and
records per-case JUnit evidence. Collection, setup, teardown, import or
infrastructure failures never count as behavioral kills. The original audited
mutation classifier is unchanged. M38's selecting tests now include a joint
qualified-provider/unqualified-settlement case: the first rerun exposed that
the stricter identity gate masked its old settlement-only witness. The initial
effective survivor result is retained, and the strengthened semantic witness
must kill M38 before this package is treated as verified.

Runtime source changes are limited to `alpha_identity.py`,
`alpha_providers.py`, `alpha_schema.py`, `alpha_dispatcher.py`, `alpha_meta.py`,
`alpha_gateway.py`, `alpha_ledger.py` and `alpha_learning.py`. No broker,
execution, order, risk, production bootstrap, config, workflow, source
normalization or settlement qualification implementation was edited.

## Remaining operational decisions

This patch does not fund or provision an API account, choose an Astra authority,
alter credentials, or demonstrate a genuine provider response. It must receive
separate review before Alpha integration. The operator must establish access
and approve the intended actual provider/model mapping. After a separately
reviewed integration, a genuine response for a genuine snapshot must be
committed and reproduced after an Alpha-only restart before
`ASTRA_IDENTITY=PROVEN` can be considered.

## Final local validation

- Canonical repository suite: **2,220 passed**, zero failures, errors or skips
  (2,185 original tests plus 35 new identity regressions).
- Independent synthetic counter-review: **45 passed**, including five strict
  prediction chronology refusals and the same-second microsecond positive.
- Historical mutation suite: **53 cases**; 50 behavioral kills, two diagnostic
  results, and one independently demonstrated ineffective survivor (M01,
  whose stronger M01P variant is killed). Effective survivors: **zero**.
- New identity mutations: **10 behavioral kills**, no diagnostic, setup,
  collection, import or infrastructure failures counted as kills.
- Runtime code identity:
  `63ef8f271942a014ef505d37808ed87cb78af1c5ddad8ed11ef381dbcfc1dc89`.

The initial historical M38 survivor and all intermediate failed experiments
remain in the evidence package. The final full historical rerun includes its
new joint-gate semantic witness. No hosted CI or Docker build is claimed for
this local review commit; those must bind any subsequent integration candidate.
