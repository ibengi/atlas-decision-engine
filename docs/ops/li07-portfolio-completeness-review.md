# LI-07 portfolio completeness: separate production review

Status: **READY_FOR_SEPARATE_PRODUCTION_REVIEW**. This patch is undeployed and does not close the production preflight gate.

Base: `5c4e7897a0b99065f3a23bc4ed834b44dba9580c`.
Review branch: `review/li07-portfolio-completeness`.

## Problem and reproduced invariant violations

The original positions reader could turn an unfamiliar nonempty envelope into an empty list, and read only the first page despite a continuation cursor. Both cases could produce `MATCH` against an empty local ledger. The original four-case reproduction includes known-empty and visible-position disagreement controls. All four are retained in `tests/test_li07_portfolio_completeness.py`.

The invariant is: reconciliation may compare only a completely enumerated, recognized, validated portfolio scope. Unknown schema, incomplete pagination, malformed quantities or identities, and unreadable local state cannot become absence or clear an existing halt.

Independent review also found duplicate JSON fields discarded before validation, decimal fractions rounded into whole contracts, contradictory filtered order identities, hidden auxiliary pagination, and malformed local state filtered out before reconciliation. Permanent tests cover each case and positive controls.

## Patch behavior

`kalshi_client.py` provides one bounded pagination collector for positions and current orders. It validates every page before returning the collection, rejects unknown/conflicting envelopes and pagination fields, cursor cycles, exhausted page bounds, duplicate economic identities, malformed rows and contradictory aliases. A later page failure never publishes earlier rows as a complete account.

Only successful GET responses for `/portfolio/positions` and `/portfolio/orders` receive strict JSON decoding. Duplicate members and nonfinite numbers are rejected; decimal precision survives until exact whole-contract validation. Validated numeric values are normalized before JSON logging or order persistence. Mutation decoding, transport guards, signing and retry behavior remain unchanged.

Positions and orders explicitly request primary subaccount `0`, matching this baseline's writer scope. A returned contradictory subaccount is refused. Positions support the current `market_positions` envelope and the repository's existing `positions` compatibility envelope, exactly one per enumeration. Supplemental event summaries are validated but never substituted for market holdings.

Orders require an explicit string cursor; positions permit the documented optional string cursor. An empty string terminates either listing; an omitted positions cursor terminates that listing. Explicit null is rejected. These profiles follow the public [positions API](https://docs.kalshi.com/api-reference/portfolio/get-positions) and [orders API](https://docs.kalshi.com/api-reference/orders/get-orders). This is documentation-backed parsing, not an authenticated account schema capture.

Order identities, requested ticker/status filters, outcome aliases, present action/book-side binding, and exact integral fill/remaining quantities are validated. Initial-count aliases are checked for type and agreement; no conservation equation is inferred without amendment history. Distinct broker orders sharing a client ID remain visible as ambiguity.

Current orders exclude some older finalized/canceled history. Therefore a positive identity match can resolve presence, while no current-list match raises an explicit unqualified-historical-absence error. The existing order manager retains the unresolved intent as `UNAVAILABLE`; repeated empty current listings cannot advance an absence counter or authorize resubmission. A qualified history collector or independently reviewed operator evidence is required to resolve such absence. This is an intentional availability restriction.

`position_manager.py` independently validates the returned list, unique tickers, exact quantities and every stored local row before aggregation. An unknown local state cannot be filtered into an empty portfolio. Startup and periodic uncertainty preserve a halt; only a complete, valid matching position enumeration can clear it. Financial ledger mutation methods are unchanged.

## Verification and reproduction

| Verification | Result |
| --- | --- |
| Exact baseline canonical suite | 954 tests, 0 failures/errors/skips |
| Final candidate canonical suite | 993 tests, 0 failures/errors/skips |
| Added canonical LI-07 tests | 39 test methods, with adversarial subcases |
| Independent semantic witnesses | 39 passed, 0 failed or inconclusive |
| Original false-empty and unread-cursor cases | Reproduced before the fix; now refuse or enumerate correctly |
| Network/provider/broker interaction during these tests | 0 |

The full canonical suite was run using a credential-free environment, disposable `DATA_DIR`, and the audited socket isolation launcher. Baseline and candidate receipts are retained in the integration evidence package. The initial targeted test fixture had an exception-deepcopy setup error; that receipt is retained separately and is not counted as a behavioral success. A stricter test-contract update requires explicit order cursors and expects unresolved historical absence; no tests are disabled or removed.

To reproduce from a disposable checkout with repository dependencies installed:

```sh
python tools/li07_independent_probe.py
python -m unittest discover -s tests -p test_li07_portfolio_completeness.py
python run_tests.py
```

Use an empty credential environment and disposable data directory for the repository suite. The standalone independent harness clears inherited configuration itself, uses synthetic in-memory transports, denies sockets and subprocesses, and constructs neither a broker client nor an engine. It prints source hashes and per-case semantic results. Expected API refusals are distinguished from setup and infrastructure failures.

Changed application files are only `kalshi_client.py` and `position_manager.py`. Test changes are `tests/test_li07_portfolio_completeness.py`, `tests/test_client_order_id_readback.py`, and `tests/test_orders_envelope_schema.py`. The independent harness and this review document complete the patch.

## Remaining production qualification requirements

- Primary-subaccount enumeration is not an inventory of all account subaccounts. Independently verify account identity, subaccount inventory and the intended writer scope before treating production exposure as fully explained.
- Paginated APIs provide no atomic cross-page snapshot or broker freeze here. Concurrent external account changes can invalidate a completed traversal; this patch makes no atomic exposure proof. Real preflight must qualify this limitation.
- Current-order absence is deliberately unresolved without qualified historical coverage. No unresolved intent is deleted to recover availability.
- Reconciliation `MATCH` proves only the validated position comparison. It does not prove open-order completeness, ledger continuity, broker identity, settlement, fills, current risk state, or kill-switch usability. Those require fresh independent production evidence. Other read APIs, including fills, are outside this narrow patch.
- The baseline engine has a separately identified ambiguous-outcome limitation: when an execution result has no order ID, an existing path may release a half-open risk reservation while a pending intent remains. LI-07 does not modify the engine or claim this risk state proven. This requires separate review alongside the LI-06 execution preflight changes before promotion.
- Authentic account response capture, current orders/positions evidence, continuity/risk/kill-switch proof and a naturally occurring READ_ONLY `WOULD_SUBMIT` remain operator/runtime qualification gates. No permissive compatibility fallback is introduced if the real schema differs.

Broker writes, real orders, provider requests, CAPITAL changes, credential changes, main changes, production service changes, deployments and historical ledger rewrites performed by this patch/test task are all **0**. No merge or production deployment is authorized by this review package.
