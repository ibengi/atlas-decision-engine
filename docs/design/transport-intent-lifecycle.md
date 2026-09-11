# Transport intent lifecycle and evidence policy

This specification supersedes the unresolved-intent limitation M3 in the
90e460f remediation handoff. It does not enable CAPITAL, broker writes, a
provider, deployment or merge. All implementation tests use isolated synthetic
brokers. Local filesystem commits and broker completion remain distinct events.

## Authority and durable records

`transport_intents.json` schema 2 keeps every row, including terminal rows. A row
binds canonical broker/environment/account identity, immutable HTTP operation and
path, complete economic JSON/params, payload digest, unique intent ID, creation
generation and timestamp, and its append-only transition history. Authentication
and transport-option kwargs cannot enter this store. The same consumed payload
or client-order identity cannot be reused as a new mutation.

The process writer lease (when attached to a client) must belong to this process
and this canonical state root. The root lock spans preparation through handoff;
per-transition generation/content fencing remains in force. Independently pinned
account/credential and continuity proofs are required before preparation and
again before handoff. The manifest and complete intent bytes must remain equal
across the final proof callback. Generation changes cannot be hidden by reentrant
callback writers. Lease ownership, selected authority, state root, account,
environment and credential binding are rechecked after proof callbacks. Revoking
or replacing any of those during a callback cannot authorize dispatch or terminal
publication through a stale proof.

Every state transition uses the root transaction protocol: pending marker,
write-all, file fsync, atomic replace, directory fsync, checksum/manifest readback,
authenticated external CAS, then marker removal and directory fsync. Only that
completed commit publishes the next state. A failed write cannot be reported as a
successful transition. Persistence itself rejects row deletion, payload/history
rewrites, terminal-state changes and skipped transitions.

## Legal transitions

| Current state | Allowed next state | Evidence / meaning |
|---|---|---|
| PREPARED | SENT | Complete immutable intent is durable; dispatch is reserved immediately before adapter handoff |
| PREPARED | CONFIRMED_NOT_APPLIED | On restart, schema-2 PREPARED proves no dispatch began because SENT must commit first |
| PREPARED | TERMINAL_FAILED | Locally terminal validation failure before dispatch; no broker-side assumption |
| SENT | ACKNOWLEDGED | A response arrived; its digest is recorded, but it is not independent completion proof |
| SENT | UNKNOWN | Timeout, disconnect, generic HTTP failure or interrupted handoff; no automatic mutation retry |
| SENT | CONFIRMED_NOT_APPLIED | Adapter explicitly proves failure before calling its dispatch method; a generic connection error is insufficient |
| ACKNOWLEDGED | RECONCILING or UNKNOWN | Read independently observable evidence, or retain incomplete response uncertainty |
| UNKNOWN | RECONCILING | Attempt only broker reads / independently authenticated outcome evidence |
| RECONCILING | CONFIRMED_APPLIED | Exact independent broker presence/cancellation evidence or authenticated final outcome |
| RECONCILING | CONFIRMED_NOT_APPLIED | Authenticated final absence that excludes later acceptance of this exact intent |
| RECONCILING | TERMINAL_FAILED | Independently authenticated definite rejection, bound to this exact intent |
| RECONCILING | UNKNOWN | Evidence unavailable, malformed, conflicted, delayed, incomplete or unproved |
| Any terminal state | None | Retained immutable audit and replay evidence; repeated reconciliation is idempotent |

SENT denotes a durable handoff reservation, not proof that bytes reached the
broker. Restart treats SENT or interrupted RECONCILING as UNKNOWN before reading.
No automatic POST/DELETE/PUT/PATCH retry occurs. Mutating adapter retries are zero.
An unproven 4xx, 409 or 5xx is not converted into definite rejection.

## Independent broker evidence

For order creation, the adapter performs a separate read by stable
`client_order_id` and ticker. Exactly one matching broker order must exist, with
an immutable broker order ID and consistent original side, quantity and price.
Remaining quantity never substitutes for original quantity. Supplied aliases
must agree; conflicting price/action metadata, duplicate rows and partial
responses remain UNKNOWN. The POST acknowledgement alone is insufficient.

For cancellation, a separate order-ID read must show the exact order cancelled
with zero remaining quantity. A 404 does not prove cancellation. Other mutation
shapes require a selected independently authenticated final-outcome authority;
Atlas does not guess their semantics.

Empty complete pagination, repeated empty reads, elapsed time and a 404 do not
prove absence after possible send. The current repository adapter exposes no
atomic final-absence capability. An optional separately pinned outcome provider
may return a signed `transport_outcome` response bound to account/environment,
nonce, generation, request digest, authority identity, issuance/expiry and
monotonic checkpoint. Its signed claims must include exact intent ID, operation,
result and finality. Non-application/rejection additionally requires complete
history, no future acceptance and an observation watermark at or beyond send.
Atlas independently verifies the exact compared watermark and retains the
signed payload/signature for audit. No such real provider is deployed here.

A before-dispatch failure is narrower: the actual adapter can prove that local
key/signing preparation failed before `session.request`. An arbitrary
`ConnectionError` after dispatch begins remains UNKNOWN. Tests exercise a
synthetic transport's explicit no-dispatch evidence without pretending every
network failure has that property.

## Restart, migration and high-level order tracking

Startup calls transport reconciliation even when the high-level pending set is
empty. Normal pending-intent resolution also resumes it each cycle. Unresolved
transport blocks conflicting mutations conservatively across the root. Once
terminal, a different legitimate request can proceed while the old row remains.

The 90e460f schema had PREPARED without a reliable sent boundary. Lossless upgrade
retains the exact original row as `legacy_evidence` and interprets its phase as
UNKNOWN, never as proof of no-send. Unknown/malformed legacy records block;
recognized old intents can resolve from independent order evidence.

High-level `pending_intents.json` cannot close after repeated empty polls. It
requires a matching durable CONFIRMED_NOT_APPLIED or TERMINAL_FAILED transport
row to close absence. A matching CONFIRMED_APPLIED row permits exact order
adoption; unresolved low-level evidence retains the high-level intention. Order
adoption/closure still uses the existing private multi-file order transaction.
A failed closure preserves both layers for restart.

A crash inside an unfinished root filesystem transaction remains
RECOVERY_REQUIRED under the existing recovery protocol. Lifecycle reconciliation
resumes automatically from complete durable states; it does not pretend to
repair incomplete multi-file commits or reconstruct missing broker history.

## Validation and limits

The synthetic suite covers response success, definite signed rejection,
before-dispatch failure, possible-send timeout, UNKNOWN restart, delayed
visibility, duplicate evidence, repeated reconciliation, strong absence,
confirmed presence, cancellation, malformed data, persistence failure, callback
races, immutable history, legacy upgrade and two consecutive legitimate orders.
Actual process-death cases hold signing authority and broker history outside the
child process and explicitly re-establish public trust after restart.

No production authority has been selected. An order that remains invisible with
no independent final absence proof must remain blocked; that is unresolved
broker evidence, not a timer-based unblock. Unsupported generic operations also
need their own independently reviewed observable completion semantics. Root-wide
blocking is intentionally conservative; finer conflict partitioning is deferred.
