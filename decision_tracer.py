"""Lightweight end-to-end decision tracing for pipeline runs."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time
import uuid

_run_id: ContextVar[str | None] = ContextVar("atlas_run_id", default=None)
_run_started: ContextVar[float | None] = ContextVar("atlas_run_started", default=None)

# Add run_id to every LogRecord while a trace is active.  This preserves all
# existing function signatures and works with JsonFormatter's ``extra`` scan.
_previous_factory = logging.getLogRecordFactory()

def _record_factory(*args, **kwargs):
    record = _previous_factory(*args, **kwargs)
    run_id = _run_id.get()
    if run_id is not None and not hasattr(record, "run_id"):
        record.run_id = run_id
    return record

if getattr(_previous_factory, "_atlas_trace_factory", False) is False:
    _record_factory._atlas_trace_factory = True
    logging.setLogRecordFactory(_record_factory)


class DecisionTracer:
    """Owns one pipeline run context and emits machine-readable market events."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("PIPELINE")
        self.run_id = None
        self._started = None

    @property
    def active(self):
        return self.run_id is not None

    def elapsed_ms(self):
        return int((time.perf_counter() - self._started) * 1000) if self._started else 0

    def market(self, ticker, event_type, reason=None, edge=None, price=None):
        """Emit one market decision event with a stable schema."""
        if not self.active:
            return
        # run_id N'EST PAS passe via extra : la record factory (cf. module)
        # l'injecte dans chaque LogRecord pendant une trace. Le repasser ici
        # ferait lever KeyError("Attempt to overwrite 'run_id'") a INFO.
        fields = {"ticker": ticker,
                  "event_type": event_type, "timestamp_ms": self.elapsed_ms()}
        if reason is not None:
            fields["reason"] = reason
        if edge is not None:
            fields["edge"] = edge
        if price is not None:
            fields["price"] = price
        self.logger.info("[DECISION_TRACE] market %s %s", event_type, ticker,
                         extra=fields)

    def event(self, event_type, **fields):
        if not self.active:
            return
        fields.update(event_type=event_type,
                      timestamp_ms=self.elapsed_ms())
        self.logger.info("[DECISION_TRACE] run %s", event_type, extra=fields)

    @contextmanager
    def trace_run(self):
        """Create and install a fresh run ID for exactly one pipeline iteration."""
        self.run_id = uuid.uuid4().hex
        self._started = time.perf_counter()
        tokens = (_run_id.set(self.run_id), _run_started.set(self._started))
        try:
            yield self
        finally:
            _run_id.reset(tokens[0])
            _run_started.reset(tokens[1])
            self.run_id = None
            self._started = None


def current_tracer():
    """Return a tracer facade for the active context, or None."""
    run_id = _run_id.get()
    if run_id is None:
        return None
    tracer = DecisionTracer()
    tracer.run_id = run_id
    tracer._started = _run_started.get()
    return tracer
