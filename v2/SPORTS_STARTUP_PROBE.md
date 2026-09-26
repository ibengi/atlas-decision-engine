# Bounded Sports WebSocket startup proof

Implementation candidate only. No runtime handshake or native quote is claimed by unit tests.

## Activation boundary

Set `ATLAS_V2_SPORTS_PROBE_ONLY=1` on an explicitly authorized exact candidate of `atlas-v2-data`. `service.run()` verifies release identity and financial mode, then starts the probe-only health server before any BTC/qualification/learning store is opened. This is a **temporary diagnostic mode**: the regular collector and learning coordinator do not run during it. Restore the prior exact release and its original configuration afterwards. Existing learning release bindings are deliberately unchanged.

This mode addresses the absence of remote shell execution; it requires no interactive command. The current separately deployed learning release is `55cd4530dc4fc143f9c041eceaada9a1ba88bef4`. Do not deploy a normal-mode replacement into its existing learning ledger under another SHA: its frozen source binding must continue to reject that change. No main/PR79/PR80 merge is necessary or authorized.

Read credentials only from Railway variables `KALSHI_SPORTS_READ_KEY_ID` and `KALSHI_SPORTS_READ_PRIVATE_KEY`. No local copy, source secret, private key log, signature log, account query or financial mutation route is provided. GET `/api_keys` is used only to match the active key to exactly `["read"]`; its body is hash-retained, not persisted verbatim. The key identifier is redacted. Unknown/broader scopes refuse the proof.

## Scope and finite bounds

- Fixed production REST and WebSocket hosts; HTTP and WebSocket redirects refused.
- Public GET metadata allowlist: milestones, events, markets. Authentication is used only for scope verification and WebSocket upgrade.
- Only `ticker` and `orderbook_delta` subscriptions; tickers must belong to one native currently active Sports milestone (soccer/football, tennis or basketball type), with complete primary/related event and market cursor chains. No ticker is accepted from caller configuration.
- At most 10 related events and 32 contracts, 20 pages per chain, 40 HTTP requests, 256 KiB per receipt/message. Exceeding a bound rejects rather than trims membership.
- Internal 180-second budget, HTTP per-operation timeout up to 5 seconds, handshake 8 seconds, send 3 seconds, acknowledgement 8 seconds, message window 30 seconds, 100 messages per connection, close 2 seconds.
- Exactly two connection attempts at most, each fresh-signed with fresh session state. A parent process watchdog ends the child after 190 seconds, including DNS/body stalls, with bounded termination/kill waits of up to 4 further seconds. The health server remains available after the finite probe ends; no recurring probe is scheduled.
- Reconnect success requires two completed sessions with both acknowledgements and actual ticker/delta messages. No quotes, orderbook baselines or acknowledgements carry into the second connection.

## Qualification and known provider evidence gap

R01–R12 are not evaluated or changed. Frozen source/capture spread remains 250 ms and quote age remains 1 second; conservative clock uncertainty is included. Required displayed bid/ask sizes must be positive, prices executable, native `ts_ms` present, identity stable and market open. Missing/unknown/changed membership, replay, gaps, malformed depth and stale messages fail closed. Source frames and rejected observations remain distinct from admitted quotes.

A ticker frame supplies explicit bid/ask and sizes; no midpoint substitute is used. The initial orderbook snapshot lacks a documented source timestamp and is retained only as an unqualified baseline. A delta does not refresh the age of all depth levels. Snapshot counts refer to contemporaneous complete ticker legs, not qualified reconstructed full-depth books.

**Current source-clock evidence is insufficient for final qualification.** Linux `adjtimex(modes=0)` supplies only a read-only local clock bound. It cannot attest Kalshi's clock accuracy. The adapter therefore reports `source_uncertainty_ms=null`; it never fabricates the frozen source bound or exposes an operator override. Native ticker receipts, native time, receive time and observed delta are still captured, but admitted quote/snapshot counts remain zero with `SOURCE_CLOCK_BOUND_UNPROVEN`. A separately reviewed, authoritative provider-bound clock/timestamp evidence adapter is required before READY can become reachable on genuine production data. Synthetic test clocks exercise the admission code only and are not used by the runtime adapter.

Complete pre/post pagination and equal milestone membership are required. A provider catalogue that cannot establish the relation's complete membership remains rejected. The probe does not expand the twelve relations, claim cross-market edge, calculate costs or approve a candidate.

## Evidence

The only opened data store is `/data/atlas-v2/sports-probe/sports-probe.sqlite`, using the existing append-only hash chain. Per-run immutable JSON contains non-secret evidence and an external ledger anchor. Scope responses, errors and sensitive frames retain hashes only. Public validated frames may retain raw base64 plus SHA-256; decoded/escaped key material is screened too. No exception text from a network/cryptographic library is logged.

Events include scope match, discovery/pagination receipts, membership, handshake, subscriptions, native ticker observations, book baseline/deltas, local clock readings, admission/rejection, reconnect generation and final result. Reported handshake counts are actual completed upgrades. Qualified counts are admitted only after a complete unchanged membership recheck and successful session validation. Rejected generation admissions are revoked in append-only events. Guard self-checks are explicitly synthetic negative tests and contribute zero native observations.

Startup output is a sanitized result with source SHA, run ID, exact refusal code, counts, zero broker writes/orders, evidence path/hash and ledger anchor. `/health` and `/status` expose only this non-secret result. A hard process timeout yields a blocked status; any partially persisted ledger is retained, not rewritten.

## Deployment review

No deployment is performed by these source changes. Before temporary activation, authorize the exact published candidate, same service/project/environment/volume, `ATLAS_V2_SPORTS_PROBE_ONLY=1`, and matching exact-SHA predeploy check. Preserve all secret values and financial flags. The temporary pause of the ordinary collector is an explicit operational effect. Restore exact learning release `55cd4530dc4fc143f9c041eceaada9a1ba88bef4` and disable probe-only mode after collecting the bounded proof. Do not silently rebind or migrate its learning ledger.

## Official interfaces checked

- https://docs.kalshi.com/websockets
- https://docs.kalshi.com/getting_started/quick_start_websockets
- https://docs.kalshi.com/websockets/market-ticker
- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/api-reference/api-keys/get-api-keys
- https://docs.kalshi.com/api-reference/api-keys/create-api-key
- https://docs.kalshi.com/api-reference/events/get-events

Dependency `websockets==16.0` is pinned and its redirect behavior is explicitly overridden. Cryptography remains pinned to 46.0.0. No broker SDK is shipped.
