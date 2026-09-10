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
import math
import os

from config import CFG, _p
from continuity import (CONTINUITY_FILE, KIND_EVIDENCE, KIND_RECOVERY,
                        KIND_TOKEN, ChainError, ContinuityChain)
from persistence import JsonStore, file_fingerprint, read_generation
from state_tx import (NonFiniteValue, StaleAuthority, all_finite, check_finite,
                      fence_for)
import account_binding
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
#: A01: continuity with durably observed evidence cannot be proven. Never
#: cleared by anything except re-establishing that evidence.
GUARD_CONTINUITY = "continuity_rollback"
#: A06: a cash movement is being observed and has no explanation yet. The
#: balance is OBSERVABLE and may even reconcile numerically, but it is not
#: CAPITAL-ADMISSIBLE until it is classified with durable evidence.
GUARD_RESIDUAL_UNEXPLAINED = "cash_residual_unexplained"
#: A05: the accounting mode is not one this build implements.
GUARD_ACCOUNTING_MODE = "risk_accounting_mode_invalid"
#: A04: the journal carries the same economic event twice.
GUARD_JOURNAL_INTEGRITY = "journal_event_identity"
#: A19: another writer has advanced the authoritative state past the
#: generation this process holds. Everything computed from the local view
#: describes a superseded world, so no CAPITAL decision may rest on it
#: until the view is reloaded.
GUARD_STALE_READER = "authority_stale_reader"
#: A16: a non-finite number reached economic state. NaN compares False
#: against every threshold, so a guard that merely compares would pass it.
GUARD_NON_FINITE = "economic_value_non_finite"
#: A20: this durable state was written under a different environment or a
#: different account. Every number in it is self-consistent and about
#: somebody else's money.
GUARD_ACCOUNT_BINDING = "state_account_binding"
ACCOUNTING_GUARDS = (GUARD_UNSEEDED, GUARD_FLOW_UNRESOLVED,
                     GUARD_UNRECONCILED, GUARD_CAPITAL_HOLD,
                     GUARD_CONTINUITY, GUARD_RESIDUAL_UNEXPLAINED,
                     GUARD_ACCOUNTING_MODE, GUARD_JOURNAL_INTEGRITY,
                     GUARD_STALE_READER, GUARD_NON_FINITE,
                     GUARD_ACCOUNT_BINDING)

LEDGER_FILE = "equity_ledger.json"
SCHEMA_VERSION = 1

#: A05: the accounting modes this build actually implements. Anything else
#: -- a typo, a rollback value from an older release, an empty string -- is
#: INVALID. It never falls back to the cash denominator, because the cash
#: denominator is precisely what a deposit repairs: falling back to it on an
#: unrecognized mode turns an unknown configuration into a silent bypass of
#: loss-derived protection (audit finding A05).
RISK_ACCOUNTING_MODES = ("strategy", "cash")
#: ...and the subset that PRESERVES loss-derived protection. `cash` is a
#: documented rollback to the pre-F2 denominator, so it stays recognized --
#: but a deposit repairs a cash-denominated drawdown, which is the very
#: thing F2 exists to prevent. It is therefore observation-only: recognized,
#: never CAPITAL-admissible.
CAPITAL_ADMISSIBLE_MODES = ("strategy",)


def accounting_mode() -> str:
    """The configured accounting mode, lower-cased and stripped. Returns the
    raw value even when invalid: the caller decides, and every caller here
    fails closed on an unrecognized one."""
    return str(getattr(CFG, "RISK_EQUITY_MODE", "strategy") or "").strip().lower()


def accounting_mode_valid() -> bool:
    """True only for a mode this build implements. An unknown value is not
    coerced to a default and never selects the cash denominator."""
    return accounting_mode() in RISK_ACCOUNTING_MODES


def accounting_mode_capital_admissible() -> bool:
    return accounting_mode() in CAPITAL_ADMISSIBLE_MODES


#: Local evidence bound into a rebase / seed decision (audit finding A03).
#: Two sequential broker GETs are not a transaction, and neither are two
#: reads of a local file: whatever authorizes a mutation must still be
#: byte-identical when the mutation commits.
BOUND_STATE_FILES = ("kalshi_trades.json", LEDGER_FILE, "positions_state.json",
                     "orders_state.json", "pending_intents.json",
                     "submission_guard.json")


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round(x) -> float:
    return round(float(x) + 0.0, 4)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and math.isfinite(float(x))


def _event_keys(row) -> list:
    """The economic identity of a journal row (audit finding A04).

    Delegates to the journal's own definition so the writer and every reader
    agree on what "the same event" means. Falls back to the local id only if
    the journal module is unavailable, which keeps this module importable in
    isolation without silently weakening the check in production."""
    try:
        from trade_logger import TradeLogger
        return list(TradeLogger.event_keys(row))
    except Exception:                                       # noqa: BLE001
        return [("trade_id", str(row.get("trade_id")))] if row.get("trade_id") else []


def journal_digest(rows) -> str:
    """Order-preserving fingerprint of settled rows: identity, PnL, time."""
    return _sha(_canonical([(t.get("trade_id"), _round(t.get("net_pnl") or 0.0),
                             t.get("settled_at")) for t in rows]))


class EquityLedger:
    """Persisted risk-equity state under DATA_DIR/equity_ledger.json."""

    def __init__(self, tlog, posmgr, env: str = "prod", path: str = None,
                 readonly: bool = False, binding: dict = None):
        self.tlog = tlog
        self.posmgr = posmgr
        self.env = env
        self.path = path or _p(LEDGER_FILE)
        #: A12: a genuinely non-mutating construction. Every durable write
        #: becomes a refusal that is LOGGED, not a silent no-op, so an
        #: inspection path can never be mistaken for an engine path.
        self.readonly = bool(readonly)
        self.chain = ContinuityChain(os.path.join(os.path.dirname(
            os.path.abspath(self.path)), CONTINUITY_FILE))
        self.k_quiet_cycles = max(1, int(getattr(CFG, "EQUITY_FLOW_QUIET_CYCLES", 3)))
        self.eps_base = float(getattr(CFG, "EQUITY_FLOW_EPS", 0.01))
        self.eps_per_trade = float(getattr(CFG, "EQUITY_FLOW_EPS_PER_TRADE", 0.005))
        self.schema_reject = None      # why a persisted ledger was refused
        self.from_backup = None        # which rotation copy answered, if any
        self.generation = 0            # fencing generation held by THIS reader
        self._durable = None           # last state proven to be on disk
        # A20: the environment/account this PROCESS is talking to. Compared
        # against the one stamped in the state on load.
        self.binding = binding if binding is not None else \
            account_binding.fingerprint(env=env)
        self.binding_status = "unbound"
        self.binding_reason = None
        self._binding_checked = False
        self.state = self._load()
        # A20: fresh state never reaches _load's binding check (there is no
        # file yet), so bind it here too. Every path through the constructor
        # leaves the state stamped with the account it belongs to.
        if not self.state.get("binding"):
            self._check_binding(self.state)
        self._last_obs = None          # (settled_count, open_count)
        self._reconcile_on_load()

    @classmethod
    def load_readonly(cls, tlog, posmgr, env: str = "prod", path: str = None):
        """A12: inspection instance. Reads state, computes everything, and
        writes NOTHING -- not the ledger, not a backup, not the continuity
        chain. `snapshot()` on the result is byte-for-byte non-mutating."""
        return cls(tlog, posmgr, env=env, path=path, readonly=True)

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
                "journal_watermark": None, "journal_mismatch": None,
                # A01: fencing generation and the continuity-chain record
                # this state was written against. Both are compared with the
                # authority OUTSIDE this file before the state is believed.
                "generation": 0, "continuity": {"seq": 0, "hash": None},
                # A06: sticky record of the worst unexplained ADVERSE cash
                # movement. Separate from `pending`, which any calm cycle
                # clears.
                "adverse_residual_floor": None,
                # A20: which environment/account this history is about.
                "binding": None,
                "continuity_block": None}

    #: A01 rule 3: strict schema. A field that is present but of the wrong
    #: shape is a REJECTION, never a value coerced into a default, because
    #: the defaults here ("no watermark", "no consumed token") all read as
    #: "nothing bad ever happened".
    def _schema_problem(self, raw):
        if not isinstance(raw, dict):
            return f"persisted ledger is {type(raw).__name__}, object expected"
        if raw.get("version") != SCHEMA_VERSION:
            return f"unknown ledger schema version {raw.get('version')!r}"
        for key, typ in (("flows", list), ("rebases", list),
                         ("consumed_tokens", list), ("hwm", dict),
                         ("status_basis", dict)):
            if key in raw and not isinstance(raw[key], typ):
                return f"'{key}' is {type(raw[key]).__name__}, {typ.__name__} expected"
        if any(not isinstance(t, str) for t in raw.get("consumed_tokens") or []):
            return "'consumed_tokens' holds a non-string entry"
        wm = raw.get("journal_watermark")
        if wm is not None:
            if not isinstance(wm, dict):
                return f"'journal_watermark' is {type(wm).__name__}, object expected"
            n = wm.get("settled_count")
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                return f"watermark settled_count {n!r} is not a count"
            if not isinstance(wm.get("digest"), str) or len(wm["digest"]) != 64:
                return "watermark digest is not a sha256"
        seed = raw.get("seed")
        if seed is not None:
            if not isinstance(seed, dict):
                return f"'seed' is {type(seed).__name__}, object expected"
            for f in ("strategy_equity_0", "realized_pnl_cum_0", "hwm_0"):
                v = seed.get(f)
                if v is not None and not _finite(v):
                    return f"seed.{f} = {v!r} is not a finite number"
        gen = raw.get("generation")
        if gen is not None and (not isinstance(gen, int) or isinstance(gen, bool)
                                or gen < 0):
            return f"'generation' {gen!r} is not a generation counter"
        return None

    def _load(self) -> dict:
        raw = JsonStore.load(self.path, None)
        self.from_backup = JsonStore.recovered_from_backup.get(
            os.path.abspath(self.path))
        if raw is None and not os.path.exists(self.path):
            return self._empty()
        problem = self._schema_problem(raw)
        if problem:
            # Not a silent reset: the continuity check below turns a refused
            # ledger into a BLOCK whenever the chain proves there was history
            # this empty state does not contain.
            self.schema_reject = problem
            log.error(f"[EQUITY] persisted ledger REFUSED: {problem}")
            return self._empty()
        base = self._empty()
        base.update(raw)
        self.generation = int(raw.get("generation") or 0)
        if self.from_backup:
            # The primary file exists but could not be verified, so a
            # rotation copy answered. Its generation is BEHIND the primary's,
            # and the fence would then refuse every future write -- a ledger
            # that can never be corrected is not fail-closed, it is stuck.
            # The high-water generation is adopted so the next commit
            # explicitly supersedes the unverifiable primary; the content
            # stays the recovered one, and `_continuity_reason` blocks this
            # boot precisely because that content is not proven current.
            on_disk = read_generation(self.path)
            if on_disk is not None and on_disk > self.generation:
                log.warning(f"[EQUITY] recovered from "
                            f"{os.path.basename(self.from_backup)} at "
                            f"generation {self.generation}; adopting the "
                            f"on-disk high-water generation {on_disk} so the "
                            f"next commit supersedes the unverifiable file")
                self.generation = int(on_disk)
        self._check_binding(base)
        return base

    def _check_binding(self, state: dict) -> None:
        """A20: whose history is this? Compared on every load, blocking on a
        mismatch, adopted (and logged) when the state predates the check."""
        stored = state.get("binding")
        if not stored and not isinstance(state.get("seed"), dict):
            # A ledger with no seed has no economic history yet: stamping it
            # now binds it from birth, and there is nothing to misattribute.
            state["binding"] = dict(self.binding)
            self.binding_status, self.binding_reason = "match", None
            return
        status, reason = account_binding.compare(stored, self.binding)
        if status == "credential_changed" and \
                account_binding.rebind_acknowledged(self.binding):
            log.warning("[EQUITY_BINDING] credential change ACKNOWLEDGED by "
                        "the operator for this exact fingerprint "
                        f"{account_binding.digest(self.binding)}: state "
                        f"re-bound to the current credential")
            state["binding"] = dict(self.binding)
            status, reason = "match", None
        self.binding_status, self.binding_reason = status, reason
        if status == "unbound":
            # Legacy state, written before the binding existed. It is
            # adopted on the next durable write, and said out loud: adoption
            # is not proof that the history belongs to this account, only a
            # record of the account that is claiming it from now on.
            state["binding"] = dict(self.binding)
            log.info(f"[EQUITY_BINDING] state carried no account binding; "
                     f"adopting {account_binding.digest(self.binding)} "
                     f"({self.binding.get('environment')}) -- prior history "
                     f"is claimed, not proven, for this account")
        elif status == "match":
            state["binding"] = dict(self.binding)
        else:
            log.critical(
                f"[EQUITY_BINDING] MISMATCH ({status}): {reason} -- this "
                f"ledger describes another account's economics; CAPITAL is "
                f"INELIGIBLE and a migration is required. Nothing is "
                f"rewritten and no history is discarded.")

    # ── the durable commit protocol (A01 rules 5, 6) ────────────────────
    def _continuity_payload(self) -> dict:
        rows = self.settled()
        eq = self.strategy_equity()
        return {"settled_count": len(rows), "digest": journal_digest(rows),
                "realized_pnl_cum": _round(self.realized_pnl_cum()),
                "strategy_equity": _round(eq) if eq is not None else None,
                "hwm": (_round(self.risk_equity_reference())
                        if self.risk_equity_reference() is not None else None),
                "ledger_generation": self.generation}

    def save(self) -> bool:
        """Commit in a fixed order: EVIDENCE FIRST, then state.

        1. advance the watermark in memory;
        2. append the evidence to the append-only continuity chain and fsync
           it -- this is the record that must survive a rewind of everything
           else, so it cannot be written second;
        3. write the ledger through a FENCED atomic replace: a writer that
           no longer holds the current generation is refused rather than
           allowed to clobber newer state.

        A crash between 2 and 3 leaves the chain ahead of the ledger. That is
        recoverable and safe: the chain only ever states that history existed,
        so the next load re-derives the watermark from a journal that still
        contains it. A crash between 1 and 2 loses nothing durable.
        """
        if self.readonly:
            log.info("[EQUITY] readonly instance: durable write skipped "
                     "(inspection path, no state mutated)")
            return False
        if not all_finite(self.state):
            log.critical("[EQUITY] refusing to persist non-finite economic "
                         "state (A16); nothing written")
            return False
        self._advance_journal_watermark()
        if self.seeded and not self._append_evidence():
            self._rollback_to_durable()
            return False
        ok = JsonStore.save(self.path, self.state,
                            expect_generation=self.generation)
        if ok:
            self.generation += 1
            self.state["generation"] = self.generation
            self._mark_durable()
        else:
            log.error("[EQUITY] equity_ledger.json NOT saved "
                      "(persistence sentinel tripped or write fenced off)")
            # A15: a refused write leaves NO authoritative side effect. The
            # in-memory state goes back to the last state that is actually
            # on disk, so "refused" and "applied" can never look the same to
            # a later reader.
            self._rollback_to_durable()
        return ok

    def _mark_durable(self) -> None:
        """Remember the exact state that is now on disk, so a later refused
        write can restore it verbatim."""
        try:
            self._durable = json.loads(json.dumps(self.state))
        except (TypeError, ValueError):                     # noqa: BLE001
            self._durable = None

    def _rollback_to_durable(self) -> None:
        if getattr(self, "_durable", None) is None:
            return
        try:
            self.state = json.loads(json.dumps(self._durable))
        except (TypeError, ValueError):                     # noqa: BLE001
            pass

    def _source_versions(self) -> dict:
        """The exact versions of every source a validated decision rests on.

        Audit findings A03 and A07. Validating a migration or a rebase and
        then writing it are two moments, and everything the validation
        looked at can move in between: a settlement lands, another writer
        commits, the continuity chain grows. Binding those versions and
        re-checking them immediately before the durable write is what makes
        the decision apply to the state it was actually taken on.
        """
        rows = self.settled()
        try:
            head = self.chain.head_pointer()
        except ChainError as e:
            head = {"seq": None, "hash": f"unreadable: {e}"}
        return {"settled_count": len(rows),
                "journal_digest": journal_digest(rows),
                "continuity_seq": head.get("seq"),
                "continuity_hash": head.get("hash"),
                "ledger_generation": self.generation,
                "durable_generation": read_generation(self.path),
                "open_positions": (self.posmgr.open_count()
                                   if self.posmgr is not None else 0)}

    def _shadow(self, prepared: dict):
        """A ledger view over a PREPARED state.

        The commit path has to advance the watermark and compute continuity
        evidence for the state it is about to write. Doing that by assigning
        `self.state = prepared` is exactly the premature publication of
        A14: every other reader in the process would see the new HWM, the
        released hold and the consumed token while the durable state still
        says otherwise. The shadow gives those helpers the prepared state to
        work on without any of them becoming visible.
        """
        sh = object.__new__(EquityLedger)
        sh.__dict__.update(self.__dict__)
        sh.state = prepared
        return sh

    def _append_evidence(self) -> bool:
        """Append this state's evidence to the chain. False = do not commit:
        an unrecordable evidence is not a reason to proceed anyway."""
        try:
            payload = self._continuity_payload()
            floor = self.chain.evidence_floor()
            head = self.chain.head_pointer()  # raises on a broken chain
            if floor is None or payload["settled_count"] > floor["settled_count"] \
                    or (payload["settled_count"] == floor["settled_count"]
                        and payload["strategy_equity"] is not None
                        and (floor["strategy_equity"] is None
                             or payload["strategy_equity"] < floor["strategy_equity"])) \
                    or head["seq"] == 0:
                rec = self.chain.append(KIND_EVIDENCE, payload, now_iso())
                head = {"seq": rec["seq"], "hash": rec["hash"]}
            self.state["continuity"] = head
            return True
        except ChainError as e:
            from persistence import PersistenceSentinel
            log.critical(f"[EQUITY] continuity evidence NOT durable: {e} -- "
                         f"ledger write ABANDONED (a state whose evidence "
                         f"cannot be recorded must not become authoritative)")
            PersistenceSentinel.record_failure(self.chain.path, str(e))
            return False

    def _record_consumed_token(self, token: str, kind: str, when: str) -> bool:
        """A01 rule 9: a consumed authorization is recorded in the chain, so
        restoring a ledger snapshot from before the consumption does not make
        the token usable again."""
        if self.readonly:
            return False
        try:
            self.chain.append(KIND_TOKEN, {"token_sha256": _sha(token),
                                           "action": kind}, when)
            return True
        except ChainError as e:
            log.critical(f"[EQUITY] consumed token NOT durable: {e} -- "
                         f"action refused")
            return False

    def token_consumed(self, token: str) -> bool:
        """True when this exact token was consumed, according to EITHER the
        ledger or the chain. The chain outranks the ledger: it is the copy a
        snapshot restore does not rewind."""
        if token in (self.state.get("consumed_tokens") or []):
            return True
        try:
            return _sha(token) in self.chain.consumed_tokens()
        except ChainError:
            # An unreadable chain cannot clear a token. Fail closed.
            return True

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

    def settled_unique(self) -> list:
        """Settled rows with each economic identity counted ONCE (A04).

        The first occurrence wins: a later copy carries no new economic
        evidence, so it cannot add PnL. This is what makes a replayed
        profitable trade unable to improve the historical drawdown -- the
        CAPITAL guard blocks the account, but the reported number has to
        stay honest too, because that number is what an operator reads.
        """
        seen, out = set(), []
        for t in self.settled():
            keys = _event_keys(t)
            if any(k in seen for k in keys):
                continue
            seen.update(keys)
            out.append(t)
        return out

    def strategy_equity_conservative(self):
        eq = self.strategy_equity()
        if eq is None:
            return None
        if self.duplicate_events():
            # count the duplicated event once and keep the WORSE of the two
            # readings: a replay never improves history
            s = self.state["seed"]
            unique_pnl = float(sum(t.get("net_pnl") or 0.0
                                   for t in self.settled_unique()))
            eq = min(eq, float(s["strategy_equity_0"])
                     + (unique_pnl - float(s["realized_pnl_cum_0"])))
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
        if self.state.get("continuity_block"):
            # A01: the same bound, taken from the authority the rewind did
            # not reach. Without it a restored journal+ledger pair reports a
            # drawdown of zero and the loss is gone from every number.
            floor = self._chain_floor_safe() or {}
            fl = floor.get("strategy_equity")
            if fl is not None and _finite(fl):
                eq = min(eq, float(fl))
        return eq

    def flows_cum(self) -> float:
        """Movements that ENTER the accounting identity (deposits,
        withdrawals, rounding). An unclassified movement is deliberately
        absent: it is not yet known to be external."""
        return float(sum(float(f["amount"]) for f in self.state["flows"]
                         if f.get("kind") in FLOW_KINDS_COUNTED))

    def accounted_flows_cum(self) -> float:
        """Movements already RECORDED against the balance, whatever their
        classification (audit finding A11).

        `flows_cum` answers "what do I count as external?"; this answers
        "what have I already written down?". Conflating the two is what made
        one unresolved -1 withdrawal become three -1 flows: an unclassified
        row does not enter `flows_cum`, so the expected balance never moved,
        so the SAME residual reappeared every k quiet cycles and was
        recorded again as a new economic event.

        `initial_stake` is excluded because it was folded into the seed, and
        `loss_by_correction` because the journal now carries that PnL: in
        both cases the expectation already moved by that amount elsewhere.
        """
        kinds = set(FLOW_KINDS_COUNTED) | {FLOW_UNCLASSIFIED}
        return float(sum(float(f["amount"]) for f in self.state["flows"]
                         if f.get("kind") in kinds))

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
        if self.state.get("continuity_block"):
            return
        rows = self.settled()
        # A04: extension validates IDENTITY, not just count and digest. A
        # replayed profitable trade keeps the count growing and the digest
        # self-consistent while quietly improving the historical drawdown.
        if self.duplicate_events():
            log.error("[EQUITY] watermark NOT extended: the journal counts "
                      "the same economic event more than once")
            return
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

    # ── continuity verification against the independent chain (A01) ─────
    def _continuity_reason(self):
        """Why continuity cannot be proven right now, or None.

        The chain is a FLOOR, never a source of values: it states what was
        once durably true so that anything claiming less is refused. The
        journal, the ledger and the consumed-token set must all be at or
        above it. Being merely *ahead* of the chain pointer is normal (a
        crash between the two commit steps); being *below* the chain is a
        rollback and blocks.
        """
        ok, why = self.chain.healthy()
        if not ok:
            return f"continuity chain unusable: {why}"
        try:
            floor = self.chain.evidence_floor()
        except ChainError as e:
            return f"continuity chain unusable: {e}"
        if floor is None:
            # No evidence was ever recorded. A ledger that claims seeded
            # history without a single chain record is a ledger whose
            # continuity file was removed with the rest of the rewind.
            if self.seeded and (self.state.get("journal_watermark") or {}).get("settled_count"):
                return ("the ledger claims evidenced trading history but the "
                        "continuity chain holds no record of it")
            return None
        if self.schema_reject:
            return (f"the persisted ledger was refused ({self.schema_reject}) "
                    f"while the chain evidences {floor['settled_count']} "
                    f"settled rows")
        if not self.seeded:
            return (f"the ledger is unseeded while the chain evidences "
                    f"{floor['settled_count']} settled rows")
        rows = self.settled()
        n = int(floor["settled_count"])
        if len(rows) < n:
            return (f"the journal holds {len(rows)} settled rows, the chain "
                    f"durably evidenced {n} (restored or truncated journal)")
        if floor.get("digest") and n and journal_digest(rows[:n]) != floor["digest"]:
            return (f"the first {n} settled rows differ from the durably "
                    f"evidenced history (replaced journal)")
        wm = self.state.get("journal_watermark") or {}
        wm_n = wm.get("settled_count")
        if not isinstance(wm_n, int) or wm_n < n:
            # rules 1 and 2: a missing or zeroed watermark on state that the
            # chain proves was reconciled is NOT "no history".
            return (f"the ledger watermark ({wm_n!r}) is below the durably "
                    f"evidenced {n} settled rows")
        ptr = self.state.get("continuity") or {}
        try:
            head = self.chain.head_pointer()
        except ChainError as e:
            return f"continuity chain unusable: {e}"
        if isinstance(ptr.get("seq"), int) and ptr["seq"] > head["seq"]:
            return (f"the ledger was written against continuity record "
                    f"{ptr['seq']}, the chain now ends at {head['seq']} "
                    f"(the chain was truncated)")
        try:
            chain_tokens = self.chain.consumed_tokens()
        except ChainError as e:
            return f"continuity chain unusable: {e}"
        mine = {_sha(t) for t in (self.state.get("consumed_tokens") or [])}
        missing = chain_tokens - mine
        if missing:
            return (f"{len(missing)} consumed authorization token(s) are "
                    f"recorded in the chain but absent from the ledger "
                    f"(state restored to before they were consumed)")
        if self.from_backup:
            # Rule 7. A rotation copy answered instead of the primary file,
            # so the primary was unreadable or its checksum did not match.
            # The copy may be perfectly consistent -- it usually is -- but
            # "usually consistent" is not "proven current": whatever the
            # primary held has just been discarded unread. That is exactly
            # the shape a rewind takes, so it blocks for this boot and
            # clears on the next load once the primary is authoritative
            # again. CAPITAL is never granted on state nobody can date.
            return (f"the ledger was recovered from "
                    f"{os.path.basename(self.from_backup)}: the primary file "
                    f"was unusable, so this state is not proven current")
        return None

    def _check_continuity(self) -> bool:
        """Set or clear the blocking recovery state. True when it changed.

        Clearing is not a timeout and not an operator flag: it happens only
        when the reason is gone, i.e. when the journal and the ledger again
        contain everything the independent chain says was durably observed.
        That is the "independently reconstructed and verified" exit.
        """
        reason = self._continuity_reason()
        was = self.state.get("continuity_block")
        if reason and not was:
            self.state["continuity_block"] = {
                "detected_at": now_iso(), "reason": reason,
                "chain_head": self.chain.head_pointer_safe(),
                "evidenced": self._chain_floor_safe()}
            self._note_unproven("continuity with durably observed evidence "
                                "cannot be proven: " + reason)
            log.critical(f"[EQUITY] CONTINUITY ROLLBACK: {reason}; "
                         f"risk_equity_status=UNRECONCILED, CAPITAL blocked, "
                         f"rebase refused, evidenced losses preserved")
            return True
        if reason and was and was.get("reason") != reason:
            self.state["continuity_block"]["reason"] = reason
            return True
        if not reason and was:
            self.state["continuity_block"] = None
            unproven = self.state["status_basis"].get("unproven") or []
            self.state["status_basis"]["unproven"] = [
                u for u in unproven
                if not u.startswith("continuity with durably observed")]
            if not self.readonly:
                try:
                    self.chain.append(KIND_RECOVERY, {
                        "cleared": was.get("reason"),
                        "verified_against_seq":
                            self.chain.head_pointer_safe().get("seq"),
                    }, now_iso())
                except ChainError:
                    pass
            log.warning("[EQUITY] continuity re-established: the evidenced "
                        "history is present again in journal and ledger")
            return True
        return False

    def _chain_floor_safe(self):
        try:
            return self.chain.evidence_floor()
        except ChainError:
            return None

    def continuity_blocked(self) -> bool:
        return bool(self.state.get("continuity_block"))

    # ── A04: economic event identity ────────────────────────────────────
    def duplicate_events(self) -> list:
        """Settled rows sharing one economic identity. A journal that counts
        the same trade twice makes a loss look smaller; the aggregate stays
        plausible, which is exactly why the count and the digest alone never
        detect it (audit finding A04)."""
        seen, dupes = {}, []
        for t in self.settled():
            # A04: one definition of economic identity, shared with the
            # journal that writes the rows. Local ids alone cannot detect a
            # replay, because a replay mints a new one.
            keys = _event_keys(t)
            for key in keys:
                if key in seen:
                    dupes.append({"key": "%s=%s" % key, "first_index": seen[key],
                                  "net_pnl": t.get("net_pnl")})
                else:
                    seen[key] = len(seen)
        return dupes

    def derive_status(self) -> str:
        """Re-derive; automatic moves are conservative only."""
        if not self.seeded or self.schema_reject:
            return STATUS_UNRECONCILED
        # Every blocking reason is NOTED before any of them returns: an
        # operator reading `unproven` must see all of them, not only the one
        # that happened to be evaluated first.
        blocked = bool(self.state.get("continuity_block"))
        if not self._seed_prefix_intact():
            self._note_unproven("journal does not contain the seed prefix "
                                "(restored journal older than the ledger?)")
            blocked = True
        if self.state.get("journal_mismatch"):
            blocked = True
        if blocked:
            return STATUS_UNRECONCILED
        if self.duplicate_events():
            self._note_unproven("the journal counts the same economic event "
                                "more than once")
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
        # A19: freshness before anything else. Every number below is derived
        # from a view of authority; if another writer has already advanced
        # that authority, this view describes a world that no longer exists
        # and no amount of internal consistency makes it admissible.
        if not self.authority_is_current()[0]:
            out.append(GUARD_STALE_READER)
        # A20: state that belongs to another account or environment is not
        # this account's risk history, however internally consistent it is.
        if self.binding_status in ("mismatch", "credential_changed"):
            out.append(GUARD_ACCOUNT_BINDING)
        # A16: a non-finite value anywhere in economic state. Checked
        # explicitly rather than by comparison, because NaN > x and NaN < x
        # are both False -- every threshold silently admits it.
        if not self.economic_values_finite():
            out.append(GUARD_NON_FINITE)
        # A01: continuity first. Everything below reasons about numbers; if
        # the state those numbers come from may have been rewound, no number
        # is admissible, whatever it says.
        if self.state.get("continuity_block"):
            out.append(GUARD_CONTINUITY)
        # A05: an accounting mode this build does not implement makes the
        # percentages meaningless. It is never a reason to use another
        # denominator; it is a reason to be ineligible.
        if not accounting_mode_capital_admissible():
            out.append(GUARD_ACCOUNTING_MODE)
        # A04: the same economic event counted twice flatters the drawdown.
        if self.seeded and self.duplicate_events():
            out.append(GUARD_JOURNAL_INTEGRITY)
        if not self.seeded:
            out.append(GUARD_UNSEEDED)
        else:
            if self.unclassified_flows():
                out.append(GUARD_FLOW_UNRESOLVED)
            st = self.derive_status()
            allow_cons = bool(getattr(CFG, "RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE", False))
            if st == STATUS_UNRECONCILED or (st == STATUS_CONSERVATIVE and not allow_cons):
                out.append(GUARD_UNRECONCILED)
        # A06: OBSERVABLE and RECONCILED are not CAPITAL_ADMISSIBLE. A cash
        # movement under observation reconciles numerically -- that is what
        # the epsilon is for -- but an unexplained one has no durable
        # explanation yet, and an unexplained ADVERSE movement is
        # indistinguishable from an unrecorded loss until it has one.
        if self.seeded and self._unexplained_residual() is not None:
            out.append(GUARD_RESIDUAL_UNEXPLAINED)
        if self.state.get("capital_hold"):
            out.append(GUARD_CAPITAL_HOLD)
        return out

    def _unexplained_residual(self):
        """The adverse balance movement currently lacking an explanation, or
        None. The numerical tolerance for API rounding (`eps_base`) stays a
        ROUNDING tolerance: it is not allowed to grow into a tolerance for
        unexplained economic loss, so anything beyond it counts here from the
        FIRST observation, not after k quiet cycles."""
        # A06: the FLOOR outranks the live pending row. `pending` is cleared
        # by any cycle whose residual lands inside the (growing) epsilon and
        # is never written at all on a non-quiet cycle, so reading only
        # `pending` means a noisy account can drop adverse evidence for ever.
        # The floor is a separate, sticky record of the worst adverse
        # residual ever measured, cleared only by classification.
        floor = self.state.get("adverse_residual_floor") or {}
        fr = floor.get("residual")
        if fr is not None and _finite(fr) and float(fr) < -abs(self.eps_base):
            return {"residual": _round(fr), "since": floor.get("first_seen_at"),
                    "cycles": floor.get("observations"),
                    "source": "adverse_floor"}
        pend = self.state.get("pending") or {}
        r = pend.get("residual")
        if r is None or not _finite(r):
            return None
        if float(r) < -abs(self.eps_base):
            return {"residual": _round(r), "since": pend.get("first_seen_at"),
                    "cycles": pend.get("consecutive")}
        return None

    def _record_adverse_floor(self, r, when, cycle_n) -> None:
        """Remember the worst adverse residual ever measured (A06).

        Measurement tolerance and economic admissibility are different
        questions. Epsilon exists so the engine does not alert on API
        rounding; it must not double as permission to forget a loss. Any
        residual beyond the ROUNDING tolerance (`eps_base`, never the grown
        per-trade epsilon) is recorded here the first time it is seen, on a
        quiet cycle or not, and stays until something explains it.
        """
        try:
            r = check_finite(r, "cash residual")
        except NonFiniteValue:
            return
        if r >= -abs(self.eps_base):
            return
        floor = self.state.get("adverse_residual_floor") or None
        if floor is None:
            self.state["adverse_residual_floor"] = {
                "residual": _round(r), "first_seen_at": when,
                "first_seen_cycle": cycle_n, "worst_at": when,
                "observations": 1}
            log.error(f"[EQUITY] adverse cash residual {r:+.4f}$ recorded as "
                      f"UNEXPLAINED (floor): tolerance is for rounding, not "
                      f"for loss -- CAPITAL ineligible until classified")
            return
        floor["observations"] = int(floor.get("observations") or 1) + 1
        if r < float(floor.get("residual", 0.0)):
            floor["residual"] = _round(r)
            floor["worst_at"] = when

    def _clear_adverse_floor_if_explained(self) -> None:
        """The floor clears only when nothing is unexplained any more: every
        flow classified and no pending residual beyond the rounding
        tolerance. It is never cleared by the tolerance growing."""
        if self.unclassified_flows():
            return
        pend = self.state.get("pending") or {}
        r = pend.get("residual")
        if r is not None and _finite(r) and float(r) < -abs(self.eps_base):
            return
        if self.state.get("adverse_residual_floor"):
            log.warning("[EQUITY] adverse residual floor cleared: every "
                        "movement is classified and no adverse residual "
                        "remains")
        self.state["adverse_residual_floor"] = None

    # ── freshness and finiteness (A19, A16) ─────────────────────────────
    def authority_is_current(self) -> tuple:
        """(ok, reason). False when the durable generation has moved past
        the one this instance holds (A19).

        A reader that loaded generation N and kept deciding while another
        process committed N+1 is not "slightly behind": it is authoritative
        about a state that has been superseded. Read directly from disk --
        the point is to see what is there now, not what this process
        remembers.
        """
        try:
            on_disk = read_generation(self.path)
        except Exception:                                   # noqa: BLE001
            return False, "authority generation unreadable"
        if on_disk is None:
            return True, None
        if int(on_disk) > int(self.generation):
            return False, (f"STALE_READER: durable generation {on_disk} is "
                           f"ahead of the {self.generation} held here")
        return True, None

    def require_current_authority(self, what: str) -> None:
        """Raise StaleAuthority when this view is behind. Called before any
        safety-critical transition, never after it."""
        ok, reason = self.authority_is_current()
        if not ok:
            log.critical(f"[EQUITY_STALE_READER] {what} refused: {reason}")
            raise StaleAuthority(reason)

    def economic_values_finite(self) -> bool:
        """A16: no NaN/Inf anywhere in persisted economic state or in the
        journal-derived quantities the guards compare."""
        # A16: a REFUSED input is itself the evidence. The balance that
        # could not be read is not in the state (it was refused), so a scan
        # of stored values alone would report everything finite and let
        # CAPITAL stay eligible on an account whose balance is unknown.
        if self.state.get("non_finite_input"):
            return False
        if not all_finite(self.state):
            return False
        for value in (self.realized_pnl_cum(), self.open_cost_basis()):
            if not all_finite(value):
                return False
        if self.seeded and not all_finite(self.strategy_equity()):
            return False
        return True

    def capital_eligible(self) -> bool:
        return not self.guards()

    # ── the per-cycle observation (design §5) ───────────────────────────
    #: A06: the per-trade term exists because each settlement can carry a
    #: sub-cent rounding difference, so the acceptable rounding error grows
    #: with the number of settlements since the last exact match. It is a
    #: MEASUREMENT tolerance and it is capped: without a cap it grows by
    #: $0.005 per settled trade for ever, and after a few hundred trades it
    #: quietly becomes a tolerance for economic loss -- which is what Astra
    #: measured. The cap is deliberately small relative to any real loss.
    EPSILON_CAP = 0.25

    #: A11: how many observation cycles an unclassified flow may go unseen
    #: and still be considered "the same movement still being observed".
    #: Beyond it, an equal amount is a DISTINCT economic event and gets its
    #: own row, so two legitimate equal movements are never merged into one.
    FLOW_CONTINUITY_CYCLES = 2

    def _epsilon(self) -> float:
        n = int(self.state.get("anchor", {}).get("settled_since_anchor") or 0)
        grown = self.eps_base + self.eps_per_trade * n
        return min(grown, max(abs(self.eps_base), self.EPSILON_CAP))

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
        # A16: a NaN balance must never enter the accounting. It cannot be
        # caught downstream by comparison, because every comparison against
        # NaN is False -- including the guard thresholds.
        try:
            cash = check_finite(cash, "broker cash balance")
        except NonFiniteValue as e:
            log.critical(f"[EQUITY] observation REFUSED: {e} -- the balance "
                         f"is not a number, so no accounting is possible "
                         f"and CAPITAL is ineligible")
            self.state["non_finite_input"] = {"at": when, "what": str(e)}
            return self.snapshot()
        self.state.pop("non_finite_input", None)
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
                    + self.accounted_flows_cum())
        r = (cash + basis) - expected
        eps = self._epsilon()
        pend = self.state.get("pending")
        # A06: record adverse evidence BEFORE deciding what to do with the
        # measurement. Whether the cycle is calm, and whether the tolerance
        # has grown, are questions about measurement noise. Whether money
        # went missing is not. The floor is written first so that no branch
        # below can discard it.
        self._record_adverse_floor(r, when, cycle_n)
        if abs(r) <= eps:
            # An ADVERSE residual is never folded into `rounding` on the
            # strength of a grown epsilon: rounding is symmetric noise, a
            # one-sided loss is not. Only movements within the base rounding
            # tolerance are auto-classified.
            if abs(r) > 1e-9 and (r > 0 or abs(r) <= abs(self.eps_base)):
                self._append_flow(r, FLOW_ROUNDING, "auto", when, cycle_n, {
                    "cash": cash, "open_cost_basis": basis, "epsilon": eps})
                self.state["pending"] = None
                self.state["anchor"] = {"settled_since_anchor": 0, "at": when}
            elif abs(r) <= 1e-9:
                self.state["pending"] = None
                self.state["anchor"] = {"settled_since_anchor": 0, "at": when}
            else:
                # adverse, inside the GROWN epsilon but outside the rounding
                # tolerance: keep it visible instead of absorbing it.
                self.state["pending"] = {"residual": _round(r), "first_seen_cycle": cycle_n,
                                         "consecutive": 1, "first_seen_at": when}
        elif not quiet:
            # A race between the broker credit and the journal write can
            # explain a residual, so no flow is recorded here -- but the
            # adverse floor above has already remembered it, so a permanently
            # noisy account can no longer drop the evidence for ever.
            pass
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
        """Record a balance movement ONCE.

        A11: an unresolved residual is observed on every cycle until an
        operator classifies it. Re-observing it is not a new economic event,
        so an open unclassified row for the same amount is UPDATED (last
        seen, observation count) instead of a second row being appended.
        Every other kind is a one-off by construction: the expectation moves
        with it, so the residual it explains does not come back.
        """
        if kind == FLOW_UNCLASSIFIED:
            existing = self._open_unclassified_like(amount, when, cycle_n)
            if existing is not None:
                existing["last_seen_at"] = when
                existing["last_seen_cycle"] = cycle_n
                existing["observations"] = int(existing.get("observations") or 1) + 1
                existing["last_evidence"] = evidence
                log.warning(f"[EQUITY_FLOW] {existing['id']} re-observed "
                            f"({existing['observations']}x, {existing['amount']:+.4f}$): "
                            f"same unresolved movement, NOT a new economic event")
                return existing
        row = {"id": self._next_flow_id(), "at": when, "amount": _round(amount),
               "kind": kind, "classified_by": classified_by, "cycle": cycle_n,
               "evidence": evidence, "note": "",
               "first_seen_at": when, "last_seen_at": when,
               "last_seen_cycle": cycle_n, "observations": 1,
               "balance_snapshot": (evidence or {}).get("cash")}
        self.state["flows"].append(row)
        if kind != FLOW_ROUNDING:
            log.warning(f"[EQUITY_FLOW] {row['id']} kind={kind} amount={row['amount']:+.4f}$ "
                        f"classified_by={classified_by}; strategy_equity unchanged")
        return row

    def _open_unclassified_like(self, amount, when=None, cycle_n=None):
        """The still-unclassified row this movement is a re-observation of.

        Identity is the amount within the rounding tolerance: the broker
        gives no reference for an unexplained balance delta, so the amount
        plus "still unresolved" is the only honest key. Two genuinely
        different unresolved movements of the same size collapse into one
        row here -- which is why the row carries its observation count and
        both timestamps, and why CAPITAL stays blocked until an operator
        classifies it against real funding records.
        """
        eps = abs(self.eps_base)
        for f in self.state["flows"]:
            if f.get("kind") != FLOW_UNCLASSIFIED:
                continue
            if abs(float(f["amount"]) - _round(amount)) > eps:
                continue
            # A11: same amount is NOT the same event. Idempotency has to
            # suppress the SAME movement observed repeatedly, without
            # collapsing two genuinely distinct movements that happen to be
            # equal -- two $25 withdrawals a week apart are two withdrawals.
            # What distinguishes "still observing the one residual" from "a
            # second movement of the same size" is continuity of
            # observation: the open row is still being seen, cycle after
            # cycle. A row that stopped being observed and is seen again
            # later is a NEW event.
            if cycle_n is not None:
                last_cycle = f.get("last_seen_cycle")
                if isinstance(last_cycle, int) and isinstance(cycle_n, int) \
                        and cycle_n - last_cycle > self.FLOW_CONTINUITY_CYCLES:
                    continue
            return f
        return None

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
        # A07 rule 3: the proposal is bound to the EXACT evidence it was
        # computed on -- the source state root of every local file, the
        # ledger generation, the settlement frontier, the cash observation
        # and the schema. Any of them moving invalidates the hash, so the
        # boot recomputation cannot silently apply a proposal to a different
        # world than the one the operator reviewed.
        proposal["bound_state"] = self.bound_state()
        proposal["settlement_frontier"] = {
            "settled_count": len(rows), "digest": journal_digest(rows),
            "last_settled_at": max([t.get("settled_at") or "" for t in rows],
                                   default=None)}
        proposal["schema_version"] = SCHEMA_VERSION
        proposal["continuity_head"] = self.chain.head_pointer()
        proposal["sha256"] = self.seed_proposal_sha(proposal)
        return proposal

    @staticmethod
    def seed_proposal_sha(proposal: dict) -> str:
        """The hash of the DATA, recomputed from the object in hand.

        Audit finding A07 rule 2: `apply_seed` used to compare the operator's
        expected hash with `proposal["sha256"]` -- a value stored INSIDE the
        mutable object it was supposed to authenticate. Editing any other
        field while leaving that one alone produced a modified proposal that
        still carried its previously accepted hash.
        """
        return _sha(_canonical({k: v for k, v in proposal.items()
                                if k != "sha256"}))

    def apply_seed(self, proposal: dict, expected_sha: str, when: str = None) -> bool:
        """Apply a migration seed, re-deriving and re-verifying everything.

        A migration may never make equity, the HWM or the drawdown look
        safer because the evidence moved underneath it. Astra settled a -3
        loss between the proposal and its application: the loss became part
        of the new seed prefix, equity and HWM stayed at 10 and the drawdown
        read 0. So the proposal is recomputed from live data here, and both
        the recomputed proposal and the caller's must hash to what the
        operator authorized.
        """
        if self.seeded:
            log.warning("[EQUITY_SEED] refused: a seed already exists (never overwritten)")
            return False
        if not isinstance(proposal, dict):
            log.warning("[EQUITY_SEED] refused: proposal is not an object")
            return False
        recomputed_sha = self.seed_proposal_sha(proposal)
        if not expected_sha or recomputed_sha != expected_sha:
            log.warning(f"[EQUITY_SEED] refused: hash mismatch (recomputed from the "
                        f"proposal's own data: {recomputed_sha}; stored field: "
                        f"{proposal.get('sha256')}; expected: {expected_sha})")
            return False
        if self.state.get("continuity_block"):
            log.warning("[EQUITY_SEED] refused: continuity rollback open")
            return False
        # A07 rules 4 and 5: revalidate against the state as it is NOW.
        try:
            fresh = self.propose_seed(proposal["pre_flow_cash"],
                                      proposal["pre_flow_at"],
                                      proposal["evidence_ref"],
                                      proposal["cash_now"],
                                      proposal.get("open_cost_basis_now"))
        except (KeyError, TypeError, ValueError) as e:
            log.warning(f"[EQUITY_SEED] refused: proposal cannot be recomputed: {e}")
            return False
        if fresh["sha256"] != expected_sha:
            drift = [k for k in sorted(set(fresh) | set(proposal))
                     if fresh.get(k) != proposal.get(k) and k != "sha256"]
            log.warning(f"[EQUITY_SEED] refused: the evidence changed since the "
                        f"proposal was authorized ({drift}) -- regenerate the "
                        f"proposal and review it again; a migration never "
                        f"absorbs a change it was not shown")
            return False
        proposal = fresh
        when = when or now_iso()
        status = proposal["risk_equity_status"]
        # A07: the migration is prepared on a COPY and bound to the exact
        # source versions it was validated against. An apply that fails --
        # a full disk, a stale generation, a settlement at the boundary --
        # leaves an UNSEEDED ledger, not a half-migrated one.
        bind = self._source_versions()
        prepared = json.loads(json.dumps(self.state))
        sh = self._shadow(prepared)
        prepared["seed"] = sh._seed_dict(
            when, proposal["source"], proposal["account_equity_0"],
            proposal["strategy_equity_0"], proposal["hwm_0"], status,
            {k: proposal[k] for k in ("pre_flow_cash", "pre_flow_at", "evidence_ref",
                                      "cash_now", "open_cost_basis_now",
                                      "realized_pnl_cum", "settled_rows",
                                      "rolling_drawdown")},
            "EQUITY_LEDGER_SEED_SHA256")
        prepared["hwm"] = {"risk_equity_reference": proposal["hwm_0"], "at": when,
                           "rebased_from": None, "floor_from_settled_index": 0}
        prepared["risk_equity_status"] = status
        prepared["status_basis"] = {
            "set_at": when, "set_by": "migration",
            "provenance_window": {"from": proposal["pre_flow_at"], "to": None},
            "unproven": list(proposal["unproven"]),
            "evidence_sha256": prepared["seed"]["evidence_sha256"]}
        mf = proposal.get("migration_flow")
        if mf:
            sh._append_flow(mf["amount"], mf["kind"],
                            "migration" if mf["kind"] == FLOW_DEPOSIT else None,
                            when, 0, {"pre_flow_cash": proposal["pre_flow_cash"],
                                      "cash_now": proposal["cash_now"],
                                      "evidence_ref": proposal["evidence_ref"]})
        sh._roll_day(when)
        sh._reconcile_status()
        ok = self._commit(prepared, bind=bind)
        if not ok:
            log.error("[EQUITY_SEED] NOT applied: the ledger stays UNSEEDED "
                      "(nothing partially migrated, in memory or on disk)")
            return False
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
    def bound_state(self) -> dict:
        """Fingerprint of every local file whose content can change what a
        rebase or a seed is allowed to do (audit finding A03).

        The old `evidence_sha256` bound the journal, the ledger and the
        positions. It did NOT bind the orders or the pending intents, so a
        second process could add a resting order between validation and
        commit and the authorization hash stayed identical. Everything the
        preconditions read is bound here, per file, so the caller can see
        WHICH one moved.

        The ledger itself is deliberately absent: this instance owns it, and
        a concurrent writer to it is caught by the fencing generation, which
        is a stronger check than a hash (it refuses the write outright).
        """
        out = {name: file_fingerprint(_p(name)) for name in BOUND_STATE_FILES
               if name != LEDGER_FILE}
        out["ledger_generation"] = self.generation
        return out

    def evidence_sha256(self) -> str:
        """One hash over the whole bound state, for proposals and tokens."""
        return _sha(_canonical(self.bound_state()))

    def _bound_state_drift(self, captured) -> list:
        """Which bound files changed since `captured` was taken. Empty list
        means the authorizing state is still the committing state."""
        if not isinstance(captured, dict):
            return ["no bound state captured with the authorization"]
        now = self.bound_state()
        return [f"{k}: {captured.get(k)!r} -> {now.get(k)!r}"
                for k in sorted(set(now) | set(captured))
                if captured.get(k) != now.get(k)]

    def rebase_preconditions(self, ctx: dict) -> list:
        """ctx: drawdown_firing, reconcile_status, open_positions, in_flight_orders."""
        failures = []
        if self.state.get("continuity_block"):
            failures.append("continuity rollback: "
                            f"{self.state['continuity_block'].get('reason')}")
        if not accounting_mode_capital_admissible():
            failures.append(f"accounting mode {accounting_mode()!r} is not "
                            f"CAPITAL-admissible (expected one of "
                            f"{list(CAPITAL_ADMISSIBLE_MODES)})")
        if self.duplicate_events():
            failures.append("the journal counts the same economic event twice")
        if self._unexplained_residual() is not None:
            failures.append("an unexplained cash residual is under observation")
        # A03: the evidence must have been collected while nothing moved, and
        # the collector must say so. A context that does not carry its own
        # stability proof is not an authorization.
        if not isinstance(ctx, dict):
            failures.append("no rebase context provided")
            return failures
        if ctx.get("evidence_unstable"):
            failures.append(f"execution state moved while the rebase evidence "
                            f"was collected: {ctx.get('evidence_unstable')}")
        if "bound_state" not in ctx:
            failures.append("the context carries no bound local state "
                            "(orders/intents/journal/positions versions)")
        elif self._bound_state_drift(ctx.get("bound_state")):
            failures.append(f"bound local state changed since the evidence "
                            f"was collected: {self._bound_state_drift(ctx['bound_state'])}")
        if ctx.get("quiescent") is not True:
            failures.append("execution is not quiescent (a rebase is only "
                            "applied at boot, before any cycle runs)")
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
        """PREPARE -> VALIDATE -> RE-VALIDATE -> COMMIT -> PUBLISH.

        Audit findings A03 and A10. The old order was "mutate the
        authoritative in-memory state, then try to persist it": a failed
        write returned False having already lowered the HWM, consumed the
        token and installed the hold in memory, so "refused" and "applied"
        looked identical to every in-process reader. And the authorization
        bound only three files, so execution state could move between the
        checks and the commit without invalidating anything.

        Here nothing authoritative moves until the durable write has
        succeeded. The new state is built on a COPY; the bound local state
        and the caller's broker evidence are re-verified immediately before
        the write; and `self.state` is republished only on success.
        """
        try:
            proposal = self.propose_rebase(reason, action_id)
        except ValueError as e:
            log.warning(f"[EQUITY_REBASE] refused: {e}"); return False
        if self.token_consumed(token):
            log.warning("[EQUITY_REBASE] refused: token already consumed (replay)"); return False
        if token != proposal["token"]:
            log.warning("[EQUITY_REBASE] refused: token does not match the recomputed proposal"); return False
        failures = self.rebase_preconditions(ctx)
        if failures:
            log.warning(f"[EQUITY_REBASE] refused: {failures}"); return False
        # A19: authority freshness is a precondition like any other, and it
        # is re-checked inside _commit immediately before the write.
        try:
            self.require_current_authority("rebase")
        except StaleAuthority as e:
            log.warning(f"[EQUITY_REBASE] refused: {e}"); return False

        # RE-VALIDATE. Two sequential broker GETs are not a transaction, and
        # neither is a check followed by a write. Whatever the caller can
        # re-read, it re-reads now; whatever it cannot, it must have proven
        # stable while collecting (`evidence_unstable`).
        revalidate = ctx.get("revalidate")
        if callable(revalidate):
            try:
                fresh = revalidate() or {}
            except Exception as e:                        # noqa: BLE001
                log.warning(f"[EQUITY_REBASE] refused: re-validation failed: {e}")
                return False
            again = self.rebase_preconditions(fresh)
            if again:
                log.warning(f"[EQUITY_REBASE] refused at commit: the authorizing "
                            f"state is no longer current: {again}")
                return False
            for key in ("reconcile_status", "open_positions"):
                if fresh.get(key) != ctx.get(key):
                    log.warning(f"[EQUITY_REBASE] refused at commit: {key} changed "
                                f"{ctx.get(key)!r} -> {fresh.get(key)!r}")
                    return False
            if (fresh.get("orders") or {}).get("broker_open_ids") != \
                    (ctx.get("orders") or {}).get("broker_open_ids"):
                log.warning("[EQUITY_REBASE] refused at commit: the broker order "
                            "set changed since authorization")
                return False
        drift = self._bound_state_drift(ctx.get("bound_state"))
        if drift:
            log.warning(f"[EQUITY_REBASE] refused at commit: bound local state "
                        f"changed: {drift}")
            return False

        # PREPARE the new state on a copy. Nothing below this line is visible
        # to any other reader until the durable write returns True.
        when = when or now_iso()
        # A03: bind the sources at the moment the decision is taken, so the
        # commit can prove they have not moved.
        rebase_bind = self._source_versions()
        prior = dict(self.state["hwm"])
        entry = dict(proposal)
        entry.update({"applied_at": when, "token_sha256": _sha(token), "prior_hwm_row": prior,
                      "settled_count_at_rebase": len(self.settled()), "validation": None})
        entry.pop("token", None)
        prepared = json.loads(json.dumps(self.state))
        prepared["rebases"].append(entry)
        prepared["hwm"] = {"risk_equity_reference": proposal["new_baseline"]["hwm"], "at": when,
                           "rebased_from": proposal["rebase_id"],
                           "floor_from_settled_index": len(self.settled())}
        prepared["consumed_tokens"].append(token)
        prepared["capital_hold"] = {"reason": "post_rebase_validation", "since": when,
                                    "rebase_id": proposal["rebase_id"], "released_by": None}
        # The token is burned in the append-only chain BEFORE the state that
        # spends it. A crash here leaves a token that can never be replayed
        # and a rebase that never happened -- the safe half of the pair. The
        # operator issues a new action id; the alternative (burn after) is a
        # token that survives its own consumption.
        if not self._record_consumed_token(token, "rebase", when):
            return False
        ok = self._commit(prepared, bind=rebase_bind)
        if not ok:
            log.error(f"[EQUITY_REBASE] NOT applied: durable write failed; the "
                      f"previous baseline stays authoritative "
                      f"(hwm={prior.get('risk_equity_reference')}, no hold, "
                      f"token {_sha(token)[:12]} burned and unusable)")
            return False
        log.warning(f"[EQUITY_REBASE] id={proposal['rebase_id']} from_hwm={prior.get('risk_equity_reference')} "
                    f"to_hwm={proposal['new_baseline']['hwm']} drawdown_acknowledged="
                    f"{proposal['old_baseline']['drawdown_usd']} operator_action_id={action_id} "
                    f"capital_hold=post_rebase_validation")
        return True

    def _commit(self, prepared: dict, bind: dict = None) -> bool:
        """Durably commit a PREPARED state, then publish it. On failure the
        current state stays authoritative, unchanged, in memory and on disk
        (audit finding A10)."""
        if self.readonly:
            log.info("[EQUITY] readonly instance: commit refused")
            return False
        # A16: validate the numbers before anything durable happens.
        if not all_finite(prepared):
            log.critical("[EQUITY] commit refused: prepared state carries a "
                         "non-finite economic value (A16)")
            return False
        # A19: a commit computed from a superseded view must not overwrite
        # the writer that superseded it. The fence below makes the check and
        # the write one step; this makes the refusal explicit and logged.
        ok_fresh, why = self.authority_is_current()
        if not ok_fresh:
            log.critical(f"[EQUITY] commit refused: {why}")
            return False
        # A14: the prepared state is worked on through a SHADOW view and is
        # published to `self.state` only after the durable write returned
        # True. Until then every reader -- guards, dashboard, sizing --
        # keeps seeing the old HWM, the old hold and the old token set.
        shadow = self._shadow(prepared)
        try:
            shadow._advance_journal_watermark()
            if shadow.seeded and not shadow._append_evidence():
                return False
            # A03/A07 LINEARIZATION POINT. The last thing before the write is
            # a re-check that every source this decision was validated
            # against is still exactly what it was. A settlement that landed
            # in the meantime does not get absorbed into an already-approved
            # migration, and a rebase does not commit against exposure that
            # has moved.
            if bind is not None:
                now_versions = self._source_versions()
                drifted = [k for k in bind
                           if k not in ("ledger_generation", "continuity_seq",
                                        "continuity_hash")
                           and now_versions.get(k) != bind.get(k)]
                if drifted:
                    log.critical(
                        f"[EQUITY] commit REFUSED at the linearization point: "
                        f"{drifted} changed since the decision was validated "
                        f"-- the authorized state is not the state being "
                        f"written; re-authorize against current evidence")
                    return False
            ok = JsonStore.save(self.path, prepared,
                                expect_generation=self.generation)
        except Exception:                                  # noqa: BLE001
            # Nothing was published; self.state was never touched.
            raise
        if not ok:
            return False
        self.generation += 1
        prepared["generation"] = self.generation
        self.state = prepared          # PUBLISH (single atomic rebind)
        self._mark_durable()
        return True

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
        if self.token_consumed(token):
            log.warning("[EQUITY_HOLD_RELEASE] refused: token already consumed"); return False
        if token != proposal["token"]:
            log.warning("[EQUITY_HOLD_RELEASE] refused: token mismatch"); return False
        if self.state.get("continuity_block"):
            log.warning("[EQUITY_HOLD_RELEASE] refused: continuity rollback open"); return False
        # A19: a release computed from a superseded view is not a release.
        try:
            self.require_current_authority("hold release")
        except StaleAuthority as e:
            log.warning(f"[EQUITY_HOLD_RELEASE] refused: {e}"); return False
        when = when or now_iso()
        # A15: PREPARE on a copy. The old code dropped the hold and appended
        # the token in `self.state` and only then tried to persist, so a
        # refused release -- a stale generation, a full disk -- still left an
        # in-memory ledger with no hold and CAPITAL eligible.
        prepared = json.loads(json.dumps(self.state))
        for r in prepared["rebases"]:
            if r.get("rebase_id") == proposal["rebase_id"]:
                r["validation"] = {"operator_action_id": str(action_id),
                                   "validation_ref": str(validation_ref),
                                   "released_at": when, "token_sha256": _sha(token)}
        prepared["capital_hold"] = None
        prepared["consumed_tokens"].append(token)
        if not self._record_consumed_token(token, "hold_release", when):
            return False
        ok = self._commit(prepared)
        if not ok:
            log.error(f"[EQUITY_HOLD_RELEASE] NOT applied: durable write "
                      f"failed; the hold STAYS in force and CAPITAL stays "
                      f"ineligible (token {_sha(token)[:12]} burned)")
            return False
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
                or not self._seed_prefix_intact() or self.state.get("journal_mismatch") \
                or self.duplicate_events():
            log.warning("[EQUITY_ATTEST] refused: ledger is not in an attestable state "
                        "(unseeded, unresolved flow, pending residual or journal mismatch)")
            return False
        if self.token_consumed(token) or token != proposal["token"]:
            log.warning("[EQUITY_ATTEST] refused: token invalid or consumed"); return False
        if self.state.get("continuity_block"):
            log.warning("[EQUITY_ATTEST] refused: continuity rollback open -- an "
                        "attestation cannot vouch for state that may be rewound")
            return False
        # A19: an attestation computed from a superseded view attests to a
        # state that is no longer authoritative.
        try:
            self.require_current_authority("attestation")
        except StaleAuthority as e:
            log.warning(f"[EQUITY_ATTEST] refused: {e}"); return False
        when = when or now_iso()
        # A15: PREPARE on a copy; RECONCILED becomes visible only once the
        # write that records it has succeeded.
        prepared = json.loads(json.dumps(self.state))
        prepared["risk_equity_status"] = STATUS_RECONCILED
        prepared["seed"]["status_at_seed"] = STATUS_RECONCILED
        prepared["status_basis"] = {"set_at": when, "set_by": "operator",
                                    "provenance_window": (prepared.get("status_basis") or {}).get("provenance_window"),
                                    "unproven": [], "evidence_sha256": proposal["evidence_sha256"],
                                    "attestation": {"operator_action_id": str(action_id),
                                                    "funding_records_sha256": str(funding_records_sha256),
                                                    "token_sha256": _sha(token)}}
        prepared["consumed_tokens"].append(token)
        if not self._record_consumed_token(token, "attest", when):
            return False
        ok = self._commit(prepared)
        if not ok:
            log.error("[EQUITY_ATTEST] NOT applied: durable write failed; the "
                      "previous status stays authoritative")
            return False
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
        before = self.state.get("risk_equity_status")
        # A01: the continuity check runs even on an unseeded or refused
        # ledger -- those are precisely the shapes a rewind produces.
        changed = self._check_continuity()
        if not self.seeded:
            if changed:
                self._reconcile_status()
                self.save()
            return
        changed = self._check_journal_against_watermark() or changed
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
            "continuity_block": self.state.get("continuity_block"),
            "duplicate_events": self.duplicate_events(),
            "accounting_mode": accounting_mode(),
            "accounting_mode_valid": accounting_mode_valid(),
            "accounting_mode_capital_admissible": accounting_mode_capital_admissible(),
            "unexplained_residual": self._unexplained_residual(),
            "recovered_from_backup": self.from_backup,
            "ledger_generation": self.generation,
            "continuity_head": self.chain.head_pointer_safe(),
        }

    def banner_line(self) -> str:
        s = self.snapshot()
        return (f"[EQUITY] risk_equity_status={s['risk_equity_status']} "
                f"strategy_equity={s['strategy_equity']} hwm={s['risk_equity_reference']} "
                f"drawdown_pct={s['drawdown_pct']} flows_cum={s['external_flows_cum']} "
                f"capital_guards={s['capital_guards']} capital_eligible={s['capital_eligible']}")
