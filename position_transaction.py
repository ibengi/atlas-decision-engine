"""Commit journal, positions and replay IDs before publishing a settlement."""
import copy
import functools

from config import CFG, _p
from persistence import PersistenceSentinel, file_fingerprint
from state_authority import Transaction, AuthorityError


def position_transaction(method):
    @functools.wraps(method)
    def transact(self, *args, **kwargs):
        if getattr(self, "_position_draft", False):
            return method(self, *args, **kwargs)
        path = _p(CFG.POSITIONS_FILE)
        try:
            with Transaction(path, {CFG.POSITIONS_FILE, CFG.TRADES_FILE,
                                    "seen_fill_ids.json"}, owner=self):
                if self.integrity_error:
                    raise AuthorityError(self.integrity_error)
                for name, expected in ((CFG.POSITIONS_FILE, self._fingerprint),
                                       ("seen_fill_ids.json", self._fills_fingerprint),
                                       (CFG.TRADES_FILE, self.tlog._fingerprint)):
                    if file_fingerprint(_p(name)) != expected:
                        raise AuthorityError("STALE_INSTANCE: " + name)
                draft = copy.copy(self)
                draft.positions = copy.deepcopy(self.positions)
                draft.seen_fill_ids = set(self.seen_fill_ids)
                draft._position_draft = True
                draft.tlog = copy.copy(self.tlog)
                draft.tlog.trades = copy.deepcopy(self.tlog.trades)
                draft.tlog._journal_draft = True
                result = method(draft, *args, **kwargs)
                from persistence import JsonStore
                for actual, expected, filename in ((draft.tlog.trades, self.tlog.trades, CFG.TRADES_FILE),
                        (draft.positions, self.positions, CFG.POSITIONS_FILE),
                        (sorted(draft.seen_fill_ids), sorted(self.seen_fill_ids), "seen_fill_ids.json")):
                    if actual != expected and JsonStore.load(_p(filename), None) != actual:
                        raise AuthorityError("component commit is not independently readable")
            self.tlog.trades = draft.tlog.trades
            self.tlog._fingerprint = draft.tlog._fingerprint
            self.tlog.duplicate_ids = draft.tlog.duplicate_ids
            self._fingerprint = draft._fingerprint
            self._fills_fingerprint = draft._fills_fingerprint
            self.seen_fill_ids = draft.seen_fill_ids
            self.positions = draft.positions
            self.reconcile_halt = draft.reconcile_halt
            return copy.deepcopy(result)
        except Exception as exc:
            PersistenceSentinel.record_failure(path, str(exc))
            raise
    return transact
