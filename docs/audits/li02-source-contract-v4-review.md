# LI-02: explicit modern source contract — review only

Branch: `alpha/astra-source-contract-v4-review`.

Base: `8c9274f841e9be8742dc6ab5803a348d761ff61a` (isolated producer on the audited v5 packaging release).

This patch is not deployed. Legacy v3 remains the default collector mode. It
does not close LI-02 or qualify a settlement authority. Enabling the new mode
requires independent review and genuine authenticated event/series captures.
No broker, execution, risk, order, credential, production-service or main change
is included. Alpha remains SHADOW_ONLY; no new execution consumer is introduced.

## The observed incompatibility

The permanent fixture `tests/fixtures/li02_market_capture_20260913.json` is the
unchanged 3,600-byte real public market capture. Its decoded response is 2,145
bytes. Record SHA-256 is
`469739d2d736287c262ac4962eb132bf9103853ca0ab1949fe26a09e97e94add`;
response SHA-256 is
`5450d82401be7f8f6a90cf774c4419c15c095bf2fafd7f192cba84eabc864d8e`.
The selected market is `KXBTC15M-26SEP131615-15`.

Its observed quotes use four-decimal dollar strings, and volume/open interest
use two-decimal fractional contract counts. Expected expiration is
2026-09-13T20:20:00Z, while final/latest expiration is
2026-09-20T20:15:00Z. These are distinct observations. The market has no
structured settlement-source member. Rules mentioning an index cannot supply
an independently verified source preimage or settlement authority.

The unchanged v3 validator refuses this real capture. V4 also refuses this
market-only capture until its genuine parent metadata is supplied. No raw field
is renamed to pretend that legacy observations were present.

## Explicit contract and automatic collector

`research_source_contract_v4.py` implements pure replay of
`atlas-market-event-series-bundle-v1` into `atlas-research-candidate-v4` using
`kalshi-binary-usd-4dp-count-2dp-v1` normalization. Each bundle retains exact raw
market, event and series response bytes, byte hashes, capture identities,
observer timestamps, fixed URLs, and the exact market JSON pointer.

Replay checks the market's event identity against the event response and the
event's series identity against the series response. Structured source identity
comes from the retained series settlement-source list. Supplied identity/source
aliases and optional event-market collection members cannot contradict the join.
Mixed legacy numeric fields, malformed source containers, unsupported source
extensions and unsupported capture metadata refuse. Separately timed event
quotes are not claimed to be an atomic copy of the selected market quotes.

Quotes require four-decimal string syntax, binary unit payout, finite bounds
and lossless JSON numeric round-trip. Counts require two-decimal strings and
retain fractions. All four quote sides are independently observed. Expected,
final, latest, close and occurrence times remain distinct in retained bytes.
Capture skew is limited to 120 seconds. Capture timestamps, future source
updates, and prediction-before-metadata chronology are verified independently.

Default `RESEARCH_SOURCE_CONTRACT=legacy-v3` behavior is unchanged. The explicit
review-only option `market-event-series-v4` supports this automatic sequence:

1. Fixed public market GET for the bounded KXBTC15M sample.
2. Fixed-host event GET for each validated exact event ticker.
3. Fixed-host series GET for the independently observed KXBTC15M series.
4. Durably retain captures; validate every bundle in the batch; publish only
   independently replayable candidate records.

The opt-in has at most one market, ten distinct event, and one series request
per poll. Metadata identifiers cannot create another host, query or route.
HTTPS certificate verification, no redirects, no proxy inheritance, body caps,
timeouts, authenticated read-only routes and bounded rolling storage remain in
place. Unknown schemas, HTTP 403, incomplete metadata and failed durability
barriers refuse without alternate hosts or inferred sources. No inbound route
can initiate capture or send control back to the producer/execution engine.

Hashes establish byte integrity and replay establishes the semantic join.
Neither a TLS label nor an operator source label independently authenticates an
offline capture. The offline qualification tool consequently always reports
live authenticity and settlement authority as unproven. A synthetic complete
join is a semantic positive control, never real authority evidence.

## Changed paths

| Path | Purpose |
|---|---|
| `research_source_contract_v4.py` | Bounded pure source replay and normalization |
| `candidate_contract.py` | Exact schema-version dispatch; legacy body untouched |
| `source_identity.py` | Replay the retained structured v4 source join |
| `alpha_feed_readiness.py` | Recognize explicitly supported versions |
| `alpha_settlement_validation.py` | Exact version/environment and metadata-time binding |
| `alpha_gateway.py` | Preserve microseconds for v4 predictions; legacy seconds unchanged |
| `readonly_research_producer.py` | Opt-in fixed GET collector, durable bundle intake, truthful page schema |
| `tools/alpha_live_schema_qualify.py` | Offline bundle interface; no authority overclaim |
| Tests, fixture and mutation/isolation tools | Permanent witnesses and reproducible local verification |

The shared consumer, snapshot representation, prediction/processed/budget
durability protocols and historical ledger formats are unchanged. Existing
replay excludes invalid history without rewriting it. The strict neutral import
allowlist explicitly pins the new pure module and its complete import set;
the guard is extended, not removed.

## Counterexamples retained and neighboring tests

The new module contains 34 tests. All event/series metadata, predictions and
settlements in these tests are explicitly synthetic. They retain the genuine
market fixture without changing its bytes.

Tests cover missing parent metadata, exact raw replay, distinct schedule fields,
fractional quantities, malformed decimals, missing quote sides, source/identity
contradictions, unsupported nested source members, duplicate identities,
capture skew and future updates, environment and pointer/unit tampering,
all-four-derived quotes, raw-preimage changes with recomputed hashes, strict
settlement replay, append/restart bytes, six repeated failed synchronization
barriers, default-mode isolation, fixed fake-HTTP joins, 403 refusal, unknown
schema refusal, disallowed URL rejection, capture scope/completeness and
continuation contradictions, and same-second prediction chronology.

Independent review first reproduced seven accepted contradictory/invalid
aliases or parent collections; all now refuse. It also found that gateway
second truncation falsely ordered a valid prediction before microsecond source
metadata. V4 preserves the exact instant; a genuinely earlier prediction still
refuses. Self-review additionally corrected the page envelope's misleading v3
version and tightened untrusted capture completeness/continuation claims.

Independent final evidence records 35 semantic executions and 17 static
non-regression checks passing. These checks establish code behavior, not real
exchange/settlement authentication.

## Verification and truthful mutation results

The final canonical suite completed under the credential-free external-network
isolation runner: **2,248 tests; 0 failures; 0 errors; 0 skips**. Collection was
2,243 unittest cases plus 5 module-level tests. This includes the historical
AA, NEW-01 and v5 regression suites. The 34 new tests plus 29 existing isolated
producer tests passed together, with zero unexpected external-transport denials.

The supplied historical mutation suite produced 53 final classifications:
50 `KILLED_BEHAVIORALLY`, two declared `DIAGNOSTIC_ONLY` controls (M06, M17),
and one independently demonstrated ineffective `SURVIVED` control (M01).
There are zero surviving effective safety mutations and zero final
inconclusive experiments. M08's initial invalid baseline is retained as
inconclusive; only its clean rerun with the reviewed import pins is a kill.

The new scoped suite has 14 mutations: normalized replay, event join, series
join, expected-time semantics, fractional counts, dollar units, identity aliases,
optional collection schema, mixed-unit refusal, capture skew, environment,
derived quotes, prediction precision, and account-completeness claims. All 14
are `KILLED_BEHAVIORALLY` by named invariant assertions. No setup, collection,
import or infrastructure failure counts as a behavioral kill.

Reproduction commands (from this branch, with dependencies installed):

```sh
python tools/offline_tests.py -m unittest tests.test_li02_source_contract_v4 tests.test_readonly_research_producer
python tools/offline_tests.py -m tools.li02_contract_mutations --evidence-dir /tmp/li02-v4-mutations
python tools/offline_tests.py -m tools.astra_mutation_probe --json --evidence-dir /tmp/li02-historical-mutations
```

An initial inherited canonical invocation made an unintended public market-data
transport attempt before its synthetic monkeypatch was installed. That run is
invalid evidence. The unused pre-mock test call is removed. Final verification
uses external DNS/socket denial with a credential/proxy-free environment;
there were no broker writes or real inference-provider requests. A second
incomplete canonical invocation is likewise not counted as a pass.

Full raw test logs and structured mutation phase receipts accompany the
integration evidence package. Docker and hosted CI have not been run for this
review-only branch. No runtime mode has been enabled and no deployment was made.

## Remaining required inputs

- Genuine authenticated event and series captures satisfying the explicit
  source profile and exact joins; the market capture alone remains refused.
- Independent qualification of the actual settlement authority and outcome
  evidence, distinct from market-source identity.
- Independent code review, then separate authorization for any deployment or
  runtime mode change; exact deployment/CI and real intake/restart proofs follow.

**LI-02 remains OPEN. This is an isolated review package, not a live-readiness
recommendation.**
