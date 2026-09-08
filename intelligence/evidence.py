"""Durable append-only evidence for shadow AI predictions."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable

from .schemas import IntelligenceObservation, MarketCandidate


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class IntelligenceEvidenceStore:
    """Append-only JSONL store for provider predictions.

    This is research evidence only.  It is intentionally a separate file from
    trade journals and does not participate in risk, sizing or broker state.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike,
        filename: str = "ai_shadow_observations.jsonl",
        fsync: bool = True,
    ) -> None:
        self.path = Path(data_dir) / filename
        self.fsync = bool(fsync)
        self._lock = Lock()

    def _row(
        self,
        candidate: MarketCandidate,
        observation: IntelligenceObservation,
    ) -> dict:
        if candidate.contract_id != observation.contract_id:
            raise ValueError("candidate and observation contract_id mismatch")
        body = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": _now_iso(),
            "contract_id": candidate.contract_id,
            "candidate_observed_at": candidate.observed_at,
            "market_probability": candidate.market_probability,
            "category": candidate.category,
            "observation": observation.as_evidence(),
        }
        body["evidence_id"] = hashlib.sha256(_canonical(body)).hexdigest()
        return body

    def append(
        self,
        candidate: MarketCandidate,
        observations: Iterable[IntelligenceObservation],
    ) -> list[str]:
        rows = [self._row(candidate, obs) for obs in observations]
        if not rows:
            return []
        payload = b"".join(_canonical(row) + b"\n" for row in rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write to AI evidence journal")
                    view = view[written:]
                if self.fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)
        return [row["evidence_id"] for row in rows]
