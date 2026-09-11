# Astra AA-01 .. AA-18 — remediation report

**Branch:** `alpha/astra-candidate-feed-v2-remediation`
**Rejected candidate:** `alpha/astra-candidate-feed-v1` @ `898cac1b6ff53e1f00cf092876454801c4e41d20` (untouched)
**Verdict sought:** `SAFE_FOR_INDEPENDENT_REAUDIT` — **not** production-ready, **not** CAPITAL-ready.

This document does not defend the rejected candidate and does not soften any
finding. Where a claim cannot be proven from inside this repository, it is
recorded as unproven rather than argued around.

---

## 1. Protected areas

| Area | Count |
|---|---|
| `MAIN_CHANGES` | **0** — `origin/main` at `5c4e789`, untouched |
| `PRODUCTION_DEPLOYMENTS` | **0** |
| `BROKER_WRITES` | **0** |
| `CAPITAL_CHANGES` | **0** |
| `RISK_GATE_CHANGES` | **0** |
| `HISTORICAL_LEDGER_REWRITES` | **0** |
| Rejected branch modified | **0** — still `898cac1` |
| Railway variables touched | **0** |

`config.py` changed only by ADDING four research-spool/queue bounds and one
read-only `ALPHA_ENVIRONMENT` label. No `ALLOW_ORDER_SUBMISSION`,
`LIVE_BROKER_WRITES_AUTHORIZED`, `MODEL_APPROVED`,
`DAILY_RESEARCH_ORACLE_APPROVED`, `KILL_SWITCH`, risk threshold, drawdown
guard, order cap, credential or execution-mode value was read, written or
moved.

The only money-path edit in the entire diff is the argument list of the
existing research hook in `execution_engine._shadow_observer`: the raw market
is now passed alongside the normalized book. `order_manager.py`,
`kalshi_client.py`, `risk_manager.py`, `position_sizer.py` and
`market_validator.py` are byte-identical to the rejected candidate.

---

## 2. Architecture

```
RAW EXCHANGE OBSERVATION            market dict exactly as published
        |                           (quotes live here; nothing derived)
        v
candidate_from_market()             AA-01: quotes read ONLY from raw;
        |                           each tagged observed | derived
        v
NEUTRAL VALIDATED EVIDENCE          candidate_contract.validate_record()
        |                           AA-02/03/05/06/07/08 — ONE contract,
        |                           imported by producer, consumer, readiness
        v
NONBLOCKING RESEARCH QUEUE          AA-10: bounded by items AND bytes;
        |                           put_nowait, drops under pressure;
        |                           the engine never waits for disk
        v
ISOLATED WRITER THREAD              all serialization, write, fsync, prune
        |
        v
BOUNDED DURABLE SPOOL               AA-11: count + bytes + temp files;
        |                           fails CLOSED when capacity is unknown;
        |                           never deletes an unknown file
        v
CHECKSUM-VERIFYING CONSUMER         AA-04: digest RECOMPUTED and compared;
        |                           AA-09: per-record failure containment
        v
APPEND-ONLY PREDICTION              AA-12: complete writes, torn tail closed
        |                           by separation, never truncation
        |                           AA-13: PREPARE -> commit -> ack
        |                           AA-14: check+append under one lock
        v
VERIFIED SETTLEMENT BINDING         AA-15: source identity re-checked;
        |                           mismatch = REJECT, both values reported
        v
LEARNING                            AA-16: a derived report can never
                                    replace a source ledger
```

`candidate_contract.py` is deliberately **not** named `alpha_*`. The producer
runs inside the engine, and the repository's safety boundary is keyed on that
prefix; a shared module carrying it would have made the engine look dependent
on the research subsystem.

---

## 3. AA-01 .. AA-18 status

Every row was reproduced against the rejected behaviour before being closed.

| ID | Status | Evidence |
|---|---|---|
| **AA-01** derived quotes as observed | **CLOSED** | `AA01_DerivedQuotesArePresentedAsObserved` (6 tests). Control proves `normalize_book` *still* derives the NO side for execution; research refuses it; no prediction row is created. |
| **AA-02** malformed types as facts | **CLOSED** | `AA02_MalformedTypesPassAsMarketFacts` (13 tests + subtests) covering booleans, NaN, ±Inf, negative sizes, impossible prices, crossed books, arrays/objects as text, blank text, malformed settlement collections, malformed timestamps, 2**70 integers, numeric strings. One shared contract asserted by import. |
| **AA-03** contradictory aliases | **CLOSED** | `AA03_ContradictoryAliasesSilentlyChosen` (6 tests). Neither value is chosen; both are named. `close_time` is proven *not* to be an alias of the resolution time. |
| **AA-04** checksum not verified | **CLOSED** | `AA04_ConsumerDoesNotVerifyTheChecksum` (6 tests). Digest recomputed; verified digest retained downstream. The integrity-not-authenticity limit is asserted, not glossed. |
| **AA-05** provenance only a string | **CLOSED** | `AA05_ProvenanceWasOnlyANonemptyString` (8 tests). `yes_ask` from `market.title` refused; enforced at producer, consumer and readiness. |
| **AA-06** missing time gets the clock | **CLOSED** | `AA06_MissingObservationTimeGetsTheCurrentClock` (5 tests). Same record replayed an hour later mints the same identity; static check that `mint()` reads no clock. |
| **AA-07** readiness bypassed provenance | **CLOSED** | `AA07_ReadinessBypassedProvenance` (4 tests) + `test_alpha_feed_readiness.py`. Shape-complete row is now NOT ready, with `missing_fields == []` proving it is the contract refusing it. |
| **AA-08** `event_id` mismatch | **CLOSED** | `AA08_EventIdContractMismatch` (4 tests). Optional in all three; a supplied value must validate and be attributed. |
| **AA-09** one bad row starves the batch | **CLOSED** | `AA09_OneBadRowStarvesTheBatch` (5 tests). Poisoned row skipped, batch completes; a programmer error still raises. |
| **AA-10** research I/O blocks the cycle | **CLOSED** | `AA10_ResearchIOBlockedTheEngineCycle` (7 tests). Writer fsync held indefinitely; 50 further emits complete in < 2 s. Static check: no filesystem call reachable from `emit_candidate`. |
| **AA-11** spool limits fail under faults | **CLOSED** | `AA11_SpoolLimitsFailUnderFilesystemFaults` (8 tests). Enumeration failure fails closed; byte bound; oversize refusal; temp bytes counted; unknown files never deleted. |
| **AA-12** short writes / torn tail | **CLOSED** | `AA12_ShortWritesAndTornTails` (7 tests). One-byte-at-a-time write completes; EINTR retried; torn fragment preserved and separated, never truncated. |
| **AA-13** commit vs processed ack | **CLOSED** | `AA13_PredictionCommitVersusProcessedAck` (8 tests). Stable `analysis_id`; one snapshot → at most one prediction; non-committed → DEFERRED; restart reconciliation reports, never repairs. |
| **AA-14** multi-writer races | **CLOSED** | `AA14_MultiWriterRaces` (6 tests). 8 threads race → exactly 1 prediction; 6 threads race → exactly 1 resolution; conflicts surfaced. Model documented in `docs/design/alpha-writer-model.md` with the per-host advisory limits stated. |
| **AA-15** R4 join not verified | **CLOSED** | `AA15_R4JoinIsNotVerified` (8 tests). Every binding field checked; mismatch rejected with both values; malformed `resolved_at` refused; optional trusted-source allow-list. |
| **AA-16** report aliases the ledger | **CLOSED** | `AA16_ReportOutputCanAliasTheSourceLedger` (7 tests). Path, `..`, symlink and hard-link aliases all refused; ledger bytes unchanged. |
| **AA-17** surviving safety mutations | **CLOSED** | `tests/test_astra_mutation_regression.py` (26 tests) asserts EFFECT — no snapshot minted, no prediction appended. `tools/astra_mutation_probe.py`: **11/11 killed, 0 survivors**. |
| **AA-18** CI misses the branch | **CLOSED** pending the hosted run | Workflow targets the remediation branch, preserves `alpha/astra-learning-v1`, adds PR triggers, and runs every required suite. |

**`SURVIVING_SAFETY_MUTATIONS = 0`.**

---

## 4. Mutation results

`python tools/astra_mutation_probe.py`

| Mutation | Result |
|---|---|
| M01 fallback `"kalshi"` | KILLED |
| M02 fallback `0.0` | KILLED |
| M03 `close_time` as resolution time | KILLED |
| M04 ignore provenance *(survived before)* | KILLED |
| M05 provenance excluded from checksum | KILLED |
| M06 accept legacy v1 | KILLED |
| M07 missing book side | KILLED |
| M08 forbidden execution import | KILLED |
| M09 producer exception propagation | KILLED |
| M10 overwrite historical bytes | KILLED |
| M11 accept legacy, keep diagnostics *(survived before)* | KILLED |

**11/11 killed, 0 survivors.** The runner copies the repo to a throwaway
directory; the working tree is never mutated. Each mutation asserts its anchor
text is present before applying, so a refactor that moves the code reports
`NOT_APPLIED` rather than a false KILLED.

---

## 5. Remaining external blockers

Unchanged by this remediation, and deliberately not worked around.

* **R1 — Railway `/data` runtime restart proof.** Not obtainable without
  touching production. Open.
* **R3 — real Astra identity.** `ASTRA_IDENTITY_UNPROVEN`. Not solved by
  renaming OpenAI/Grok/Gemini; a test asserts no such alias exists.
* **Live exchange schema capture.** `LIVE_SCHEMA_UNPROVEN`. No real payload has
  been shown to carry `rules_primary`, `settlement_sources`,
  `expected_expiration_time` and all four quotes.
  `tools/alpha_live_schema_qualify.py` discharges this later from a captured
  read-only payload; it makes no network call and refuses to fabricate one.
* **Settlement authority qualification.** The binding check proves a settlement
  is *consistent with* the prediction it names. It cannot prove the settlement
  came from the exchange. `trusted_sources` is off by default because no
  authority has been qualified.

A checksum proves byte integrity, never source authenticity. Anyone able to
rewrite a record can recompute its digest; a test pins that this is stated
rather than overclaimed.

---

## 6. What this report does not say

It does not say production-ready. It does not say CAPITAL-ready. It does not
say CI-proven beyond the hosted run recorded at merge time on this exact SHA.
The only positive endpoint sought is **`SAFE_FOR_INDEPENDENT_REAUDIT`**, and
that judgement belongs to the counter-audit, not to this document.
