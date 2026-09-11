"""Fenced half-open risk claims; a failed release never clears the claim."""
import copy
import functools

from config import CFG, _p
from persistence import PersistenceSentinel, file_fingerprint, JsonStore
from state_authority import AuthorityError, Transaction


def risk_transaction(method):
    @functools.wraps(method)
    def transact(self, *args, **kwargs):
        try:
            with Transaction(_p(CFG.RISK_FILE), {CFG.RISK_FILE}, owner=self):
                if file_fingerprint(_p(CFG.RISK_FILE)) != self._fingerprint:
                    raise AuthorityError("STALE_INSTANCE: risk state requires reload")
                draft = copy.copy(self)
                draft.state = copy.deepcopy(self.state)
                result = method(draft, *args, **kwargs)
                if draft.state != self.state and JsonStore.load(_p(CFG.RISK_FILE), None) != draft.state:
                    raise AuthorityError("risk action did not commit")
            self._fingerprint = draft._fingerprint
            self.state = draft.state
            return result
        except Exception as exc:
            PersistenceSentinel.record_failure(_p(CFG.RISK_FILE), str(exc))
            return (False, "risk_state_recovery_required") if method.__name__ == "claim_half_open_attempt" else False
    return transact
