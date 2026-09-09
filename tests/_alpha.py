# -*- coding: utf-8 -*-
"""Shared fixtures for the Alpha Gateway suite.

Every case runs the PRODUCTION classes on a throwaway DATA_DIR with no
network: providers are driven through an injected synthetic session or
replaced by in-process doubles, so CI never opens a socket and never spends
a token. `tests/_netblock.py` blocks the rest.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

from alpha_providers import AlphaProvider                     # noqa: E402
from alpha_schema import SCHEMA_VERSION                       # noqa: E402
from alpha_snapshot import build_snapshot                     # noqa: E402
from config import CFG                                        # noqa: E402

CONTRACT = "KXBTCD-26SEP0912-T60000"


def valid_payload(snapshot, *, p_yes=0.60, low=0.55, high=0.65,
                  confidence=0.7, evidence=0.8, completeness=0.9,
                  model="m", **over):
    """A model answer that passes every check. Cases mutate one field."""
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "contract_id": snapshot.contract_id,
        "model": model, "model_version": "1.0",
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "p_yes": p_yes, "probability_low": low, "probability_high": high,
        "confidence": confidence, "evidence_quality": evidence,
        "data_completeness": completeness,
        "analysis_latency_ms": 120,
        "valid_until_utc": snapshot.analysis_deadline_utc,
        "key_drivers": ["driver"], "counterarguments": [],
        "invalidation_triggers": [], "assumptions": [],
        "resolution_interpretation": "as written",
        "status": "VALID",
    }
    payload.update(over)
    return payload


class FakeProvider(AlphaProvider):
    """In-process double for the whole adapter contract.

    `behaviour` is either a payload dict/str to return, or a callable that
    raises or sleeps. Nothing here touches HTTP.
    """

    env_key = None

    def __init__(self, name, behaviour=None, *, tokens=(900, 200),
                 latency_ms=0, error=None, sleep_s=0.0):
        self.name = name
        self.behaviour = behaviour
        self.tokens = tokens
        self.latency_ms = latency_ms
        self.error = error
        self.sleep_s = sleep_s
        self.calls = 0
        super().__init__()

    def default_model(self):
        return self.name

    def configured(self):
        return True

    def analyze(self, snapshot, timeout):
        import time
        self.calls += 1
        meta = {"provider": self.name, "model": self.model,
                "latency_ms": self.latency_ms,
                "cost": {"provider": self.name, "model": self.model,
                         "input_tokens": self.tokens[0],
                         "output_tokens": self.tokens[1],
                         "api_cost_usd": 0.0, "cost_priced": False,
                         "latency_ms": self.latency_ms},
                "error": None}
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.error:
            meta["error"] = self.error
            return None, meta
        if callable(self.behaviour):
            return self.behaviour(snapshot), meta
        payload = self.behaviour
        if payload is None:
            payload = valid_payload(snapshot, model=self.name)
        return (payload if isinstance(payload, str) else json.dumps(payload)), meta


def http_session(response_body, *, status=200, raises=None):
    """A synthetic `requests`-like session. Never opens a socket."""

    class _Response:
        status_code = status

        def __init__(self, body):
            self._body = body

        @property
        def text(self):
            return json.dumps(self._body) if isinstance(self._body, dict) \
                else str(self._body)

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    class _Session:
        def __init__(self):
            self.posts = []

        def post(self, url, *, headers=None, json=None, timeout=None):
            self.posts.append({"url": url, "headers": dict(headers or {}),
                               "json": json, "timeout": timeout})
            if raises is not None:
                raise raises
            return _Response(response_body)

    return _Session()


class AlphaCase(unittest.TestCase):
    """Isolated DATA_DIR, deterministic config, no provider credentials."""

    ENV_KEYS = ("XAI_API_KEY", "GOOGLE_GEMINI_API_KEY", "OPENAI_API_KEY",
                "ALPHA_GATEWAY_ENABLED")

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self._tmp = tempfile.mkdtemp(prefix="alpha-")
        self._patches = [patch.object(CFG, "DATA_DIR", self._tmp)]
        for p in self._patches:
            p.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── snapshots ───────────────────────────────────────────────────────
    def snapshot(self, *, minutes_to_resolution=240, catalyst_in=None,
                 yes_ask=0.46, yes_bid=0.44, no_ask=0.56, no_bid=0.54,
                 contract_id=CONTRACT, snapshot_time=None):
        now = snapshot_time or datetime.now(timezone.utc)
        return build_snapshot(
            contract_id=contract_id, event_id="EV-1",
            question="Will BTC be above 60000 at 12:00 ET?",
            resolution_rules="CF Benchmarks RTI at 12:00 ET",
            resolution_source="kalshi",
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
            volume=1200, open_interest=3400,
            snapshot_time_utc=now.isoformat(),
            market_close_time_utc=(
                now + timedelta(minutes=minutes_to_resolution - 30)).isoformat(),
            expected_resolution_time_utc=(
                now + timedelta(minutes=minutes_to_resolution)).isoformat(),
            catalyst_name="CPI release" if catalyst_in else "",
            catalyst_time_utc=(now + timedelta(seconds=catalyst_in)).isoformat()
            if catalyst_in else None)

    def agreeing_providers(self, *, probabilities=(0.58, 0.61, 0.57, 0.60)):
        names = ("grok", "gemini", "openai", "atlas_quant")
        return [FakeProvider(name,
                             behaviour=lambda s, p=p, n=name:
                             json.dumps(valid_payload(s, p_yes=p,
                                                      low=max(0.0, p - 0.04),
                                                      high=min(1.0, p + 0.04),
                                                      model=n)))
                for name, p in zip(names, probabilities)]
