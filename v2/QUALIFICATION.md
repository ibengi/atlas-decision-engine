# Phase 2C qualification evidence

This revision adds public GET-only supplementary collection. It does not change
`alpha_lab.py`, any of its five formulas, its cohort, windows, thresholds, scope,
minimum sample requirements, execution limits or approval flags.

## Deployment scope

Requires separate operator authorization for the exact new commit. Previous
authorization covered f9d3e3b5d043e7f3384cff17a7d5b04b2972bea2 only. The running
service remains pinned to that commit until authorization.

Only `atlas-v2-data` may receive `ATLAS_V2_QUALIFICATION_ON_START=1`. This creates
`/data/atlas-v2/qualification.sqlite` on its existing dedicated volume using the
existing append-only ledger implementation. `observations.sqlite` is neither
migrated nor relabelled. No V1 store is opened. Existing export remains intact.
Qualification exports are separately bound under `qualification-exports/` when
`ATLAS_V2_EXPORT_ON_START=1`; every event and failed attempt remains included.

No credentials are accepted. No financial SDK, order/cancellation method,
position mutation, model approval or live gate is added. READ_ONLY / CAPITAL OFF
and zero broker writes/orders remain mandatory.

## Source contracts

* Settlements: Kalshi public GET `/markets/{ticker}` for V2-observed KXBTC15M
  tickers only. Require `finalized`, nonprovisional binary yes/no, exact ticker,
  event and close-time binding, settlement timestamp between close and receipt,
  and payout equal to result. `closed`, `determined`, disputed, scalar and void
  outcomes are not converted to binary final labels. Revisions append; conflicting
  final outcomes block replay rather than overwrite history.
* B: Coinbase Exchange BTC-USD ticker; use native trade time and native precision,
  preserve receipt time separately. No timestamp rounding, interpolation or
  carry-forward. The frozen formula requires two source times exactly 60 seconds
  apart and the latest within five seconds of the Kalshi observation. Polling
  does not guarantee this; missing pairs stay missing. Coinbase is not BRTI.
* C: Coinbase Exchange 60-second candle buckets. Require all 31 exact consecutive
  closed buckets, valid OHLCV and source receipt received before the decision.
  Missing buckets, duplicates, irregular timestamps, future bars and late
  historical backfill are not admitted as prospective features. Raw extra buckets
  remain preserved and their explicit window-selection count is reported.
* D: complete paginated same-event Kalshi markets collected before the decision.
  Enforce five-second freshness, same expiry, binary strict-greater strikes,
  exact common rules/reference, distinct ordered strikes and center binding.
  Reject incomplete or semantically unproved triples. Current KXBTC15M receipts
  show greater-or-equal contracts, not the frozen strict-greater family. No
  substitution of daily contracts, equality semantics or alternative scope.
* Execution: refresh the public single-market quote immediately after the cohort
  observation, retaining both prices, spread, displayed ask size and timestamps.
  This is top-of-book indicative liquidity, not a guaranteed fill or full book.
  The documented orderbook endpoint requires credentials; this collector does
  not add them. Missing size blocks economic admission.
* Fees: retain series fee metadata and the official schedule PDF before a new
  decision. For quadratic one-contract diagnostics, compute 0.07*M*p*(1-p), add
  one cent rounding allowance and round upward to cents. Recompute using the
  refreshed price. Unknown/zero multiplier is refused. This is an explicitly
  unqualified conservative scenario until schedule version/effective applicability
  and account/FCM fee class are evidenced; a downloaded PDF alone is not approval.
* Slippage: explicitly assume one quoted tick plus any positive ask movement from
  decision to refresh. Always nonzero. It is a stress assumption, not proven
  execution latency cost. Raw refreshed receipts permit later empirical study;
  no historical missing refresh is manufactured.

Official schema/rule references checked 2026-09-26:

* https://docs.kalshi.com/api-reference/market/get-market
* https://docs.kalshi.com/api-reference/market/get-markets
* https://docs.kalshi.com/api-reference/market/get-series
* https://docs.kalshi.com/api-reference/market/get-market-orderbook
* https://docs.kalshi.com/getting_started/fee_rounding
* https://kalshi.com/docs/kalshi-fee-schedule.pdf (July 7, 2026 effective version observed)
* https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker
* https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles

## Offline reproduction

Bounded manual backfill creates a NEW supplementary store, never modifies inputs:

```sh
PYTHONPATH=v2 python -m atlas_v2.qualification snapshot.json anchor.json qualification.sqlite --max-markets 8
```

Export the supplementary store with `export_qualification`; retain its native
external final anchor. Replay with both final anchors:

```sh
PYTHONPATH=v2 python -m atlas_v2.qualification_run snapshot.json anchor.json qualification-snapshot.json qualification-anchor.json PREREGISTRATION.json result.json --cutoff TIMESTAMP
```

Optional `--hypothetical-equity DOLLARS` is an explicit simulation allocation,
never a claim about actual account starting equity. Costs use the SAME `reprice`
function and unchanged limits. The scenario enforces one intent per market,
three simultaneous positions and the 20% drawdown stop; open positions are marked
at zero until authoritative settlement, with absolute and percentage drawdown.
`would_submit` is always false.

Full-cohort paired Brier/log loss/calibration are computed only when every row has
all family features and an admitted label. Caller-derived features/probabilities
are never trusted: raw receipts are reconstructed first. Partial scoring is not
promoted. The runner does not auto-approve statistical independence, fee class,
slippage or a candidate. Until those evidence gates are qualified, statuses remain
NOT_TESTABLE even when diagnostic metrics can be computed. A source hash proves
integrity, not provider authority without the independently retained native anchor.

Seven complete UTC days remain the temporal lower bound. Earlier market rows
cannot gain contemporaneous reference or refresh evidence through a later fetch.
The first complete eligible interval must be recalculated after deployment and
source availability; October 3 is not a guaranteed qualification date.
