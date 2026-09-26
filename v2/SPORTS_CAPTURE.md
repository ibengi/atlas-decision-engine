# Prospective sports capture — blocked source integration

This opt-in CLI is separate from the BTC service and has no deployment hook.
It performs bounded public GET discovery and retains raw native receipts in a
new append-only SQLite store. No credentials, order client or historical endpoint
is available. It must be invoked with a new output directory:

```bash
PYTHONPATH=v2 python -m atlas_v2.sports_capture /new/isolated/output-directory
```

The run makes at most 100 requests, 20 pages per cursor chain and 120 seconds
before a subsequent request is refused. Each request has a five-second timeout
and two MiB response bound. Partial scans, source errors, malformed envelopes,
unknown sports types and excluded ended events are explicitly retained. There
is no hidden retry loop or background process. Native milestone type prefixes
are discovery hints, not verified exhaustive competition coverage.

The CLI captures the public filters, complete bounded Sports milestone pages,
union of primary/related event anchors, market pages and requested active books.
Every raw receipt has a body hash and receive timestamp. Opposing bids are
converted to asks with the identical native displayed quantity. Missing depth
is never filled. A batch is checked for exact requested ticker coverage.

**This is raw prospective acquisition, not a qualified live feed.** The REST
book schema has no documented quote-as-of timestamp. Metadata update timestamps
and local receipt times cannot replace it. All comparisons therefore remain
SNAPSHOT_REJECTED until a source-attested native stream and complete stable
event membership are available. No caller-supplied approval flag enables one.
Limits remain 250 ms synchronization and 1,000 ms quote age. R01–R12 are unchanged.

The authenticated native ticker stream documents millisecond source timestamps,
bid/ask and top-level sizes. A trusted live transport, native timestamp-semantics
proof, lifecycle/clock handling and complete membership binding are still
required. This CLI does not sign or open that connection. No broker credentials
were added or extracted. Subsecond streaming, relationship-triggered full-leg
refresh, rule-bound relationship evaluation and qualified fee/slippage/PnL
calculations **are not yet integrated** and are not claimed operational.

References:

- https://docs.kalshi.com/websockets/market-ticker
- https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks
- https://docs.kalshi.com/api-reference/milestone/get-milestones

No deployment is authorized by this code. A persistent collector requires a
separately reviewed runnable source integration and exact deployment approval.
