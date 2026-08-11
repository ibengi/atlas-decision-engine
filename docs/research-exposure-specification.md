# Decision Engine Research Exposure Specification (Program 008E)

Read-only. This interface defines no operation that changes engine state.

## Scope of the change

Three files touched, in one branch, `review/008e-evidence-exposure`:

| file | change | business logic touched |
|---|---|---|
| `research_export.py` | new; reads persisted evidence | none |
| `dashboard_web.py` | +3 routes on the existing daemon-thread handler | none |
| `opportunity_pipeline.py` | +3 keys on the record written to `decisions.jsonl` | none |

Nothing in strategy, market selection, probability, calibration, edge, EV,
fees, slippage, filters, risk, Kelly, sizing, execution, ordering, settlement
or scheduling was modified. Proven by replay — see
`docs/engine-equivalence-report.md`.

## Endpoints

All under `/api/research/v1`, served from the dashboard's existing daemon
thread, which was already off the trading critical path.

| endpoint | returns |
|---|---|
| `GET /capabilities` | what this engine can serve, and what it cannot |
| `GET /decisions?cursor=&limit=` | historical decision records, exactly as written |
| `GET /settlements?cursor=&limit=` | authoritative accounting records |

### Authorisation

Bearer token in `RESEARCH_API_TOKEN`, compared with `hmac.compare_digest`.
**No token configured means refusal, not open access.** An evidence endpoint
that serves everything when nobody set a token is worse than one that serves
nothing.

The token grants research reads only. There is no route on this interface for
trade control, admin operations, configuration, secrets, arbitrary queries,
the filesystem or a shell.

## Decision schema

The record is the engine's existing `Decision.to_dict()` plus `final_decision`,
wrapped in an envelope. 008E adds three envelope keys and changes nothing
inside `decision`.

```
{
  "cycle_id":    str,        # pre-existing
  "decision":    { ... },    # pre-existing, unchanged, 23 fields
  "run_id":      str|null,   # 008E: the tracer's per-cycle id
  "recorded_at": str,        # 008E: ISO-8601 UTC
  "lineage":     { ... },    # 008E: see below
  "record_id":   str         # added by the export layer, not stored
}
```

`decision` keys, preserved exactly: `ticker`, `accepted`, `rejection_reason`,
`strategy`, `market_type`, `category`, `side`, `entry_ask`,
`model_probability`, `market_probability`, `gross_edge`, `net_edge`, `net_ev`,
`confidence`, `taille`, `reason`, `estimated_fees`, `expected_slippage`,
`model_output`, `decision_id`, `spread`, `liquidity`, `kelly_fraction`.

**Accepted and rejected decisions are both exported.** The rejected ones are
the larger half and the more informative one.

## Settlement schema

The engine's existing trade record, unchanged: `trade_id`, `order_id`,
`ticker`, `timestamp`, `settled_at`, `state`, `result`, `won`, `gross_pnl`,
`fees`, `net_pnl`, `roi`, `holding_seconds`, `avg_fill_price`, `filled_count`.

**No PnL is recomputed.** Where a record does not reconcile — `gross − fees ≠
net` — the discrepancy is listed in a `discrepancies` array and the record is
served unchanged. Correcting history in an exposure layer is how the corrected
figure becomes the one people quote.

## Model identity

`model_version` comes from the engine's declared `MODEL_VERSION` where one
exists, alongside `model_artifact_sha256`: SHA-256 over the source of
`btc_probability_model.py`, `strategy_router.py` and `pattern_engine.py`.

Deterministic across restarts, and different only when the definition is
different. Stamping a new version on every boot would make lineage useless in
exactly the way lineage exists to prevent.

## Calibration identity

`calibration_version` is `cal-<sha256[:16]>` over `calibration.py`,
`model_calibration.py` and `model_gatekeeper.py`. It identifies calibration; it
does not alter it, and `behaviour_changed: false` says so on every response.

## Cursor and pagination

`record_id` is a SHA-256 of the raw line, so it survives the rotation that
renames the file underneath it. Ordering is oldest generation first, line order
within a generation — a total order, stable across requests.

A cursor whose row is no longer retained returns `cursor_state: "expired"` with
a note. **Evidence between the last cursor and the oldest retained row is
gone**, and that is reported rather than papered over by silently restarting
from the beginning, which would duplicate history.

Page size is bounded at 500.

## Correlation

Available on new records: `run_id`, `cycle_id`, `decision_id`.
Available on settlements: `trade_id`, `order_id`.

**Not available, and not invented:** decision → order. The engine does not
record an order identifier on the decision record, so the two cannot be joined.
Records written before this branch carry no `run_id` and none is back-filled.

## Retention — read this before relying on the decision feed

`decisions.jsonl` rotates at 5 MB × 4 generations. Rows past that are deleted
and unrecoverable. This is pre-existing behaviour and was **not** changed here.

The 008E lineage stamp grows a decision row by ~259 bytes (25.9%), which
reduces retained rows per generation from ~5,248 to ~4,167 — **about 20% less
retained history**. That is a real cost of observability, paid in the currency
the observability exists to preserve.

A retention change was not authorised and was not made. If permanence is
wanted, it needs approval as its own decision.
