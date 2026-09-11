"""Private resolution of pending intent and adopted order in one commit."""
import copy
import functools

from config import CFG, _p
from persistence import file_fingerprint, PersistenceSentinel
from state_authority import Transaction, AuthorityError


def resolve_transaction(method):
    @functools.wraps(method)
    def transact(self, *args, **kwargs):
        try:
            with Transaction(_p(self.PENDING_FILE), {self.PENDING_FILE,
                              CFG.ORDERS_FILE, "submission_guard.json"}, owner=self):
                for name, expected in ((self.PENDING_FILE, self._pending_fingerprint),
                                       (CFG.ORDERS_FILE, self._orders_fingerprint),
                                       ("submission_guard.json", self._guard_fingerprint)):
                    if file_fingerprint(_p(name)) != expected:
                        raise AuthorityError("STALE_INSTANCE: " + name)
                if method.__name__ == "resolve_intent":
                    ticker, intent = args
                    if intent != self.pending_intents.get(ticker) or not self._valid_intent(ticker, intent):
                        raise AuthorityError("unproven or malformed intent resolution")
                draft = copy.copy(self)
                draft.pending_intents = copy.deepcopy(self.pending_intents)
                draft.open_orders = copy.deepcopy(self.open_orders)
                draft.session_submitted = copy.deepcopy(self.session_submitted)
                call_args = ((ticker, draft.pending_intents[ticker]) if
                             method.__name__ == "resolve_intent" else args)
                result = method(draft, *call_args, **kwargs)
                from persistence import JsonStore
                for attr, name in (("pending_intents", self.PENDING_FILE),
                                   ("open_orders", CFG.ORDERS_FILE),
                                   ("session_submitted", "submission_guard.json")):
                    if getattr(draft, attr) != getattr(self, attr):
                        if JsonStore.load(_p(name), None) != getattr(draft, attr):
                            raise AuthorityError("prepared order state not independently readable")
            self.open_orders = draft.open_orders
            self.session_submitted = draft.session_submitted
            self.pending_intents = draft.pending_intents
            self.resolution_halt = draft.resolution_halt
            for attr in ("_pending_fingerprint", "_orders_fingerprint", "_guard_fingerprint"):
                setattr(self, attr, getattr(draft, attr))
            return result
        except Exception as exc:
            PersistenceSentinel.record_failure(_p(self.PENDING_FILE), str(exc))
            self._halt_resolution("RECOVERY_REQUIRED", str(exc))
            return "UNAVAILABLE"
    return transact
