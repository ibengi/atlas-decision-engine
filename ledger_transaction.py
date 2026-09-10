"""Private ledger drafts: commit disk, then publish one object reference."""
import copy
import functools
import os

from persistence import PersistenceSentinel, file_fingerprint, read_generation
from state_authority import Transaction, AuthorityError
from prepared_continuity import PreparedContinuity


def ledger_transaction(method):
    @functools.wraps(method)
    def transact(self, *args, **kwargs):
        if self.readonly:
            return self.snapshot() if method.__name__ == "observe" else False
        if getattr(self, "_is_draft", False):
            return method(self, *args, **kwargs)
        try:
            if self.owner_pid != os.getpid():
                raise AuthorityError("inherited ledger is not a process authority")
            with Transaction(self.path, {os.path.basename(self.path),
                                        os.path.basename(self.chain.path)}, owner=self) as tx:
                tx.stage_json = True
                disk_generation = read_generation(self.path)
                if disk_generation != self.generation and not (
                        disk_generation is None and self.generation == 0 and
                        not os.path.exists(self.path)):
                    raise AuthorityError("FENCE_REFUSED: STALE_INSTANCE: reload required")
                draft = copy.copy(self)
                draft.state = copy.deepcopy(self.state)
                draft.chain = PreparedContinuity(self.chain)
                draft._is_draft = True
                draft._transaction = tx
                draft._read_set = {name: file_fingerprint(os.path.join(
                    os.path.dirname(self.path), name)) for name in (
                    "kalshi_trades.json", "positions_state.json", "orders_state.json",
                    "pending_intents.json", "submission_guard.json")}
                result = method(draft, *args, **kwargs)
                if result is False:
                    draft.chain.burn_aborted_tokens()
                    if tx.started:
                        tx.failed = True
                    return False
                for name, value in draft._read_set.items():
                    if file_fingerprint(os.path.join(os.path.dirname(self.path), name)) != value:
                        tx.failed = True
                        raise AuthorityError("authorizing read set changed: " + name)
                draft.chain.persist()
                tx.persist_prepared_json()
                if draft.generation != self.generation or draft.state != self.state:
                    from strict_data import loads
                    with open(self.path, "rb") as fh:
                        if loads(fh.read()) != draft.state:
                            raise AuthorityError("prepared ledger not independently readable")
            # Transaction.__exit__ has verified the durable root and removed
            # the incomplete marker. No callback has seen this draft as self.
            self.generation = draft.generation
            self._last_obs = draft._last_obs
            self.state = draft.state
            return self.snapshot() if method.__name__ == "observe" else result
        except Exception as exc:
            PersistenceSentinel.record_failure(self.path, str(exc))
            return self.snapshot() if method.__name__ == "observe" else False
    return transact
