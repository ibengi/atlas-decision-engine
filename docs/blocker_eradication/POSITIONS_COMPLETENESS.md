# Positions response completeness remediation

Baseline: `033fb43e7858594e7f3d62844830f935bd0275c5`.
Scope: offline remediation candidate. No production deployment, credentials,
orders, risk changes, account repairs, or historical ledger edits.

## Independent reproduction and root cause

`tools/reproduce_positions_false_match.py` executes against the supplied
checkout with socket connections forbidden. At the deployed baseline, unknown
envelopes, hidden continuation pages, late errors on unread pages, and an
unsupported subaccount all return `MATCH` against empty local positions.
These are synthetic witnesses, not evidence about the September 23 account.

`KalshiClient.get_positions` previously performed a single GET and defaulted
unrecognized/missing envelopes to `[]`, discarded cursors and event positions,
and omitted explicit subaccount scope. Both reconciliation paths trusted any
iterable as complete. An empty partial observation therefore became proof of
an empty account and could clear the reconciliation halt.

## Remediation contract

- Only a `PositionSnapshot` can establish reconciliation. It contains immutable
  JSON row/event data, the complete terminal cursor chain, explicit primary
  scope, before/after all-subaccount inventory, and authoritative event binding.
  Bare lists, arbitrary iterators and duck-typed approval markers fail closed.
- Every positions page requires HTTP 200 and the exact known envelope, both
  required arrays, all documented provider row fields and explicit cursor.
  Unknown metadata, malformed rows, nonfinite financial fields, duplicate
  market tickers, cyclic cursors, missing/invalid terminal cursors, and a
  100-page cap with remaining data invalidate the complete observation.
- Strict completeness reads reject duplicate JSON members recursively before
  decoding can discard evidence, including escaped-equivalent names, nested
  quantity/scope fields, cursors, inventory and market metadata. NaN/Infinity
  JSON constants are rejected even in otherwise unused metadata. Native JSON
  decimal numbers decode exactly with Decimal, preventing underflow/rounding
  before schema validation; unsupported numeric financial fields fail closed
  instead of silently becoming zero or an integer. Redirects
  are disabled for these authenticated reads; non-200 statuses are rejected
  before body decoding, as are Content-Range partial-response headers even
  with HTTP 200. Provider rows are validated before raw logging, preserving
  the explicit UNKNOWN classification for malformed numeric bodies.
  Other transport paths retain their existing behavior.
- Every page pins `subaccount=0`, sets `limit=1000`, and applies no ticker,
  event, count or exchange-shard filter. The documented all-subaccount balances
  endpoint brackets the read. Its full required row schema and exact envelope
  are mandatory. Only an inventory containing primary account 0 is supported.
  Nonprimary accounts are not silently excluded even when their cash is zero.
- Event records remain part of the proof. Nonzero event exposure requires an
  authoritative GET `/markets/{ticker}` binding to a returned nonzero market
  position, with exact requested market identity, event identity, HTTP 200 and
  no partial/error metadata. No ticker-prefix or exposure-sum inference is used.
  Historical event costs/PnL with zero current exposure remain valid.
- Zero quantity plus nonzero market exposure is unknown. Quantity parsing uses
  Decimal, preserving the preexisting prohibition on fractional contracts and
  rejecting boolean quantities and precision-losing fractional truncation.
- Any late page, inventory or metadata failure discards the observation.
  Both startup and periodic verification remain non-destructive. A proved
  matching startup observation, like periodic verification, clears the halt.
- All added requests are GETs through the existing guarded transport. The
  broker-write policy and every order/cancel path are unchanged.

This proves documented response completeness and supported scope. The REST
pagination API does not provide an atomic-account snapshot guarantee; this
change does not claim atomicity or infer historical account state from a later
MATCH. Schema changes, unsupported subaccounts, inaccessible metadata, and
unexplained exposure intentionally reduce availability by keeping the halt.

## Primary API contracts

Reviewed September 24, 2026:

- https://docs.kalshi.com/api-reference/portfolio/get-positions
- https://docs.kalshi.com/api-reference/portfolio/get-all-subaccount-balances
- https://docs.kalshi.com/api-reference/market/get-market
- https://docs.kalshi.com/openapi.yaml (independently reviewed by counter-audit)

Positions requires `market_positions` and `event_positions`; explicit empty
cursor is the documented terminal signal. Subaccount defaults to 0, which is
now explicit. Omitting `exchange_index` covers all exchange shards. Required
MarketPosition and SubaccountBalance fields are enforced at the HTTP adapter;
legacy unit tests for quantity/reconciliation use explicitly typed synthetic
complete observations, never relaxed production parsing.

## Verification

Permanent regressions: `tests/test_positions_completeness.py`. They cover both
reconciliation paths, valid empty and valid nonempty controls, hidden pages,
unknown/malformed/partial envelopes, malformed/cyclic cursors, page exhaustion,
late errors, strict inventories, unknown subaccount scope, event contradictions,
metadata identity, Decimal precision, immutable evidence and HTTP 206.
Actual `requests.Response` byte bodies cover duplicate JSON evidence loss,
nonstandard numeric constants, redirect rejection and a valid MATCH control.

Targeted mutation runner: `tools/mutate_positions_completeness.py`.
A clean control and 20 mutants run in separate disposable copies with `-B`,
bytecode disabled, and socket connections denied. All 20 mutants are killed by
regression assertions; collection/import/network errors do not count as kills.
Generated test logs, baseline/candidate witnesses, and detailed mutation JSON
remain outside the tracked checkout. The final combined canonical suite and
exact-candidate hosted CI are integration gates owned by the coordinating agent.
Neither deployment nor production closure is claimed by this report.
