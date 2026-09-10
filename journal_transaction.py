"""Journal mutation with content fencing and publication after durability."""
import copy
import functools
import os

from state_authority import Transaction, AuthorityError
from persistence import file_fingerprint, PersistenceSentinel


def journal_transaction(method):
    @functools.wraps(method)
    def mutate(self, *args, **kwargs):
        if getattr(self, "_journal_draft", False):
            return method(self, *args, **kwargs)
        try:
            with Transaction(self.path, {os.path.basename(self.path)}, owner=self):
                if file_fingerprint(self.path) != self._fingerprint:
                    raise AuthorityError("STALE_INSTANCE: journal reload required")
                draft = copy.copy(self)
                draft.trades = copy.deepcopy(self.trades)
                draft._journal_draft = True
                result = method(draft, *args, **kwargs)
                if draft.trades != self.trades:
                    from strict_data import loads
                    with open(self.path, "rb") as fh:
                        if loads(fh.read()) != draft.trades:
                            raise AuthorityError("prepared journal not independently readable")
            self.trades = draft.trades
            self.duplicate_ids = draft.duplicate_ids
            self.integrity_error = draft.integrity_error
            self._fingerprint = draft._fingerprint
            return copy.deepcopy(result)
        except Exception as exc:
            PersistenceSentinel.record_failure(self.path, str(exc))
            raise
    return mutate
