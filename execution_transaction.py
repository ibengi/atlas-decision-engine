"""Serialize intent confirmation through broker transport and resolution."""
import functools
from state_authority import root_lock, active_transaction, recovery_problem
from persistence import PersistenceSentinel
from config import _p
from execution_result import ExecutionResult


def locked_submission(method):
    @functools.wraps(method)
    def execute(self, ticker, side, count, limit_cents):
        path = _p(self.PENDING_FILE)
        with root_lock(path):
            issue = recovery_problem(path)
            if active_transaction(path) is not None:
                active_transaction(path).failed = True
                issue = "conflicting execution during authoritative transaction"
            if issue:
                PersistenceSentinel.record_failure(path, issue)
                return ExecutionResult(None, count, 0, limit_cents,
                                       "blocked:recovery_required", "rejected")
            self._intent_side = side
            return method(self, ticker, side, count, limit_cents)
    return execute
