"""Private continuity preparation for a ledger transaction.

Record hashes and token decisions are prepared without appending anything.
The enclosing transaction persists the ledger and these exact records under
one incomplete marker, then publishes the ledger only after both are durable.
"""
import copy

from continuity import ContinuityChain, CHAIN_VERSION, GENESIS, ChainError


class PreparedContinuity(ContinuityChain):
    def __init__(self, source):
        self.source = source
        self.path = source.path
        self.prepared = copy.deepcopy(source.records())
        self.pending = []

    def records(self):
        return self.prepared

    def append(self, kind, payload, at):
        prev = self.prepared[-1]["hash"] if self.prepared else GENESIS
        record = {"version": CHAIN_VERSION, "seq": len(self.prepared) + 1,
                  "prev": prev, "kind": kind, "at": at,
                  "payload": copy.deepcopy(payload)}
        record["hash"] = self.record_hash(record)
        self._validate_record(record, len(self.prepared) + 1, prev)
        self.prepared.append(record)
        self.pending.append(record)
        return copy.deepcopy(record)

    def persist(self):
        for record in self.pending:
            actual = self.source.append(record["kind"], record["payload"], record["at"])
            if actual != record:
                raise ChainError("prepared continuity changed before commit")

    def burn_aborted_tokens(self):
        from continuity import KIND_TOKEN
        for record in self.pending:
            if record["kind"] == KIND_TOKEN:
                self.source.append(record["kind"], record["payload"], record["at"])
