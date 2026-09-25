# Atlas V2 foundation — RESEARCH / READ_ONLY

The production collector only performs public Kalshi market GETs. It has no
broker credentials, private account API, model, order/cancel/transfer adapter or
LLM dependency. The failed V1 model is not imported. The repository root V1 files
remain recoverable history; the V2 image copies only this directory.

Run offline invariants:

```sh
PYTHONPATH=v2 python -m unittest discover -s v2/tests -v
python v2/mutate.py
```

Inspect a new dedicated database or explicitly collect one public scan:

```sh
PYTHONPATH=v2 python -m atlas_v2 /path/to/atlas-v2/observations.sqlite
PYTHONPATH=v2 python -m atlas_v2 /path/to/atlas-v2/observations.sqlite --collect-once
```

The service runs `python -m atlas_v2.service`, with a dedicated volume mounted at
`/data` and `ATLAS_V2_DATA_DIR=/data/atlas-v2`. No V1 state is migrated. `/health`
reports cheap liveness; `/status` reports collection state, last success/error,
source SHA and hash-chain anchor. Neither endpoint exposes raw/account data or
grants qualification. Copy anchors to independent immutable storage before
research admission; local SQLite/hash chains do not prove absence of whole-file
replacement, tail truncation or destruction of the volume.

The public collector preserves failed/partial page receipts and records a failed
scan. Only explicit terminal cursors permit a complete traversal. Traversal is
not an atomic exchange snapshot. Missing features, underlying prices, depth,
models and settlement authority remain missing. Source update time is retained,
not relabelled as an independently certified quote timestamp. Collected data is
not automatically prospective approval-quality data.

## Research boundary

`validation.py` supports preregistration, event-disjoint split manifests, immutable
candidate fingerprints, post-lock predictions and identical-row market metrics.
It refuses consumed V1 datasets, pre-lock observations, after-close predictions,
baseline changes and selected subsets of persisted predictions. It reports
diagnostics with `approved=false` at all times. Training/calibration artifact
authentication, automated predeclared stopping/multiplicity checks, underlying
basis and execution-cost qualification remain required before scientific approval.
No candidate or hypothesis is automatically selected, locked or fitted.

`approval.py` checks an externally authenticated Ed25519 review signature against
source/model/features/thresholds/config/dataset/lock/validation/review/cost hashes.
No approver public key is trusted by default. A research review never enables
financial authority, even with a valid signature or environment flags.

## Economic simulation boundary

`execution.py` is an offline shadow-intent harness, not a real broker adapter.
It reapplies economic limits to the refreshed selected-side quote, uses the same
upward-rounded fee bound for edge and sizing, fences control changes, enforces
durable uniqueness, and reserves cash/position slots transactionally. A simulated
intent always says `would_submit=false`. No TTL releases unresolved reservations.

`accounting.py` calculates receipt-referenced cash bridges and unitized Atlas
equity; it never invents an opening allocation or cashflow cause. PnL, external
flows and marks remain separate; corrections reference originals. The 20% live
drawdown limit is unchanged. Lifecycle inputs are explicitly simulations, and
their receipt hashes are not authenticated broker authority. They cannot repair
an account, release a live position or qualify actual settlements. Dedicated
Atlas account/subaccount evidence is required before any future execution design.

## Qualification still open

Provider-effective authenticated account/fractional schemas; immutable external
anchors; independent settlement/rules labels; actual training/calibration lineage;
preregistered statistical thresholds and stopping; authentic cost/basis study;
future OOS superiority; external independent review; and separately authorized
financial capability. No real canary, new credentials, risk increase or V1 ledger
rewrite is part of this foundation.
