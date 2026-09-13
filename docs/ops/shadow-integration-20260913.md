# Controlled Alpha integration candidate

This SHADOW_ONLY candidate combines the source-contract review at local commit
`a2785421bddc741897acaef133b96e2c7e93066b` and provider-identity review at local
commit `4cb0cd45d25752071586e383eb5a60f5f4a4b081` on the packaging release
`2dead89fde8098b06efa07976ffb56c0c8f8c460` and isolated producer
`8c9274f841e9be8742dc6ab5803a348d761ff61a`.

The source contract retains and independently replays market, event and series
response preimages. Fixed-point dollar prices and fractional activity counts
keep their declared units. The producer's default remains legacy-v3; its only
new collection mode is the explicit `market-event-series-v4` profile. A failed
or unsupported source response refuses the poll. No alternate host or access
workaround exists.

Provider receipts retain controlled HTTPS response observations and exact
request/snapshot identity. They are not remote signatures. The reviewed Astra
model mapping remains empty: configured labels and self-authored receipts
cannot establish Astra identity. Existing provider credentials are neither
changed nor included in the evidence package.

The only overlapping runtime edit combines the gateway timestamp rules:
preserve microseconds when either v4 source evidence or a provider receipt is
present. Both observations must precede the actual prediction instant. Legacy
rows retain their existing format; no historical row is rewritten.

Deployment scope is only the separate `atlas-alpha-shadow` and
`atlas-research-readonly` services, after local and hosted verification. Their
own volumes, broker-credential refusal, authority refusal and one-way research
flow remain in force. No production money-path file changes are included.
The separately published LI06 and LI07 production patches are not part of this
candidate and remain undeployed pending separate production review.

## Evidence gates remain distinct

- LI02 requires genuine authenticated joined candidate intake, independent
  checksum replay, valid readiness and malformed-input refusal. Settlement
  authority qualification is LI04 and does not block LI02 once intake is proven.
- LI03 requires an actual source-bound Alpha prediction or safe terminal row,
  complete before/after bytes on the same Railway volume, exact processed
  identity, no duplicates and successful reconstruction after Alpha-only restart.
  Synthetic tests and telemetry alone cannot establish this result.
- LI05 requires an authenticated live provider response, reviewed model mapping,
  retained request/snapshot-bound identity and reproducibility after restart.
  Passing synthetic tests or provider health does not establish identity.
- LI04 requires qualified real outcome authority and complete strict settlement
  binding. An index mentioned in a market's rules, source label or checksum is
  insufficient. Unqualified history remains excluded from learning.

All new local tests use synthetic inputs and deny external transport. The source
branch retains an initial invalid canonical attempt caused by an inherited
unused pre-mock public-market call; the call was removed and final canonical
verification rerun with enforced external transport denial. Setup and diagnostic
failures are not counted as behavioral mutation kills.

No merge, CAPITAL change, broker write, real order, credential change, production
service change or historical-ledger rewrite is authorized by this candidate.
