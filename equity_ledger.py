"""Risk-equity accounting (audit finding F2, design rev 2).

Separates six quantities the engine used to conflate into one number
("capital" = live broker cash):

  broker_cash            live balance, affordability only (unchanged callers)
  open_cost_basis        cost + fees of open positions
  account_equity         broker_cash + open_cost_basis
  realized_trading_pnl   sum of settled net_pnl from the journal (single truth)
  external_flows         deposits / withdrawals, inferred and classified
  strategy_equity        seed + realized PnL since the seed. FLOWS NEVER ENTER IT.
  risk_equity_reference  high-water mark of strategy_equity (monotone, rebase-only)

Core invariant: a deposit may raise affordability but can never reduce
strategy drawdown or clear a loss-derived guard; a withdrawal can never
create or deepen strategy drawdown.

Everything here is fail-closed for CAPITAL and observation-only for
PRODUCTION READ_ONLY. Nothing here submits, cancels or touches a broker:
the module reads the journal, the positions and one balance number, and
writes one state file under DATA_DIR through JsonStore.

Baseline provenance (`risk_equity_status`):
  RECONCILED            full provenance; CAPITAL eligible subject to every other gate
  CONSERVATIVE_ESTIMATE conservative reconstruction, unproven funding history;
                        CAPITAL blocked unless RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE
  UNRECONCILED          cannot be established safely; CAPITAL blocked, no override

Operator actions are declarative, hash-bound and single-use:
  EQUITY_LEDGER_SEED_PRE_FLOW_CASH / _PRE_FLOW_AT / _EVIDENCE + _SHA256   seed (migration)
  EQUITY_FLOW_CLASSIFY="flow-0002=withdrawal[:action_id]"                   classification
  EQUITY_LEDGER_REBASE_REASON / _ACTION_ID / _TOKEN                          rebase
  EQUITY_LEDGER_HOLD_RELEASE_ACTION_ID / _VALIDATION / _TOKEN                hold release
  EQUITY_LEDGER_ATTEST_ACTION_ID / _FUNDING_RECORDS_SHA256 / _TOKEN          attest RECONCILED
A dry-run (`tools/equity_ledger_tool.py`) prints every proposal and its hash
or token; the engine recomputes at boot and applies only on an exact match.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from config import CFG, _p
from persistence import JsonStore
from trade_logger import now_iso

log = logging.getLogger("EQUITY")

STATUS_RECONCILED = "RECONCILED"
STATUS_CONSERVATIVE = "CONSERVATIVE_ESTIMATE"
STATUS_UNRECONCILED = "UNRECONCILED"
STATUSES = (STATUS_RECONCILED, STATUS_CONSERVATIVE, STATUS_UNRECONCILED)

FLOW_DEPOSIT = "deposit"
FLOW_WITHDRAWAL = "withdrawal"
FLOW_ROUNDING = "rounding"
FLOW_UNCLASSIFIED = "unclassified"
FLOW_KINDS_COUNTED = (FLOW_DEPOSIT, FLOW_WITHDRAWAL, FLOW_ROUNDING)

GUARD_UNSEEDED = "equity_ledger_unseeded"
GUARD_FLOW_UNRESOLVED = "equity_flow_unresolved"
GUARD_UNRECONCILED = "risk_equity_unreconciled"
GUARD_CAPITAL_HOLD = "capital_hold_post_rebase"
ACCOUNTING_GUARDS = (GUARD_UNSEEDED, GUARD_FLOW_UNRESOLVED,
                     GUARD_UNRECONCILED, GUARD_CAPITAL_HOLD)

LEDGER_FILE = "equity_ledger.json"
SCHEMA_VERSION = 1


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round(x) -> float:
    return round(float(x) + 0.0, 4)


def journal_digest(rows) -> str:
    """Order-preserving fingerprint of settled rows: identity, PnL, time."""
    return _sha(_canonical([(t.get("trade_id"), _round(t.get("net_pnl") or 0.0),
                             t.get("settled_at")) for t in rows]))


class EquityLedger:
    """Persisted risk-equity state under DATA_DIR/equity_ledger.json."""

    def __init__(self, tlog, posmgr, env: str = "prod", path: str = None):
        self.tlog = tlog
        self.posmgr = posmgr
        self.env = env
        self.path = path or _p(LEDGER_FILE)
        self.k_quiet_cycles = max(1, int(getattr(CFG, "EQUITY_FLOW_QUIET_CYCLES", 3)))
        self.eps_base = float(getattr(CFG, "EQUITY_FLOW_EPS", 0.01))
        self.eps_per_trade = float(getattr(CFG, "EQUITY_FLOW_EPS_PER_TRADE", 0.005))
        self.state = self._load()
        self._last_obs = None          # (settled_count, open_count)
        self._reconcile_on_load()

    # ── persistence ─────────────────────────────────────────────────────
    def _empty(self) -> dict:
        return {"version": SCHEMA_VERSION, "seed": None,
                "risk_equity_status": STATUS_UNRECONCILED,
                "status_basis": {"set_at": None, "set_by": None,
                                 "provenance_window": {"from": None, "to": None},
                                 "unproven": ["no seed"], "evidence_sha256": None},
                "capital_hold": None,
                "hwm": {"risk_equity_reference": None, "at": None,
                        "rebased_from": None, "floor_from_settled_index": 0},
                "rebases": [], "consumed_tokens": [], "flows": [],
                "pending": None, "daily": {"date": None, "sod_strategy_equity": None},
                "anchor": {"settled_since_anchor": 0},
                # the most trading history ever evidenced by this ledger:
                # a journal that no longer contains it is a mismatch
                "journal_watermark": None, "journal_mismatch": None}

    def _load(self) -> dict:
        raw = JsonStore.load(self.path, None)
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            return self._empty()
        base = self._empty()
        base.update(raw)
        return base

    def save(self) -> bool:
        self._advance_journal_watermark()
        ok = JsonStore.save(self.path, self.state)
        if not ok:
            log.error("[EQUITY] equity_ledger.json NOT saved (persistence sentinel tripped)")
        return ok

    # ── journal-derived quantities (never trusted from disk) ────────────
    def settled(self) -> list:
        return list(self.tlog.settled_trades())

    def realized_pnl_cum(self) -> float:
        return float(sum(t.get("net_pnl") or 0.0 for t in self.settled()))

    def open_cost_basis(self) -> float:
        basis = float(self.posmgr.open_risk()) if self.posmgr is not None else 0.0
        fees = 0.0
        try:
            fees = float(sum(float(t.get("fees") or 0.0) for t in self.tlog.open_trades()))
        except Exception:  # noqa: BLE001 - a journal without open rows
            fees = 0.0
        return basis + fees

    def rolling_drawdown_journal(self) -> float:
        curve, peak = 0.0, 0.0
        for t in self.settled():
            curve += float(t.get("net_pnl") or 0.0)
            peak = max(peak, curve)
        return max(0.0, peak - curve)

    @property
    def seeded(self) -> bool:
        return isinstance(self.state.get("seed"), dict) and \
            self.state["seed"].get("strategy_equity_0") is not None

    def strategy_equity(self):
        if not self.seeded:
            return None
        s = self.state["seed"]
        return float(s["strategy_equity_0"]) + (self.realized_pnl_cum()
                                                - float(s["realized_pnl_cum_0"]))

    def strategy_equity_conservative(self):
        eq = self.strategy_equity()
        if eq is None:
            return None
        pend = self.state.get("pending") or {}
        r = float(pend.get("residual") or 0.0)
        unresolved = sum(float(f["amount"]) for f in self.state["flows"]
                         if f.get("kind") == FLOW_UNCLASSIFIED)
        eq = eq + min(0.0, r) + min(0.0, unresolved)
        wm = self.state.get("journal_watermark") or {}
        if self.state.get("journal_mismatch") and wm.get("strategy_equity") is not None:
            # evidenced losses never disappear because the journal shrank:
            # the lowest evidenced equity bounds the drawdown from above
            eq = min(eq, float(wm["strategy_equity"]))
        return eq

    def flows_cum(self) -> float:
        return float(sum(float(f["amount"]) for f in self.state["flows"]
                         if f.get("kind") in FLOW_KINDS_COUNTED))

    def unclassified_flows(self) -> list:
        return [f for f in self.state["flows"] if f.get("kind") == FLOW_UNCLASSIFIED]

    # ── high-water mark: persisted, recomputable, monotone ──────────────
    def _recomputed_hwm(self):
        if not self.seeded:
            return None
        s = self.state["seed"]
        rows = self.settled()
        start = int(self.state["hwm"].get("floor_from_settled_index") or 0)
        start = max(start, int(s.get("settled_count_0") or 0))
        base = float(s["strategy_equity_0"]) - float(s["realized_pnl_cum_0"])
        cum = 0.0
        best = None
        for i, t in enumerate(rows):
            cum += float(t.get("net_pnl") or 0.0)
            if i + 1 >= start:
                v = base + cum
                best = v if best is None else max(best, v)
        eq = self.strategy_equity()
        best = eq if best is None else max(best, eq)
        if self.state["hwm"].get("rebased_from") is None:
            best = max(best, float(s["hwm_0"]))
        return best

    def risk_equity_reference(self):
        if not self.seeded:
            return None
        persisted = self.state["hwm"].get("risk_equity_reference")
        rec = self._recomputed_hwm()
        if persisted is None:
            return rec
        return max(float(persisted), rec)

    def _refresh_hwm(self, when: str = None) -> bool:
        """Raise the persisted mark to the journal-implied one; never lower it.
        Returns True when the persisted value changed."""
        if not self.seeded:
            return False
        ref = self.risk_equity_reference()
        old = self.state["hwm"].get("risk_equity_reference")
        if old is None or ref > float(old) + 1e-12:
            if old is not None:
                log.warning(f"[EQUITY] persisted HWM {float(old):.4f} below journal-implied "
                            f"{ref:.4f}: repaired upward (never downward)")
            self.state["hwm"]["risk_equity_reference"] = _round(ref)
            self.state["hwm"]["at"] = when or now_iso()
            return True
        return False

    # ── drawdown ────────────────────────────────────────────────────────
    def drawdown_usd(self):
        if not self.seeded:
            return None
        ref = self.risk_equity_reference()
        return max(0.0, ref - self.strategy_equity_conservative())

    def drawdown_pct(self):
        if not self.seeded:
            return None
        ref = self.risk_equity_reference()
        dd = self.drawdown_usd()
        if ref is None or ref <= 0:
            return 100.0 if dd and dd > 0 else 0.0
        return 100.0 * dd / ref

    # ── status / guards ─────────────────────────────────────────────────
    def _seed_prefix_intact(self) -> bool:
        s = self.state["seed"]
        n0 = int(s.get("settled_count_0") or 0)
        rows = self.settled()
        if len(rows) < n0:
            return False
        if s.get("prefix_digest"):
            return journal_digest(rows[:n0]) == s["prefix_digest"]
        prefix = float(sum(float(t.get("net_pnl") or 0.0) for t in rows[:n0]))
        return abs(prefix - float(s["realized_pnl_cum_0"])) < 1e-6

    # ── journal evidence watermark (defect 1) ───────────────────────────
    def _advance_journal_watermark(self) -> None:
        """Record the most trading history ever evidenced. Only ever grows,
        and never while the current journal contradicts the evidence."""
        if not self.seeded or self.state.get("journal_mismatch"):
            return
        rows = self.settled()
        wm = self.state.get("journal_watermark") or {}
        n_old = int(wm.get("settled_count") or 0)
        if len(rows) < n_old:
            return
        if n_old and journal_digest(rows[:n_old]) != wm.get("digest"):
            return
        eq = self.strategy_equity()
        self.state["journal_watermark"] = {
            "settled_count": len(rows), "digest": journal_digest(rows),
            "realized_pnl_cum": _round(self.realized_pnl_cum()),
            "strategy_equity": _round(eq) if eq is not None else None,
            "at": now_iso()}

    def _check_journal_against_watermark(self) -> bool:
        """True when the mismatch state changed. Sets `journal_mismatch`
        when the journal no longer contains the evidenced history, clears
        it only when that history is back."""
        wm = self.state.get("journal_watermark") or {}
        n = int(wm.get("settled_count") or 0)
        was = self.state.get("journal_mismatch")
        if not self.seeded or not n:
            return False
        rows = self.settled()
        reason = None
        if len(rows) < n:
            reason = (f"journal has {len(rows)} settled rows, {n} were evidenced "
                      f"(restored or truncated journal)")
        elif journal_digest(rows[:n]) != wm.get("digest"):
            reason = (f"the first {n} settled rows differ from the evidenced history "
                      f"(replaced journal)")
        if reason and not was:
            self.state["journal_mismatch"] = {"detected_at": now_iso(), "reason": reason,
                                              "evidenced_settled_count": n,
                                              "found_settled_count": len(rows),
                                              "evidenced_strategy_equity": wm.get("strategy_equity")}
            self._note_unproven("journal does not contain the evidenced trading history: " + reason)
            log.error(f"[EQUITY] JOURNAL MISMATCH: {reason}; risk_equity_status=UNRECONCILED, "
                      f"HWM kept, evidenced losses kept, CAPITAL blocked")
            return True
        if not reason and was:
            self.state["journal_mismatch"] = None
            unproven = self.state["status_basis"].get("unproven") or []
            self.state["status_basis"]["unproven"] = [u for u in unproven
                                                      if not u.startswith("journal does not contain the evidenced")]
            log.warning("[EQUITY] the evidenced journal is back; journal mismatch cleared")
            return True
        return False

    def derive_status(self) -> str:
        """Re-derive; automatic moves are conservative only."""
        if not self.seeded:
            return STATUS_UNRECONCILED
        if not self._seed_prefix_intact():
            self._note_unproven("journal does not contain the seed prefix "
                                "(restored journal older than the ledger?)")
            return STATUS_UNRECONCILED
        if self.state.get("journal_mismatch"):
            return STATUS_UNRECONCILED
        if self.unclassified_flows():
            return STATUS_UNRECONCILED
        persisted = self.state.get("risk_equity_status")
        if persisted not in STATUSES:
            return STATUS_UNRECONCILED
        if persisted == STATUS_UNRECONCILED:
            # the transient causes above are gone; the persisted floor is
            # whatever the seed established (never RECONCILED by itself)
            floor = self.state["seed"].get("status_at_seed", STATUS_CONSERVATIVE)
            return floor if floor in (STATUS_CONSERVATIVE, STATUS_RECONCILED) else STATUS_UNRECONCILED
        return persisted

    def _note_unproven(self, sentence: str) -> None:
        lst = self.state["status_basis"].setdefault("unproven", [])
        if sentence not in lst:
            lst.append(sentence)

    def status(self) -> str:
        return self.state.get("risk_equity_status", STATUS_UNRECONCILED)

    def _reconcile_status(self) -> None:
        new = self.derive_status()
        old = self.state.get("risk_equity_status")
        if new != old:
            log.warning(f"[EQUITY] risk_equity_status {old} -> {new}")
            self.state["risk_equity_status"] = new
        if new == STATUS_RECONCILED:
            self.state["status_basis"]["unproven"] = []

    def guards(self) -> list:
        """CAPITAL guard names currently firing, in evaluation order."""
        out = []
        if not self.seeded:
            out.append(GUARD_UNSEEDED)
        else:
            if self.unclassified_flows():
                out.append(GUARD_FLOW_UNRESOLVED)
            st = self.derive_status()
            allow_cons = bool(getattr(CFG, "RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE", False))
            if st == STATUS_UNRECONCILED or (st == STATUS_CONSERVATIVE and not allow_cons):
                out.append(GUARD_UNRECONCILED)
        if self.state.get("capital_hold"):
            out.append(GUARD_CAPITAL_HOLD)
        return out

    def capital_eligible(self) -> bool:
        return not self.guards()

    # ── the per-cycle observation (design §5) ───────────────────────────
    def _epsilon(self) -> float:
        n = int(self.state.get("anchor", {}).get("settled_since_anchor") or 0)
        return self.eps_base + self.eps_per_trade * n

    def _next_flow_id(self) -> str:
        return "flow-%04d" % (len(self.state["flows"]) + 1)

    def observe(self, cash, cycle_n: int = 0, quiet: bool = True,
                when: str = None) -> dict:
        """One observation of broker cash. Returns a snapshot. Never raises
        on accounting; a None cash is a no-op."""
        when = when or now_iso()
        if cash is None:
            return self.snapshot()
        if self._check_journal_against_watermark():
            self._reconcile_status()
            self.save()
        cash = float(cash)
        basis = self.open_cost_basis()
        settled = self.settled()
        settled_count = len(settled)
        open_count = self.posmgr.open_count() if self.posmgr is not None else 0
        # settlement / position change since the previous observation is
        # not quiet: the broker credits before the journal writes
        if self._last_obs is not None and self._last_obs != (settled_count, open_count):
            quiet = False
        self._last_obs = (settled_count, open_count)

        if not self.seeded:
            if self.env == "demo":
                self._seed_first_observation(cash, basis, when)
            else:
                self._reconcile_status()
                return self.snapshot()

        s = self.state["seed"]
        pnl_cum = self.realized_pnl_cum()
        expected = (float(s["account_equity_0"]) + (pnl_cum - float(s["realized_pnl_cum_0"]))
                    + self.flows_cum())
        r = (cash + basis) - expected
        eps = self._epsilon()
        pend = self.state.get("pending")
        if abs(r) <= eps:
            if abs(r) > 1e-9:
                self._append_flow(r, FLOW_ROUNDING, "auto", when, cycle_n, {
                    "cash": cash, "open_cost_basis": basis, "epsilon": eps})
            self.state["pending"] = None
            self.state["anchor"] = {"settled_since_anchor": 0, "at": when}
        elif not quiet:
            pass                                   # a race can explain it
        elif pend is None or abs(float(pend["residual"]) - r) > eps:
            self.state["pending"] = {"residual": _round(r), "first_seen_cycle": cycle_n,
                                     "consecutive": 1, "first_seen_at": when}
        else:
            pend["consecutive"] = int(pend["consecutive"]) + 1
            if pend["consecutive"] >= self.k_quiet_cycles:
                evidence = {"cash": cash, "open_cost_basis": basis,
                            "realized_pnl_cum": pnl_cum, "epsilon": eps,
                            "cycles_observed": pend["consecutive"],
                            "first_seen_at": pend["first_seen_at"]}
                if r > 0 and (self.risk_equity_reference() or 0.0) <= 0.0:
                    # No stake was ever recorded (reference 0): this is the
                    # initial stake, not a deposit after a loss. It becomes
                    # the seed. With any positive reference a deposit never
                    # enters strategy equity (core invariant).
                    self._fold_initial_stake(r, when, cycle_n, evidence)
                elif r > 0:
                    self._append_flow(r, FLOW_DEPOSIT, "auto", when, cycle_n, evidence)
                else:
                    self._append_flow(r, FLOW_UNCLASSIFIED, None, when, cycle_n, evidence)
                    log.error(f"[EQUITY] negative residual {r:+.4f}$ is UNCLASSIFIED: "
                              f"withdrawal or unrecorded loss cannot be told apart; "
                              f"CAPITAL blocked ({GUARD_FLOW_UNRESOLVED}) until an "
                              f"operator classifies it")
                self.state["pending"] = None
        self._track_settled_since_anchor(settled_count)
        self._refresh_hwm(when)
        self._roll_day(when)
        self._reconcile_status()
        self.save()
        return self.snapshot()

    def _fold_initial_stake(self, amount, when, cycle_n, evidence) -> None:
        s = self.state["seed"]
        s["strategy_equity_0"] = _round(float(s["strategy_equity_0"]) + amount)
        s["account_equity_0"] = _round(float(s["account_equity_0"]) + amount)
        s["hwm_0"] = _round(float(s["hwm_0"]) + amount)
        self.state["hwm"]["risk_equity_reference"] = _round(
            float(self.state["hwm"].get("risk_equity_reference") or 0.0) + amount)
        d = self.state.get("daily") or {}
        if d.get("sod_strategy_equity") is not None:
            d["sod_strategy_equity"] = _round(float(d["sod_strategy_equity"]) + amount)
        row = self._append_flow(amount, "initial_stake", "auto", when, cycle_n, evidence)
        row["note"] = "folded into the seed: no stake existed before it"
        log.warning(f"[EQUITY_SEED] initial stake {amount:+.4f}$ folded into the seed "
                    f"(strategy_equity_0={s['strategy_equity_0']}, hwm={s['hwm_0']})")

    def _track_settled_since_anchor(self, settled_count: int) -> None:
        anc = self.state.setdefault("anchor", {"settled_since_anchor": 0})
        last = anc.get("settled_count")
        if last is not None and settled_count > int(last):
            anc["settled_since_anchor"] = int(anc.get("settled_since_anchor") or 0) + (settled_count - int(last))
        anc["settled_count"] = settled_count

    def _append_flow(self, amount, kind, classified_by, when, cycle_n, evidence) -> dict:
        row = {"id": self._next_flow_id(), "at": when, "amount": _round(amount),
               "kind": kind, "classified_by": classified_by, "cycle": cycle_n,
               "evidence": evidence, "note": ""}
        self.state["flows"].append(row)
        if kind != FLOW_ROUNDING:
            log.warning(f"[EQUITY_FLOW] {row['id']} kind={kind} amount={row['amount']:+.4f}$ "
                        f"classified_by={classified_by}; strategy_equity unchanged")
        return row

    def _roll_day(self, when: str) -> None:
        day = when[:10]
        d = self.state.setdefault("daily", {"date": None, "sod_strategy_equity": None})
        if d.get("date") != day:
            d["date"] = day
            d["sod_strategy_equity"] = _round(self.strategy_equity()) if self.seeded else None

    def sod_strategy_equity(self):
        return (self.state.get("daily") or {}).get("sod_strategy_equity")

    # ── seeding ─────────────────────────────────────────────────────────
    def _seed_dict(self, at, source, account_equity_0, strategy_equity_0,
                   hwm_0, status_at_seed, evidence, applied_by) -> dict:
        rows = self.settled()
        return {"at": at, "source": source,
                "account_equity_0": _round(account_equity_0),
                "realized_pnl_cum_0": _round(self.realized_pnl_cum()),
                "settled_count_0": len(rows),
                "prefix_digest": journal_digest(rows),
                "strategy_equity_0": _round(strategy_equity_0),
                "hwm_0": _round(hwm_0),
                "status_at_seed": status_at_seed,
                "evidence": evidence, "evidence_sha256": _sha(_canonical(evidence)),
                "applied_by": applied_by}

    def _seed_first_observation(self, cash, basis, when) -> None:
        rows = self.settled()
        dd = self.rolling_drawdown_journal()
        eq0 = cash + basis
        status = STATUS_RECONCILED if not rows else STATUS_CONSERVATIVE
        self.state["seed"] = self._seed_dict(
            when, "first_observation", eq0, eq0, eq0 + dd, status,
            {"cash": cash, "open_cost_basis": basis, "settled_rows": len(rows),
             "rolling_drawdown": _round(dd)}, "first_observation")
        self.state["hwm"] = {"risk_equity_reference": _round(eq0 + dd), "at": when,
                             "rebased_from": None, "floor_from_settled_index": 0}
        self.state["risk_equity_status"] = status
        self.state["status_basis"] = {
            "set_at": when, "set_by": "first_observation",
            "provenance_window": {"from": when, "to": None},
            "unproven": [] if status == STATUS_RECONCILED
            else [f"funding before {when} (journal already held {len(rows)} settled rows)"],
            "evidence_sha256": self.state["seed"]["evidence_sha256"]}
        log.warning(f"[EQUITY_SEED] first_observation strategy_equity_0={eq0:.4f} "
                    f"hwm_0={eq0 + dd:.4f} status={status}")

    def propose_seed(self, pre_flow_cash, pre_flow_at: str, evidence_ref: str,
                     cash_now, open_basis_now=None) -> dict:
        """Migration proposal (design §2). Pure; writes nothing."""
        rows = self.settled()
        basis_now = self.open_cost_basis() if open_basis_now is None else float(open_basis_now)
        dd = self.rolling_drawdown_journal()
        pnl_cum = self.realized_pnl_cum()
        pre_flow_cash = float(pre_flow_cash)
        problems = []
        if any((t.get("settled_at") or "") > pre_flow_at for t in rows):
            problems.append("settlements after the pre-flow observation")
        if basis_now > 0 or (self.posmgr is not None and self.posmgr.open_count() > 0):
            problems.append("open positions at seed time")
        strategy_equity_0 = pre_flow_cash
        account_equity_0 = pre_flow_cash
        hwm_0 = strategy_equity_0 + dd
        residual = (float(cash_now) + basis_now) - account_equity_0
        flow = None
        if abs(residual) > self.eps_base:
            flow = {"amount": _round(residual),
                    "kind": FLOW_DEPOSIT if residual > 0 else FLOW_UNCLASSIFIED}
        proposal = {
            "kind": "seed", "source": "migration_reconstructed",
            "pre_flow_cash": _round(pre_flow_cash), "pre_flow_at": pre_flow_at,
            "evidence_ref": evidence_ref,
            "cash_now": _round(cash_now), "open_cost_basis_now": _round(basis_now),
            "realized_pnl_cum": _round(pnl_cum), "settled_rows": len(rows),
            "rolling_drawdown": _round(dd),
            "account_equity_0": _round(account_equity_0),
            "strategy_equity_0": _round(strategy_equity_0), "hwm_0": _round(hwm_0),
            "migration_flow": flow,
            "risk_equity_status": (STATUS_UNRECONCILED if problems or
                                   (flow and flow["kind"] == FLOW_UNCLASSIFIED)
                                   else STATUS_CONSERVATIVE),
            "unproven": [f"funding before {pre_flow_at}"] + problems,
            "drawdown_pct_after": _round(100.0 * dd / hwm_0) if hwm_0 > 0 else None,
        }
        proposal["sha256"] = _sha(_canonical({k: v for k, v in proposal.items()
                                              if k != "sha256"}))
        return proposal

    def apply_seed(self, proposal: dict, expected_sha: str, when: str = None) -> bool:
        if self.seeded:
            log.warning("[EQUITY_SEED] refused: a seed already exists (never overwritten)")
            return False
        if not expected_sha or proposal.get("sha256") != expected_sha:
            log.warning(f"[EQUITY_SEED] refused: hash mismatch (proposal {proposal.get('sha256')})")
            return False
        when = when or now_iso()
        status = proposal["risk_equity_status"]
        self.state["seed"] = self._seed_dict(
            when, proposal["source"], proposal["account_equity_0"],
            proposal["strategy_equity_0"], proposal["hwm_0"], status,
            {k: proposal[k] for k in ("pre_flow_cash", "pre_flow_at", "evidence_ref",
                                      "cash_now", "open_cost_basis_now",
                                      "realized_pnl_cum", "settled_rows",
                                      "rolling_drawdown")},
            "EQUITY_LEDGER_SEED_SHA256")
        self.state["hwm"] = {"risk_equity_reference": proposal["hwm_0"], "at": when,
                             "rebased_from": None, "floor_from_settled_index": 0}
        self.state["risk_equity_status"] = status
        self.state["status_basis"] = {
            "set_at": when, "set_by": "migration",
            "provenance_window": {"from": proposal["pre_flow_at"], "to": None},
            "unproven": list(proposal["unproven"]),
            "evidence_sha256": self.state["seed"]["evidence_sha256"]}
        mf = proposal.get("migration_flow")
        if mf:
            self._append_flow(mf["amount"], mf["kind"],
                              "migration" if mf["kind"] == FLOW_DEPOSIT else None,
                              when, 0, {"pre_flow_cash": proposal["pre_flow_cash"],
                                        "cash_now": proposal["cash_now"],
                                        "evidence_ref": proposal["evidence_ref"]})
        self._roll_day(when)
        self._reconcile_status()
        ok = self.save()
        log.warning(f"[EQUITY_SEED] applied sha={expected_sha[:12]} status={self.status()} "
                    f"strategy_equity={self.strategy_equity():.4f} hwm={self.risk_equity_reference():.4f} "
                    f"drawdown_pct={self.drawdown_pct():.2f}")
        return ok

    # ── operator: classification ────────────────────────────────────────
    def classify_flow(self, flow_id: str, kind: str, action_id: str = None,
                      correction_id: str = None, when: str = None) -> bool:
        row = next((f for f in self.state["flows"] if f.get("id") == flow_id), None)
        if row is None:
            log.warning(f"[EQUITY_CLASSIFY] refused: unknown flow {flow_id}"); return False
        if row.get("kind") != FLOW_UNCLASSIFIED:
            log.warning(f"[EQUITY_CLASSIFY] refused: {flow_id} already {row.get('kind')}"); return False
        if kind == FLOW_WITHDRAWAL:
            if float(row["amount"]) >= 0:
                log.warning(f"[EQUITY_CLASSIFY] refused: {flow_id} is not negative"); return False
        elif kind == "loss":
            ids = {c.get("correction_id") for c in self.tlog.correction_rows()}
            if not correction_id or correction_id not in ids:
                log.warning(f"[EQUITY_CLASSIFY] refused: loss needs a ledger correction "
                            f"present in the journal (got {correction_id!r})"); return False
        else:
            log.warning(f"[EQUITY_CLASSIFY] refused: kind {kind!r} not allowed"); return False
        when = when or now_iso()
        row["kind"] = FLOW_WITHDRAWAL if kind == FLOW_WITHDRAWAL else "loss_by_correction"
        row["classified_by"] = "operator"
        row["classified_at"] = when
        row["operator_action_id"] = action_id
        if correction_id:
            # the journal now carries the PnL through the correction, so the
            # row is neither an external flow nor unresolved: it is excluded
            # from flows_cum and the residual closes on its own
            row["resolved_by_correction"] = correction_id
            row["note"] = f"loss carried by ledger correction {correction_id}"
        self._reconcile_status()
        ok = self.save()
        log.warning(f"[EQUITY_CLASSIFY] {flow_id} -> {row['kind']} by operator action {action_id}")
        return ok

    # ── operator: rebase / hold release / attestation ───────────────────
    def evidence_sha256(self) -> str:
        parts = []
        for name in ("kalshi_trades.json", LEDGER_FILE, "positions_state.json"):
            try:
                with open(_p(name), "rb") as fh:
                    parts.append(hashlib.sha256(fh.read()).hexdigest())
            except OSError:
                parts.append("absent")
        return _sha("|".join(parts))

    def rebase_preconditions(self, ctx: dict) -> list:
        """ctx: drawdown_firing, reconcile_status, open_positions, in_flight_orders."""
        failures = []
        if self.derive_status() != STATUS_RECONCILED:
            failures.append("risk_equity_status is not RECONCILED")
        if not ctx.get("drawdown_firing"):
            failures.append("equity_drawdown is not firing")
        if self.unclassified_flows():
            failures.append("unclassified flow present")
        if self.state.get("pending"):
            failures.append("pending residual under observation")
        if ctx.get("reconcile_status") != "MATCH":
            failures.append("reconciliation is not MATCH")
        if self.state.get("journal_mismatch"):
            failures.append("journal mismatch (evidenced history missing)")
        if int(ctx.get("open_positions") or 0) > 0:
            failures.append("open positions")
        orders = ctx.get("orders")
        if not isinstance(orders, dict):
            failures.append("order state not provided (authoritative context required)")
        else:
            if orders.get("local_open"):
                failures.append(f"open local orders {sorted(orders['local_open'])}")
            if orders.get("pending_intents"):
                failures.append(f"pending submit intents {sorted(orders['pending_intents'])}")
            if orders.get("resolution_halt"):
                failures.append("ambiguous order resolution halt")
            if orders.get("broker_open") is None:
                failures.append(f"broker order state unknown ({orders.get('broker_error')})")
            elif int(orders.get("broker_open") or 0) > 0:
                failures.append(f"open orders at the broker {orders.get('broker_open_ids')}")
            if orders.get("disagreement"):
                failures.append("order state disagreement between local and broker")
        if int(ctx.get("in_flight_orders") or 0) > 0 and "in-flight orders" not in failures:
            failures.append("in-flight orders")
        if self.state.get("capital_hold"):
            failures.append("capital_hold already open")
        return failures

    def propose_rebase(self, reason: str, action_id: str) -> dict:
        if not reason or not str(reason).strip():
            raise ValueError("a reason is required")
        if not action_id:
            raise ValueError("an operator action id is required")
        ev = self.evidence_sha256()
        old = {"hwm": self.risk_equity_reference(), "strategy_equity": self.strategy_equity(),
               "drawdown_usd": self.drawdown_usd(), "drawdown_pct": self.drawdown_pct()}
        new = {"hwm": self.strategy_equity()}
        proposal = {"kind": "rebase", "rebase_id": "rb-" + _sha(ev + action_id)[:12],
                    "reason": str(reason).strip(), "operator_action_id": str(action_id),
                    "old_baseline": {k: _round(v) if v is not None else None for k, v in old.items()},
                    "new_baseline": {"hwm": _round(new["hwm"]) if new["hwm"] is not None else None},
                    "evidence_sha256": ev}
        proposal["token"] = _sha(_canonical({k: v for k, v in proposal.items() if k != "token"})
                                 + "|" + str(action_id) + "|" + ev)
        return proposal

    def apply_rebase(self, reason, action_id, token, ctx: dict, when: str = None) -> bool:
        try:
            proposal = self.propose_rebase(reason, action_id)
        except ValueError as e:
            log.warning(f"[EQUITY_REBASE] refused: {e}"); return False
        if token in self.state["consumed_tokens"]:
            log.warning("[EQUITY_REBASE] refused: token already consumed (replay)"); return False
        if token != proposal["token"]:
            log.warning("[EQUITY_REBASE] refused: token does not match the recomputed proposal"); return False
        failures = self.rebase_preconditions(ctx)
        if failures:
            log.warning(f"[EQUITY_REBASE] refused: {failures}"); return False
        when = when or now_iso()
        prior = dict(self.state["hwm"])
        entry = dict(proposal)
        entry.update({"applied_at": when, "token_sha256": _sha(token), "prior_hwm_row": prior,
                      "settled_count_at_rebase": len(self.settled()), "validation": None})
        entry.pop("token", None)
        self.state["rebases"].append(entry)
        self.state["hwm"] = {"risk_equity_reference": proposal["new_baseline"]["hwm"], "at": when,
                             "rebased_from": proposal["rebase_id"],
                             "floor_from_settled_index": len(self.settled())}
        self.state["consumed_tokens"].append(token)
        self.state["capital_hold"] = {"reason": "post_rebase_validation", "since": when,
                                      "rebase_id": proposal["rebase_id"], "released_by": None}
        ok = self.save()
        log.warning(f"[EQUITY_REBASE] id={proposal['rebase_id']} from_hwm={prior.get('risk_equity_reference')} "
                    f"to_hwm={proposal['new_baseline']['hwm']} drawdown_acknowledged="
                    f"{proposal['old_baseline']['drawdown_usd']} operator_action_id={action_id} "
                    f"capital_hold=post_rebase_validation")
        return ok

    def propose_hold_release(self, action_id: str, validation_ref: str) -> dict:
        hold = self.state.get("capital_hold")
        if not hold:
            raise ValueError("no capital_hold to release")
        if not action_id or not validation_ref:
            raise ValueError("action id and validation reference are required")
        rb = next((r for r in self.state["rebases"] if r.get("rebase_id") == hold.get("rebase_id")), None)
        if rb is not None and rb.get("operator_action_id") == str(action_id):
            raise ValueError("the release must come from a different operator action than the rebase")
        ev = self.evidence_sha256()
        proposal = {"kind": "hold_release", "rebase_id": hold.get("rebase_id"),
                    "operator_action_id": str(action_id), "validation_ref": str(validation_ref),
                    "evidence_sha256": ev}
        proposal["token"] = _sha("hold-release|" + _canonical({k: v for k, v in proposal.items() if k != "token"}))
        return proposal

    def apply_hold_release(self, action_id, validation_ref, token, when: str = None) -> bool:
        try:
            proposal = self.propose_hold_release(action_id, validation_ref)
        except ValueError as e:
            log.warning(f"[EQUITY_HOLD_RELEASE] refused: {e}"); return False
        if token in self.state["consumed_tokens"]:
            log.warning("[EQUITY_HOLD_RELEASE] refused: token already consumed"); return False
        if token != proposal["token"]:
            log.warning("[EQUITY_HOLD_RELEASE] refused: token mismatch"); return False
        when = when or now_iso()
        for r in self.state["rebases"]:
            if r.get("rebase_id") == proposal["rebase_id"]:
                r["validation"] = {"operator_action_id": str(action_id),
                                   "validation_ref": str(validation_ref),
                                   "released_at": when, "token_sha256": _sha(token)}
        self.state["capital_hold"] = None
        self.state["consumed_tokens"].append(token)
        ok = self.save()
        log.warning(f"[EQUITY_HOLD_RELEASE] rebase {proposal['rebase_id']} hold released by "
                    f"operator action {action_id} (validation {validation_ref})")
        return ok

    def propose_attestation(self, action_id: str, funding_records_sha256: str) -> dict:
        if not action_id or not funding_records_sha256:
            raise ValueError("action id and funding records hash are required")
        proposal = {"kind": "attest_reconciled", "operator_action_id": str(action_id),
                    "funding_records_sha256": str(funding_records_sha256),
                    "evidence_sha256": self.evidence_sha256(),
                    "flows": [(f["id"], f["kind"], f["amount"]) for f in self.state["flows"]]}
        proposal["token"] = _sha("attest|" + _canonical({k: v for k, v in proposal.items() if k != "token"}))
        return proposal

    def apply_attestation(self, action_id, funding_records_sha256, token, when: str = None) -> bool:
        try:
            proposal = self.propose_attestation(action_id, funding_records_sha256)
        except ValueError as e:
            log.warning(f"[EQUITY_ATTEST] refused: {e}"); return False
        if not self.seeded or self.unclassified_flows() or self.state.get("pending") \
                or not self._seed_prefix_intact() or self.state.get("journal_mismatch"):
            log.warning("[EQUITY_ATTEST] refused: ledger is not in an attestable state "
                        "(unseeded, unresolved flow, pending residual or journal mismatch)")
            return False
        if token in self.state["consumed_tokens"] or token != proposal["token"]:
            log.warning("[EQUITY_ATTEST] refused: token invalid or consumed"); return False
        when = when or now_iso()
        self.state["risk_equity_status"] = STATUS_RECONCILED
        self.state["seed"]["status_at_seed"] = STATUS_RECONCILED
        self.state["status_basis"] = {"set_at": when, "set_by": "operator",
                                      "provenance_window": self.state["status_basis"].get("provenance_window"),
                                      "unproven": [], "evidence_sha256": proposal["evidence_sha256"],
                                      "attestation": {"operator_action_id": str(action_id),
                                                      "funding_records_sha256": str(funding_records_sha256),
                                                      "token_sha256": _sha(token)}}
        self.state["consumed_tokens"].append(token)
        ok = self.save()
        log.warning(f"[EQUITY_ATTEST] risk_equity_status=RECONCILED by operator action {action_id}")
        return ok

    # ── boot-time application of declarative operator actions ───────────
    def apply_operator_actions(self, env: dict, cash_now, ctx: dict) -> dict:
        """Read the declarative variables and apply what matches. Returns
        what was attempted. Every refusal is a logged no-op."""
        done = {}
        g = env.get
        if g("EQUITY_LEDGER_SEED_PRE_FLOW_CASH") and not self.seeded and cash_now is not None:
            try:
                proposal = self.propose_seed(g("EQUITY_LEDGER_SEED_PRE_FLOW_CASH"),
                                             g("EQUITY_LEDGER_SEED_PRE_FLOW_AT") or "",
                                             g("EQUITY_LEDGER_SEED_EVIDENCE") or "", cash_now)
                log.warning(f"[EQUITY_SEED] proposal sha256={proposal['sha256']} "
                            f"status={proposal['risk_equity_status']} "
                            f"strategy_equity_0={proposal['strategy_equity_0']} hwm_0={proposal['hwm_0']} "
                            f"drawdown_pct_after={proposal['drawdown_pct_after']} "
                            f"migration_flow={proposal['migration_flow']}")
                done["seed"] = self.apply_seed(proposal, g("EQUITY_LEDGER_SEED_SHA256") or "")
            except (TypeError, ValueError) as e:
                log.warning(f"[EQUITY_SEED] proposal invalid: {e}"); done["seed"] = False
        spec = (g("EQUITY_FLOW_CLASSIFY") or "").strip()
        if spec:
            for item in [s.strip() for s in spec.split(",") if s.strip()]:
                fid, _, rest = item.partition("=")
                kind, _, extra = rest.partition(":")
                corr = extra if kind == "loss" else None
                done[f"classify:{fid}"] = self.classify_flow(
                    fid.strip(), kind.strip(), action_id=g("EQUITY_FLOW_CLASSIFY_ACTION_ID"),
                    correction_id=corr or None)
        if g("EQUITY_LEDGER_REBASE_TOKEN"):
            done["rebase"] = self.apply_rebase(g("EQUITY_LEDGER_REBASE_REASON"),
                                               g("EQUITY_LEDGER_REBASE_ACTION_ID"),
                                               g("EQUITY_LEDGER_REBASE_TOKEN"), ctx)
        if g("EQUITY_LEDGER_HOLD_RELEASE_TOKEN"):
            done["hold_release"] = self.apply_hold_release(
                g("EQUITY_LEDGER_HOLD_RELEASE_ACTION_ID"), g("EQUITY_LEDGER_HOLD_RELEASE_VALIDATION"),
                g("EQUITY_LEDGER_HOLD_RELEASE_TOKEN"))
        if g("EQUITY_LEDGER_ATTEST_TOKEN"):
            done["attest"] = self.apply_attestation(
                g("EQUITY_LEDGER_ATTEST_ACTION_ID"), g("EQUITY_LEDGER_ATTEST_FUNDING_RECORDS_SHA256"),
                g("EQUITY_LEDGER_ATTEST_TOKEN"))
        return done

    # ── load-time reconciliation ────────────────────────────────────────
    def _reconcile_on_load(self) -> None:
        if not self.seeded:
            return
        before = self.state.get("risk_equity_status")
        changed = self._check_journal_against_watermark()
        changed = self._refresh_hwm() or changed
        self._reconcile_status()
        if changed or self.state.get("risk_equity_status") != before:
            self.save()

    # ── reporting ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        eq = self.strategy_equity()
        ref = self.risk_equity_reference()
        dd = self.drawdown_usd()
        pct = self.drawdown_pct()
        return {
            "risk_equity_status": self.derive_status(),
            "seeded": self.seeded,
            "strategy_equity": _round(eq) if eq is not None else None,
            "risk_equity_reference": _round(ref) if ref is not None else None,
            "drawdown_usd": _round(dd) if dd is not None else None,
            "drawdown_pct": round(pct, 4) if pct is not None else None,
            "external_flows_cum": _round(self.flows_cum()),
            "unclassified_flows": [f["id"] for f in self.unclassified_flows()],
            "pending_residual": (self.state.get("pending") or {}).get("residual"),
            "capital_hold": self.state.get("capital_hold"),
            "capital_guards": self.guards(),
            "capital_eligible": self.capital_eligible(),
            "unproven": list((self.state.get("status_basis") or {}).get("unproven") or []),
            "sod_strategy_equity": self.sod_strategy_equity(),
            "rebases": len(self.state.get("rebases") or []),
            "journal_mismatch": self.state.get("journal_mismatch"),
        }

    def banner_line(self) -> str:
        s = self.snapshot()
        return (f"[EQUITY] risk_equity_status={s['risk_equity_status']} "
                f"strategy_equity={s['strategy_equity']} hwm={s['risk_equity_reference']} "
                f"drawdown_pct={s['drawdown_pct']} flows_cum={s['external_flows_cum']} "
                f"capital_guards={s['capital_guards']} capital_eligible={s['capital_eligible']}")
