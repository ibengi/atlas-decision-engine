# Risk-equity ledger: operator procedure

Implements `docs/design/risk-equity-accounting.md` (rev 2). State file:
`DATA_DIR/equity_ledger.json` (critical basename: a failed write trips the
persistence sentinel). Module: `equity_ledger.py`. Dry-run tool:
`tools/equity_ledger_tool.py` (reads the journal and positions, opens no
network connection, writes nothing).

Every operator action is declarative: the tool prints a proposal and its
hash or token, the operator sets the variables, and the engine recomputes
the proposal at the next boot and applies it only on an exact match. Any
change to the journal, the ledger or the positions in between changes the
hash and the boot refuses with a logged no-op. Nothing is ever edited or
deleted; every applied action is an append-only row.

## Read the state

```
DATA_DIR=/data/state5 python tools/equity_ledger_tool.py status
```

Fields: `risk_equity_status` (RECONCILED / CONSERVATIVE_ESTIMATE /
UNRECONCILED), `strategy_equity`, `risk_equity_reference` (high-water mark),
`drawdown_pct`, `external_flows_cum`, `unclassified_flows`, `capital_hold`,
`capital_guards`, `capital_eligible`, `unproven`.

The same snapshot is in every cycle's evidence row (`risk_equity_status`),
`cycle_report.json`, `dashboard_state.json`, the health payload
(`risk_equity`) and the startup log line `[EQUITY] ...`.

## Seed (one-time migration of an existing volume)

The seed is the last durable pre-flow observation: cash before the first
unexplained jump. It is a **conservative estimate**, never reconciled truth.

```
DATA_DIR=/data/state5 python tools/equity_ledger_tool.py seed \
  --pre-flow-cash 0.04 --pre-flow-at 2026-09-07T18:01:19Z \
  --evidence "railway deploy 471b446f 18:01:19Z [CAPITAL] solde=0.04$" \
  --cash-now 9.84
```

Set `EQUITY_LEDGER_SEED_PRE_FLOW_CASH`, `EQUITY_LEDGER_SEED_PRE_FLOW_AT`,
`EQUITY_LEDGER_SEED_EVIDENCE` and `EQUITY_LEDGER_SEED_SHA256` from the
printed `set_to_apply`, restart. The boot logs `[EQUITY_SEED] proposal
sha256=...` and applies only if the hashes match. Result:
`risk_equity_status=CONSERVATIVE_ESTIMATE`, `unproven=["funding before
<pre_flow_at>"]`, the jump recorded as `flow-0001 kind=deposit
classified_by=migration`, drawdown against the pre-flow stake. A seed is
never overwritten; remove the variables after the boot that applied it.

Refused (UNRECONCILED proposal) when settlements exist after the pre-flow
time, positions are open, or the residual is negative.

## Flows

Positive residuals stable for `EQUITY_FLOW_QUIET_CYCLES` (3) quiet cycles
become `deposit` automatically (they never enter strategy equity). Negative
residuals become `unclassified` and block CAPITAL (`equity_flow_unresolved`)
until an operator classifies them:

```
EQUITY_FLOW_CLASSIFY="flow-0002=withdrawal"       # a real withdrawal
EQUITY_FLOW_CLASSIFY="flow-0002=loss:<correction_id>"   # a loss, carried by a ledger correction already in the journal
EQUITY_FLOW_CLASSIFY_ACTION_ID=OPS-12
```

Unknown ids, already-classified rows, positive amounts labelled withdrawal,
and `loss` without a matching correction are refused.

The very first positive flow of an account that has never had a stake
(`risk_equity_reference == 0`) is the initial stake and is folded into the
seed (`kind=initial_stake`); with any positive reference a deposit is a
deposit.

## Journal evidence watermark

On every save the ledger records the most trading history it has ever
evidenced (`journal_watermark`: settled row count, an order-preserving
digest of the settled rows, the strategy equity at that point). It only
ever grows. On every load and every observation the current journal is
checked against it: a journal that is shorter, older, or replaced (same
length, different rows) is a **journal mismatch**:

- `risk_equity_status=UNRECONCILED`, CAPITAL blocked (`risk_equity_unreconciled`);
- the high-water mark is kept, and the drawdown is bounded by the lowest
  evidenced strategy equity, so a loss cannot vanish because the journal
  shrank;
- the watermark is not lowered to the shorter journal;
- attestation and rebase are refused while the mismatch stands;
- the mismatch clears only when the evidenced history is back in the
  journal (a longer journal that keeps the evidenced prefix is fine).

`state_restore` never writes `equity_ledger.json`, so a volume restore
that brings back an older journal is detected at the next boot.

## Rebase (rare, exceptional)

Preconditions, all checked at apply time: `RECONCILED`, no journal
mismatch, `equity_drawdown` actually firing, no unclassified flow, no
pending residual, a fresh position verification against the broker that
returns MATCH, no open position, **no live order of any kind**, no open
`capital_hold`.

The order check reads the authoritative state the execution path uses
(`execution_engine.equity_rebase_context`): the persisted OrderManager
state (`open_orders`, including partially filled and cancel-unconfirmed
orders, `pending_intents`, `resolution_halt`) **and** a fresh read-only
broker order listing. An order open on either side, a broker listing that
fails (unknown), or a disagreement between the local and broker order sets
refuses the rebase. A refusal changes nothing: no HWM mutation, token not
consumed, the file byte-identical, the reason logged as
`[EQUITY_REBASE] refused: [...]`.

```
DATA_DIR=/data/state5 python tools/equity_ledger_tool.py rebase \
  --reason "Q3 losses acknowledged; see incident 2026-09-xx" --action-id OPS-20
```

Set `EQUITY_LEDGER_REBASE_REASON`, `EQUITY_LEDGER_REBASE_ACTION_ID`,
`EQUITY_LEDGER_REBASE_TOKEN`, restart. Effect: an append-only `rebases`
row (reason, action id, old and new baseline, evidence hash, timestamp,
token hash, prior HWM row), `risk_equity_reference := strategy_equity`, the
token consumed forever, and **`capital_hold` set: CAPITAL stays blocked
(`capital_hold_post_rebase`) until an independent second action releases
it**. A rebase never makes CAPITAL eligible by itself.

## Hold release (independent validation)

```
DATA_DIR=/data/state5 python tools/equity_ledger_tool.py hold-release \
  --action-id OPS-22 --validation "review doc sha256:..."
```

The action id must differ from the rebase's. Set
`EQUITY_LEDGER_HOLD_RELEASE_ACTION_ID`, `EQUITY_LEDGER_HOLD_RELEASE_VALIDATION`,
`EQUITY_LEDGER_HOLD_RELEASE_TOKEN`, restart. Single-use token.

## Attest RECONCILED

Only when the account's funding history is fully documented:

```
DATA_DIR=/data/state5 python tools/equity_ledger_tool.py attest \
  --action-id OPS-30 --funding-records-sha256 <sha256 of the records>
```

Refused while a flow is unclassified, a residual is pending, or the seed
prefix is not intact.

## Rollback

`RISK_EQUITY_MODE=cash` restores the historical cash-denominated
percentages without a code change; the ledger keeps being computed and
persisted. The file is additive; older code ignores it.

## CAPITAL policy

`CONSERVATIVE_ESTIMATE` blocks CAPITAL by default. The separate policy
`RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE=1` lifts only that guard; it is
never set by migration or by code, and `UNRECONCILED` has no override.
`capital_eligible` in any surface is a report field, not an authorization:
every existing gate (`PROD_ACCESS_MODE=CAPITAL`, `LIVE_TRADING`,
`LIVE_TRADING_CONFIRMED`, the model gatekeeper,
`LIVE_BROKER_WRITES_AUTHORIZED`) still applies.

## Restore

`state_restore` keeps its fixed five-file manifest; `equity_ledger.json`
is not part of it. A restore without the ledger boots unseeded
(`equity_ledger_unseeded`, CAPITAL blocked, READ_ONLY keeps observing) and
is re-seeded by the procedure above. A restored journal older than the
ledger is detected on load (`UNRECONCILED`, seed prefix missing) and the
high-water mark is kept.
