#!/usr/bin/env python3
"""Timing — instrumentation temporelle des etapes du pipeline (P4.4).

Mesure la latence de chaque etape cle du pipeline et expose les
statistiques par phase via le health monitor (P4.3).

Composants
----------
- ``Timer``            mesure du temps ecoule avec ``time.perf_counter()``
                       (horloge monotone, ~50ns/lecture) et emission d'une
                       ligne de log structuree JSON (P4.1) avec
                       ``phase``, ``duration_ms``, ``run_id`` (P4.2,
                       contexte DecisionTracer) et ``timestamp`` (ajoute
                       automatiquement par JsonFormatter).
- ``timed(phase)``     context manager : ``with timed("api_fetch"): ...``
                       Surcharge < 1ms : deux lectures perf_counter + un
                       appel log.info (~quelques microsecondes).
- ``TimingStats``      accumulateur min/max/moyenne/p50/p95 par phase,
                       thread-safe, fenetre bornee pour les percentiles.
                       Expose dans la section ``timing_stats`` de ``/health``.

Integration
-----------
- ``opportunity_pipeline.py`` : ``api_fetch`` (scan + carnet frais),
  ``market_evaluation`` (boucle d'evaluation), ``signal_check`` (portes).
- ``execution_engine.py`` : ``full_cycle``, ``risk_check`` (portes risque
  globales + budgets par marche), ``order_placement`` (soumission API).
"""

import logging
import threading
import time
from collections import deque

import decision_tracer  # noqa: F401  (installe la record factory run_id, P4.2)

log = logging.getLogger("TIMING")

# Fenetre d'echantillons gardes en memoire pour le calcul des percentiles.
# Les compteurs min/max/moyenne portent sur TOUTES les mesures (count/sum).
_TIMING_WINDOW = 1000


def _r3(value):
    """Arrondi a 3 decimales, None preserve."""
    return round(value, 3) if value is not None else None


class Timer:
    """Mesure un intervalle de temps (wall-clock monotone) et l'emets en
    log structure. Utilisable directement ou via ``timed()``."""

    def __init__(self, phase: str, logger=None):
        self.phase = phase
        self.logger = logger or log
        self._started = None

    def start(self) -> "Timer":
        """Demarre la mesure (idempotent : un second start relance)."""
        self._started = time.perf_counter()
        return self

    def stop(self) -> float:
        """Arrete la mesure et retourne le temps ecoule en secondes."""
        if self._started is None:
            return 0.0
        elapsed = time.perf_counter() - self._started
        self._started = None
        return elapsed

    def emit(self, duration_s: float) -> float:
        """Emet la ligne de log structuree pour ``duration_s`` secondes et
        retourne la duree en ms. Le ``run_id`` est injecte automatiquement
        dans le LogRecord par la record factory de decision_tracer (P4.2)
        pendant une trace active ; il n'est donc PAS repasse via extra (le
        repasser leverait KeyError a INFO). Le champ ``timestamp``
        (ISO-8601 UTC) est ajoute par JsonFormatter (P4.1) a partir de
        l'heure du LogRecord."""
        duration_ms = round(duration_s * 1000.0, 3)
        fields = {"phase": self.phase, "duration_ms": duration_ms}
        self.logger.info(f"[TIMING] phase={self.phase} "
                         f"duration_ms={duration_ms}", extra=fields)
        return duration_ms

    def __enter__(self) -> "Timer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._started is not None:
            self.emit(self.stop())
        return False  # ne jamais masquer une exception


class _Timed:
    """Context manager produit par ``timed()`` : mesure + emission + stats."""

    def __init__(self, phase: str, stats=None):
        self.timer = Timer(phase)
        self.stats = stats if stats is not None else TIMING_STATS

    def __enter__(self) -> Timer:
        return self.timer.__enter__()

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = None
        if self.timer._started is not None:
            duration_ms = self.timer.emit(self.timer.stop())
        if duration_ms is not None and self.stats is not None:
            self.stats.record(self.timer.phase, duration_ms)
        return False


def timed(phase: str, stats=None):
    """Context manager de mesure : ``with timed("api_fetch"): ...``.

    Emet une ligne [TIMING] (JSON en production) et enregistre la duree
    dans ``stats`` (par defaut l'instance partagee exposee par /health).
    """
    return _Timed(phase, stats)


class TimingStats:
    """Statistiques de duree par phase : count / min / max / avg / p50 / p95.

    Thread-safe : record() est appele par le thread du moteur pendant que
    snapshot() est lu par les threads HTTP du dashboard.
    """

    def __init__(self, window: int = _TIMING_WINDOW):
        self.window = max(10, window)
        self._lock = threading.Lock()
        self._per_phase = {}          # phase -> dict de cumuls + echantillons

    def record(self, phase: str, duration_ms: float) -> None:
        with self._lock:
            st = self._per_phase.setdefault(phase, {
                "count": 0, "sum": 0.0, "min": None, "max": None,
                "samples": deque(maxlen=self.window)})
            st["count"] += 1
            st["sum"] += duration_ms
            if st["min"] is None or duration_ms < st["min"]:
                st["min"] = duration_ms
            if st["max"] is None or duration_ms > st["max"]:
                st["max"] = duration_ms
            st["samples"].append(duration_ms)

    @staticmethod
    def _percentile(sorted_values, p: float):
        """Percentile lineaire (type 7, comme numpy) sur une liste triee."""
        n = len(sorted_values)
        if n == 0:
            return None
        if n == 1:
            return sorted_values[0]
        rank = (n - 1) * p
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        return (sorted_values[lo]
                + (sorted_values[hi] - sorted_values[lo]) * frac)

    def snapshot(self) -> dict:
        """Toutes les phases triees : {phase: {count, min_ms, max_ms,
        avg_ms, p50_ms, p95_ms}}."""
        with self._lock:
            out = {}
            for phase in sorted(self._per_phase):
                st = self._per_phase[phase]
                count = st["count"]
                if count == 0:
                    continue
                samples = sorted(st["samples"])
                out[phase] = {
                    "count": count,
                    "min_ms": _r3(st["min"]),
                    "max_ms": _r3(st["max"]),
                    "avg_ms": _r3(st["sum"] / count),
                    "p50_ms": _r3(self._percentile(samples, 0.50)),
                    "p95_ms": _r3(self._percentile(samples, 0.95)),
                }
            return out

    def clear(self) -> None:
        with self._lock:
            self._per_phase.clear()

    def phases(self) -> list:
        with self._lock:
            return sorted(self._per_phase)


# Instance partagee module : enregistree par timed() par defaut et lue par
# health_monitor.get_health_json() pour la section "timing_stats".
TIMING_STATS = TimingStats()


if __name__ == "__main__":                                # pragma: no cover
    import json
    import logging.config as lc
    lc.dictConfig({"version": 1, "disable_existing_loggers": False,
                   "handlers": {"h": {"class": "logging.StreamHandler"}},
                   "root": {"handlers": ["h"], "level": "INFO"}})
    with timed("demo_phase"):
        for _ in range(1000):
            pass
    with timed("demo_phase"):
        for _ in range(100000):
            pass
    print(json.dumps(TIMING_STATS.snapshot(), indent=2, default=str))
