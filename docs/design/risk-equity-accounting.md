# Risk-equity accounting design (audit finding F2)

Status: **DESIGN — not implemented.** Written for cross-audit before any code.
Scope: `risk_manager.py`, `position_sizer.py`, `execution_engine._balance_gate`
/ `_post_balance_gates`, one new module, one new state file. Nothing in this
document enables CAPITAL trading, touches the model gate, the daily-oracle gate,
`ALLOW_ORDER_SUBMISSION`, broker writes, or any threshold value.

Finding being corrected (`docs/audits/capital-risk-sizing-audit-2026-09-07.md`,
F2): every percentage risk quantity is normalised by **today's broker cash**, so a
deposit un-trips a guard that fired on real losses and a withdrawal trips one
with no losses. Observed in production on 2026-09-07: the same −$0.4839 read as
1222 % drawdown at $0.04 and 4.92 % at $9.84, with no trade in between.

## Core invariant (normative)

> A deposit may increase affordability but must never reduce strategy drawdown
> or clear a loss-derived risk guard. A withdrawal may reduce affordability but
> must never create or deepen strategy drawdown.

Everything below is derived from this sentence. Where the design has to choose
between two readings, it picks the one under which an external cash flow can
only make the engine **more** conservative, never less.

## 0. Vocabulary (six separate quantities)

| name | definition | source | used for |
|---|---|---|---|
| `broker_cash` | live account balance in $ | `KalshiClient.get_balance()` each cycle, as today | affordability only |
| `open_cost_basis` | Σ `count × avg_price / 100` over open positions **plus** Σ `fees` over open trade rows | `PositionManager.open_risk()` + `TradeLogger.open_trades()` | flow inference |
| `account_equity` | `broker_cash + open_cost_basis` (account value at cost) | derived | flow inference, dashboard |
| `realized_trading_pnl` | Σ `net_pnl` over `settled_trades()` (corrections folded, as today) | journal `kalshi_trades.json` | strategy equity |
| `external_flows` | Σ classified flow amounts (deposit > 0, withdrawal < 0) | new ledger | account/strategy reconciliation |
| `strategy_equity` | seed + realized PnL since seed. **Flows never enter it.** | derived from seed + journal | drawdown, throttle, daily stop |
| `risk_equity_reference` | high-water mark of `strategy_equity` | persisted, monotone | drawdown denominator |

`self.capital` keeps its current meaning, **affordability capital** =
`min(configured_capital, broker_cash)`. Per-position $ allocation,
`RISK_BUDGET_PCT`, `MAX_CATEGORY_RISK_PCT`, `MAX_SINGLE_MARKET_RISK_PCT`,
`MAX_PORTFOLIO_RISK_PCT` and the correlation-group cap stay on affordability
capital: they bound exposure against money actually present, and a deposit is
allowed to raise them. Only the **loss-derived** quantities move to
`strategy_equity` / `risk_equity_reference`.

## 1. Data model

New file `equity_ledger.json` under `DATA_DIR`, written through `JsonStore`
(atomic, sha256, backup rotation) and added to `persistence._CRITICAL_BASENAMES`
so a failed write trips `PersistenceSentinel` and blocks new submissions.

```json
{
  "version": 1,
  "seed": {
    "at": "2026-09-07T18:01:19Z",
    "source": "migration_reconstructed | first_observation",
    "account_equity_0": 0.04,
    "realized_pnl_cum_0": -0.4839,
    "strategy_equity_0": 0.04,
    "hwm_0": 0.5239,
    "evidence_sha256": "…",
    "applied_by": "EQUITY_LEDGER_SEED_SHA256"
  },
  "hwm": {"risk_equity_reference": 0.5239, "at": "…"},
  "flows": [
    {"id": "flow-0001", "at": "2026-09-07T18:02:20Z",
     "amount": 9.80, "kind": "deposit",
     "classified_by": "migration | auto | operator",
     "evidence": {"cash_before": 0.04, "cash_after": 9.84,
                  "open_cost_basis_before": 0.0, "open_cost_basis_after": 0.0,
                  "realized_pnl_delta": 0.0, "cycles_observed": 2,
                  "reconcile_status": "MATCH", "in_flight_orders": 0},
     "note": ""}
  ],
  "pending": null,
  "daily": {"date": "2026-09-07", "sod_strategy_equity": 0.04}
}
```

- `flows[].kind` ∈ `deposit | withdrawal | rounding | unclassified`. Amounts are
  signed. `unclassified` rows have `amount` set and are the **only** rows that
  can be edited, and only by the operator mechanism in §3; every other row is
  append-only.
- `pending` holds the residual currently under observation (§5):
  `{"residual": r, "first_seen_cycle": n, "consecutive": k, "first_seen_at": ts}`.
- `hwm` is persisted **and** recomputable; on load the engine takes
  `max(persisted, recomputed)` (§4), so a crash can never lower it.
- Nothing in the trade journal changes. `kalshi_trades.json` remains the single
  source of truth for PnL; this file only adds the seed, the flows and the mark.

## 2. Migration strategy for the existing `/data/state5`

Facts the migration must reproduce (from the audit, all read from durable
evidence and logs, not from memory):

| fact | value | source |
|---|---|---|
| last observation before the jump | cash $0.04, open positions 0, 18:01:19Z, cycle 264 | `[CAPITAL] solde=0.04$`, `positions_state.json` |
| first observation after the jump | cash $9.84, 18:02:20Z, no trade between | `[CAPITAL] solde=9.84$` |
| rolling drawdown in dollars | $0.4839 | `RiskManager.rolling_drawdown()` on the journal |
| realized PnL cumulative | to be computed on the volume | Σ `net_pnl` over `settled_trades()` |

Seed rule (reconstruction, conservative):

```
strategy_equity_now = account_equity at the last observation BEFORE the first
                      unexplained jump                          = 0.04 + 0.00
hwm_0               = strategy_equity_now + rolling_drawdown()  = 0.04 + 0.4839 = 0.5239
strategy_equity_0   = strategy_equity_now                        = 0.04
realized_pnl_cum_0  = realized_pnl_cum(now)   (journal sum at seed time; §6 subtracts it)
flow-0001           = 9.84 − 0.04 − 0.00 = +9.80, kind=deposit, classified_by=migration
```

Resulting state after migration: `drawdown_pct = 100 × 0.4839 / 0.5239 =
92.36 %` → `equity_drawdown` **stays blocked** in CAPITAL. This is the invariant
doing exactly what it says: the deposit made $9.84 affordable and cleared
nothing. The size throttle also stays halved. READ_ONLY observation is
unaffected (PR #64 records `would_block_capital=equity_drawdown`).

Why this seed and not another:

- Seeding from **today's** cash ($9.84) would silently accept the deposit as
  the reference and reset the loss to 4.9 % — that is the defect, restated.
- Seeding from the **pre-jump** account equity is the smallest defensible
  denominator that the durable evidence supports; a smaller one cannot be
  justified, and any larger one relaxes the guard without trading evidence.
- If cycle evidence shows **earlier** unexplained jumps, the same rule is
  applied at the earliest one and every later jump becomes a migration flow.
  Historical flows are never inferred from memory; each needs two adjacent
  observations in `cycles.jsonl` / `dashboard_state.json` / logs.

Mechanism (mirrors `RESTORE_STATE_SHA256` and `LEDGER_CORRECTION_APPLY_IDS`):

1. `tools/equity_ledger_seed.py --dry-run` runs on the volume, reads only the
   journal, `positions_state.json` and the cycle evidence, prints the proposed
   seed JSON and its sha256, and writes **nothing**.
2. The operator sets `EQUITY_LEDGER_SEED_SHA256=<that hash>`. At the next boot
   the engine recomputes the proposal; it is applied only if the hash matches
   byte for byte, once, and only when `equity_ledger.json` has no `seed`.
   A mismatch or an existing seed is a logged no-op (never an overwrite).
3. A deploy of the code with the variable unset seeds nothing and, under
   `REQUIRE_PERSISTENT_STATE=true`, raises the fail-closed guard
   `equity_ledger_unseeded` (CAPITAL-blocking; READ_ONLY keeps observing, see
   open decision D1).

No trade row, no risk row and no timestamp is rewritten. The old
`rolling_drawdown()` in dollars is unchanged and equal to `hwm − strategy_equity`
by construction (§6), which is the migration's self-check.

## 3. Unknown historical deposits and withdrawals

Kalshi's public API used by this engine exposes balance, positions, orders and
fills, not a cash-flow statement. Flows are therefore **inferred** from the
accounting identity and classified by an asymmetric rule chosen so that a wrong
guess can only be conservative:

| residual `r = account_equity − expected` | auto classification | why it is safe |
|---|---|---|
| `r > ε`, stable (§5) | `deposit` | strategy equity does not rise, so drawdown is not reduced; if it was actually an unrecorded trading gain, we have understated equity, the conservative error |
| `−ε ≤ r ≤ ε` | `rounding` (bounded, see ε in §5) | too small to move any guard; recorded so the anchor stays exact |
| `r < −ε`, stable | **`unclassified`** — never auto-labelled `withdrawal` | a negative residual is either a withdrawal (harmless to risk) or an unrecorded trading loss (must count). The engine cannot tell, so it fails closed: guard `equity_flow_unresolved` blocks CAPITAL, and for throttle/guard arithmetic the residual is **treated as a loss** (`strategy_equity_conservative = strategy_equity + r`) until resolved |

Resolution of an `unclassified` row is operator-only and declarative:
`EQUITY_FLOW_CLASSIFY="flow-0007=withdrawal"` (or `=loss`). `withdrawal` moves
the amount into `external_flows`; `loss` requires a matching ledger correction
(`ledger_corrections.py`) so that the journal, not this file, carries the PnL.
The row keeps `classified_by=operator`, the raw evidence and both timestamps.
Classification is idempotent and refuses unknown ids, already-classified rows and
rows whose amount differs from the persisted one.

Historical flows **before** the seed are by definition inside the seed
(`account_equity_0`) and are not reconstructed individually; the seed's evidence
block records the pre-seed realized PnL so nothing is lost for audit.

## 4. Crash / restart behaviour

- **Source of truth order.** `strategy_equity` is never trusted from disk; it is
  recomputed at every load as `seed.strategy_equity_0 + (realized_pnl_cum −
  seed.realized_pnl_cum_0)` from the journal. Only `seed`, `flows`, `hwm`,
  `pending` and `daily` are persisted.
- **Write ordering per cycle.** journal flush (`TradeLogger.flush`) → risk state
  → equity ledger. A crash between the first and the last leaves the ledger one
  step stale; the next load recomputes equity from the journal and re-derives the
  residual, so nothing is double-counted. The `pending` counter restarts from 0
  after a restart (delays classification by K cycles; never skips it).
- **HWM monotonicity.** On load: `hwm = max(persisted hwm, max over journal
  prefixes of strategy_equity_0 + (cumsum_prefix − realized_pnl_cum_0))`; on
  the state5 seed this recomputes exactly `hwm_0 = 0.5239`. Persisted-lower-than-recomputed is repaired upward
  and logged; persisted-higher is kept (it may come from a settlement the journal
  saw but a later restore did not). The mark can only fall through an explicit
  operator rebase (§6, `kind=rebase`), never through a crash, a restore or a
  deposit.
- **Missing file.** With `REQUIRE_PERSISTENT_STATE=true` and no seed:
  `equity_ledger_unseeded` (fail-closed, CAPITAL). Without that flag (DEMO):
  first-observation seed, `source=first_observation`, logged loudly.
- **Persistence failure.** The basename is critical: a failed save trips
  `PersistenceSentinel` and `_post_balance_gates` already returns
  `persistence_failure` before any risk gate.
- **Restore.** `state_restore._STATE_FILES` gains `equity_ledger.json`; the
  never-overwrite rule applies unchanged. A restore of a journal newer than the
  ledger is handled by the recompute-on-load rule above.
- **Day roll.** `daily.sod_strategy_equity` is captured at the first cycle of
  each UTC day from `strategy_equity` (not from cash). A restart inside the day
  keeps the persisted value; a restart on a new day captures a fresh one.

## 5. Reconciliation algorithm (every cycle, after `_balance_gate`)

```
inputs : cash          = broker_cash (None → skip, no state change)
         basis         = posmgr.open_risk() + Σ fees over tlog.open_trades()
         pnl_cum       = Σ net_pnl over tlog.settled_trades()
         flows_cum     = Σ amount over flows where kind ∈ {deposit, withdrawal, rounding}
         eq_strategy   = seed.strategy_equity_0 + (pnl_cum − seed.realized_pnl_cum_0)
         expected      = seed.account_equity_0 + (pnl_cum − seed.realized_pnl_cum_0) + flows_cum
         r             = (cash + basis) − expected
         ε             = 0.01 + 0.005 × settled_since_last_anchor        (journal rounds net_pnl to 2 dp; broker reports 4 dp)

quiet   = orders_state has no in-flight order
          AND posmgr.reconcile_halt is None (last verify = MATCH)
          AND no settlement was recorded in this cycle
          AND balance was fetched in this cycle (not a cache hit)

if |r| ≤ ε:
    if r ≠ 0: append flow(kind=rounding, amount=r, classified_by=auto)   # re-anchor exactly
    pending = None
elif not quiet:
    keep pending as is (a settlement race or an in-flight order can explain r)
elif pending is None or |pending.residual − r| > ε:
    pending = {residual: r, first_seen_cycle: n, consecutive: 1}
else:
    pending.consecutive += 1
    if pending.consecutive ≥ K (=3 cycles ≈ 3 minutes at the current cadence):
        if r > 0:  append flow(kind=deposit,      amount=r, classified_by=auto, evidence=…)
        else:      append flow(kind=unclassified, amount=r, classified_by=None, evidence=…)
        pending = None

hwm = max(hwm, eq_strategy);  persist
```

Guard outputs, evaluated in `_post_balance_gates` **after** the existing
`equity_drawdown` check so that the order of the existing guards is unchanged:

| guard | condition | mode it blocks |
|---|---|---|
| `equity_ledger_unseeded` | no seed and `REQUIRE_PERSISTENT_STATE` | CAPITAL |
| `equity_flow_unresolved` | any flow with `kind=unclassified` | CAPITAL |

Neither guard is ever cleared by a balance change; only a seed application or an
operator classification clears them.

Why "stable for K quiet cycles" and not immediately: the broker credits a
settlement before the engine's settlement sweep writes `settled_at`, and a fill
observed after the balance fetch lowers cash before the position exists locally.
Both produce a transient residual that vanishes within one or two cycles;
insisting on K quiet cycles with no settlement, no in-flight order and a MATCH
reconciliation makes a transient physically unable to become a flow.

## 6. Exact drawdown formula

```
realized_pnl_cum(t)      = Σ net_pnl over settled_trades()          (fold_corrections applied)
strategy_equity(t)       = seed.strategy_equity_0 + realized_pnl_cum(t) − seed.realized_pnl_cum_0
risk_equity_reference(t) = max(risk_equity_reference(t−1), strategy_equity(t)),   reference(0) = seed.hwm_0
drawdown_usd(t)          = max(0, risk_equity_reference(t) − strategy_equity_conservative(t))
                           where strategy_equity_conservative = strategy_equity + min(0, pending/unclassified residuals)
drawdown_pct(t)          = 100 × drawdown_usd(t) / risk_equity_reference(t)      if reference > 0
                         = 100 if drawdown_usd > 0 else 0                           otherwise
```

Consumers:

- `_post_balance_gates`: `drawdown_pct ≥ MAX_EQUITY_DRAWDOWN_PCT` → `equity_drawdown` (threshold value unchanged, 20 %).
- `PositionSizer._legacy` / Kelly path: the throttle test becomes
  `drawdown_pct ≥ DD_THROTTLE_PCT` with `drawdown_pct` passed in by the caller
  (signature gains `drawdown_pct=None`; when `None` the old `drawdown/capital`
  expression is used so existing callers and tests are byte-identical until
  switched). Threshold unchanged, 10 %.
- `RiskManager.drawdown_size_factor()`: same `drawdown_pct`.
- `RiskManager.rolling_drawdown()` (dollars) is **kept** and asserted equal to
  `hwm − strategy_equity` in tests; it is the migration self-check.

Identity check with today's code: with `seed.strategy_equity_0 = 0`,
`seed.realized_pnl_cum_0 = 0`, `hwm_0 = 0` and no flows, `drawdown_usd` equals
the existing peak-to-trough of the cumulative curve exactly. The only change is
the denominator.

**Recovery is explicit, never automatic.** Under a high-water mark, a blown
drawdown can only shrink through trading profit, and trading is blocked by the
guard. The engine must not resolve this by time-decay (a rolling window would be
"reset historical losses", which the mission forbids) nor by deposit (the
invariant). Recovery is an operator **rebase**: `EQUITY_LEDGER_REBASE_SHA256`
over a dry-run proposal that sets `risk_equity_reference := strategy_equity` and
appends `{"kind": "rebase", "from": hwm, "to": equity, "drawdown_usd_acknowledged": …}`.
It is an audit row forever, requires a fresh proposal hash each time, and is a
separate operator decision from funding the account.

## 7. Daily-stop reference

```
stop_raw             = min(MAX_DAILY_LOSS,
                           MAX_DAILY_LOSS_PCT/100 × daily.sod_strategy_equity,
                           MAX_DAILY_LOSS_PCT/100 × broker_cash)
effective_daily_stop = max(0.01, round(stop_raw, 2))   if stop_raw > 0
                     = 0.0                              only if MAX_DAILY_LOSS == 0 (operator-disabled, as today)
```

- **`0` means disabled** in `can_trade` / `portfolio_check` (`if stop > 0 and
  pnl <= -stop`). Rounding a tiny positive reference down to `0.00` would
  therefore switch the stop **off**, the opposite of conservative. The `0.01`
  floor (one cent, the smallest realizable loss) is mandatory and pinned by T19.

- The strategy term stops an intraday deposit from widening the stop.
- The cash term is kept so the stop can never exceed what today's code allows
  (a withdrawal tightens, a deposit does not loosen). The new value is therefore
  always `≤` the current one, for every input.
- `daily_realized_pnl` (numerator) is unchanged: settled `net_pnl` today.
- Consequence at today's numbers after migration: `min(50, 5 % × 0.04, 5 % × 9.84)
  = $0.002` → floored to `$0.01` → any realized loss today halts. That is the honest reading of a
  stake that has lost 92 % of its reference; the way out is the rebase in §6,
  not the denominator. Percent thresholds are not changed by this design.

## 8. Test matrix

All tests drive real `RiskManager` / `PositionSizer` / `ExecutionEngine` methods
on an isolated `DATA_DIR` (`shadow_iso._IsolatedState`) and read the durable
file back. No threshold is patched. Expected values are computed from §6/§7, not
from the current code.

| # | scenario | expected |
|---|---|---|
| T1 | journal fixed at −$0.4839, cash 0.04 → 9.84 in one cycle, quiet | `drawdown_pct` unchanged (92.36 %); `equity_drawdown` still blocks; flow `deposit +9.80 auto` appended after K cycles; `self.capital` = 9.84 |
| T2 | same, cash 9.84 → 2.00 (withdrawal) | `drawdown_pct` unchanged; flow `unclassified −7.84`; `equity_flow_unresolved` blocks; conservative equity used; `self.capital` = 2.00 |
| T3 | T2 then `EQUITY_FLOW_CLASSIFY=flow-…=withdrawal` | guard clears; drawdown unchanged; row keeps evidence, `classified_by=operator` |
| T4 | T2 then classify as `loss` without a ledger correction | refused, guard stays |
| T5 | residual appears with an in-flight order / a settlement in the same cycle | `pending` not advanced; no flow row |
| T6 | transient residual that disappears next cycle | no flow row, `pending` cleared |
| T7 | property test over random sequences of (pnl, flow) events | `drawdown_pct(with flows) == drawdown_pct(same pnl, no flows)`; `hwm` non-decreasing; `daily_stop_new ≤ daily_stop_old` |
| T8 | migration dry-run on a copy of the state5 fixture (journal + positions + cycles) | proposal = §2 numbers; sha256 stable across two runs; zero writes |
| T9 | seed apply with wrong hash / existing seed | no-op, logged; `equity_ledger_unseeded` if REQUIRE_PERSISTENT_STATE |
| T10 | crash between journal flush and ledger save (simulate by saving journal only) | after reload equity recomputed from journal, no double count, `hwm` unchanged |
| T11 | persisted `hwm` lower than journal implies | repaired upward on load; persisted higher is kept |
| T12 | day roll: deposit at 18:02, stop computed at 18:03 | stop uses `sod_strategy_equity`, not 9.84 |
| T13 | `rolling_drawdown()` (dollars) == `hwm − strategy_equity` for 200 random journals | equal to 1e-9 |
| T14 | `_legacy(..., drawdown_pct=None)` | byte-identical to current sizing for the audit's capital × price matrix |
| T15 | rebase with fresh hash | reference := equity, guard clears, audit row present; second use of the same hash refused |
| T16 | READ_ONLY with every trading flag armed, guard `equity_flow_unresolved` active | no call reaches `_assert_broker_write_allowed`; `would_block_capital` carries the guard (PR #64 wrapper) |
| T17 | `equity_ledger.json` save fails | `PersistenceSentinel` tripped, `persistence_failure` first |
| T19 | reference 0.04, cash 9.84 | stop = 0.01, not 0.00; `can_trade` still enforces it |
| T18 | both runners (`run_tests.py`, `pytest`) + Docker build + gatekeeper artifacts | green; `model_validation.json` still refuses live |

Existing tests that encode the cash denominator and must be re-derived (not
weakened): `tests/test_sizing_small_account.py`, `tests/test_daily_quarantine.py`,
`tests/test_sixth_contract_correction.py`, `tests/test_periodic_reconciliation.py`,
`tests/test_shadow_write_layer_isolation.py`. Each changed expectation must cite
the §6/§7 line that produces it.

## 9. Rollback plan

- **Switch.** `RISK_EQUITY_MODE=cash|strategy`. Phase A ships with default
  `cash`: the ledger is computed, persisted and logged every cycle (`[EQUITY]
  strategy_equity=… hwm=… dd_pct_strategy=… dd_pct_cash=… residual=…`) but every
  guard, throttle and stop still reads the cash formula — byte-identical
  behaviour, evidence accumulates on READ_ONLY production where nothing can be
  submitted. Phase B flips the default to `strategy` in a separate PR after the
  shadow evidence has been cross-audited. Rollback at any time is
  `RISK_EQUITY_MODE=cash`, no deploy required.
- **Data.** The file is additive under a new basename; the previous code
  ignores it; the journal is untouched. Rolling back the code loses nothing and
  rolling forward again resumes from the persisted seed and flows.
- **Code.** One new module (`equity_ledger.py`), one new tool, and small call
  sites in `risk_manager.py`, `position_sizer.py`, `execution_engine.py`; a
  `git revert` of the PR is clean.
- **Never rolled back by rollback:** an operator rebase or classification row
  stays in the file; if the code is reverted and later restored, those decisions
  still apply.

## Open decisions for cross-audit

- **D1.** Should PR #64's READ_ONLY observation also pass through
  `equity_ledger_unseeded` / `equity_flow_unresolved`, or should those two guards
  stop the READ_ONLY scan until resolved? The wrapper today relaxes exactly one
  guard by design. Recommendation: treat both like `equity_drawdown` (observe,
  record `would_block_capital`), because neither can cause a write; but this
  widens the wrapper's allow-list from one name to three and needs a reviewer
  who did not author it.
- **D2.** `K = 3` quiet cycles and `ε = 0.01 + 0.005 × n` are proposals; both
  should be pinned by tests T5/T6 with the chosen values.
- **D3.** Phase B default flip is a policy change with a visible effect on
  today's production account (guard stays blocked at 92 % until a rebase). It
  must not ride in with Phase A.

## What this design does not do

- Does not change `MAX_POSITION_PCT`, `RISK_BUDGET_PCT`, `DD_THROTTLE_PCT`,
  `MAX_EQUITY_DRAWDOWN_PCT`, `MAX_DAILY_LOSS`, `MAX_DAILY_LOSS_PCT`.
- Does not make `size=0` at $9.84 into a non-zero size; that result is policy.
- Does not reset, rewrite, time-decay or archive any historical loss.
- Does not touch the model gate, the daily-oracle gate, `ALLOW_ORDER_SUBMISSION`,
  `LIVE_BROKER_WRITES_AUTHORIZED`, PROD access mode, or Railway configuration.
