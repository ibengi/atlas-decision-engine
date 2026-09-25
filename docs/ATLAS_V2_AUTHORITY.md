# Atlas V2 authority and frozen V1

Status: V1 PARKED_READ_ONLY; V2 RESEARCH / READ_ONLY. No financial mutation authority.

## Repository authority

The only V2 implementation branch is `atlas-v2/rebuild`. Astra is the sole writer;
helpers may inspect and reproduce, and an independent reviewer may audit a fixed
candidate. No automated research output, diagnosis, model or CI result grants
merge, deployment or financial authority.

V1 is frozen at `bd810b4f3177108032f85e21a55729b6eb4b5a53`, tree
`713103c40424ee8f6a4cb827abd5704f1314df9f`, reference
`atlas-v1-final-readonly`. This is a named forensic reference, not a claim that
GitHub branch protection makes it immutable. Preserve its commit and evidence
hashes independently. Never merge older main or stacked branches wholesale.

Production's source configuration has been corrected from main to the freeze
reference. A pre-deploy SHA interlock rejects an absent or different source SHA.
The complete private baseline audit retains native configuration receipts,
state hashes, deployment identities and the service/cost inventory. Do not commit
account exports, credentials, provider bills or personal records here.

## Intake of open branches

| PR | V2 admission decision |
|---|---|
| 76 | KEEP immutable READ_ONLY restriction; reimplement observation separation. |
| 77 | KEEP strict position completeness and candle validation invariants. |
| 78 | REIMPLEMENT exact fractional quantities; no implied historical account proof. |
| 75 | REIMPLEMENT independently verified source/settlement binding. |
| 74, 70 | LEGACY optional Alpha/provider integration and packaging. |
| 73, 72, 64 | LEGACY superseded competing remediation candidates. |
| 71 | REIMPLEMENT bounded GET-only collection with immutable provenance. |
| 69 | REJECT competing implementation-authority model. |
| 68 | REIMPLEMENT reviewed transaction/fencing primitives individually. |
| 67 | LEGACY optional LLM research; excluded from decision runtime. |
| 66 | REIMPLEMENT attributed equity; reject inferred cashflow causes or seed capital. |
| 59 | REJECT settlement authority inferred from age/repeated observations. |
| 39, 31 | LEGACY retrospective replay/mock tools; no future OOS authority. |

These are admission decisions, not claims to have independently certified every
line of every old branch. PRs 76 -> 77 -> 78 remain separate, unmerged history.

## Active V2 boundary

DATA -> RESEARCH -> ALPHA CANDIDATES -> VALIDATION -> APPROVAL -> EXECUTION -> RISK.
The logical layers need not become separate always-on services. Start with one
small GET-only data/shadow process and offline research. No mandatory LLM calls.
No legacy module import, live broker adapter or default predictive strategy is
allowed in the V2 runtime. V1's failed alpha is LEGACY_BASELINE only; its consumed
TEST data must never become pristine FUTURE_OOS.

Unknown account scope, incomplete pagination, missing/invalid settlement authority,
manual positions or incomplete economic bridges remain unresolved and blocking.
No automatic position adoption, fabricated payout, historical ledger rewrite,
inferred deposit/withdrawal or cash-based reset of historical PnL is permitted.

The live drawdown ceiling remains 20%. No approval, risk limit or provider identity
can be manufactured by a configuration flag. V2 exposes no financial mutations.

## First implementation workstreams

1. `docs: freeze V1 authority and record V2 salvage boundaries` — this document;
   SHA recorded in the private audit after commit creation.
2. `feat(v2): immutable observations and prospective validation firewall` — raw
   receipts, append-only durable events, explicit schema/scope/completeness,
   hypothesis registration, immutable candidate lock and paired future metrics.
3. `fix(v2): recheck refreshed economics and preserve economic attribution` —
   refreshed quote gates, durable unique shadow intents, unresolved lifecycle,
   explicit account allocation/cash bridge and authoritative correction events.
4. `test(v2): adversarial invariants and exact-release read-only packaging` —
   offline financial mutation tests, isolated image/CI, provenance and cheap health.

Each candidate needs actual tests and exact-SHA hosted CI. A deployed collector
does not mean an edge is selected, approved or economically qualified. Future OOS
and independent reproduction remain necessary after code delivery.
