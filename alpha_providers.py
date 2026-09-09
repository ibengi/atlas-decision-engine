"""Provider abstraction and the four adapters. SHADOW ONLY.

Alpha Gateway v1, sections 3, 4, 12, 19.

WHAT AN ADAPTER IS ALLOWED TO DO
    Build a prompt from an immutable snapshot, POST it to one vendor, and
    return raw text plus token counts and measured latency. That is all.
    An adapter does not validate (that is `alpha_schema`), does not weigh
    (that is `alpha_meta`), does not persist (that is `alpha_ledger`), and
    -- structurally, not by convention -- cannot reach the broker: nothing
    in this module imports `order_manager`, `execution_engine` or
    `kalshi_client`, and `tests/test_alpha_safety_boundary.py` fails the
    build if that ever changes.

SECRETS (section 19)
    Keys are read from the environment at call time and held only in the
    Authorization header of one request. They are never logged, never
    returned, never persisted, and never placed in an exception message --
    `_ProviderHTTP.post` truncates and sanitises every error body before it
    is allowed near a log line.

ENDPOINTS ARE CONFIGURATION, AND THE DEFAULTS ARE UNVERIFIED
    `CFG.ALPHA_*_BASE_URL` / `_MODEL` default to the shapes these vendors
    are documented to use, but this repository has no way to confirm them
    and they change. Treat the defaults as starting points: verify each
    against the vendor's current API reference before enabling the gateway
    anywhere, and correct them by configuration rather than by editing this
    file. A wrong endpoint is not dangerous here -- it produces a provider
    failure, which is an EXCLUDED signal, never a probability.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from alpha_snapshot import SCHEMA_VERSION, MarketSnapshot
from config import CFG

log = logging.getLogger("ALPHA")

#: Secrets, by name only. Their VALUES never appear in this module's output.
ENV_GROK = "XAI_API_KEY"
ENV_GEMINI = "GOOGLE_GEMINI_API_KEY"
ENV_OPENAI = "OPENAI_API_KEY"

#: Header names whose values must never be logged or persisted.
SECRET_HEADERS = frozenset({"authorization", "x-goog-api-key", "api-key",
                            "x-api-key"})


class ProviderError(RuntimeError):
    """A provider could not answer. Carries no secret and no probability."""


def _price_usd(input_tokens: int, output_tokens: int) -> tuple:
    """(usd, priced). `priced` is False while the rates are unset.

    The rates default to 0.0 and the flag says so. Inventing a plausible
    per-token price would silently decide the one question this subsystem
    exists to answer -- whether the edge survives inference cost -- so the
    ledger carries `cost_priced=false` and the metrics report refuses to
    present a net-of-AI-cost figure until an operator sets real rates.
    """
    rate_in = float(CFG.ALPHA_PRICE_IN_PER_MTOK)
    rate_out = float(CFG.ALPHA_PRICE_OUT_PER_MTOK)
    priced = rate_in > 0.0 or rate_out > 0.0
    usd = (input_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return round(usd, 8), priced


def build_prompt(snapshot: MarketSnapshot) -> str:
    """The instruction every provider receives. Identical across vendors so
    that a difference between two answers is a difference of model, not of
    prompt."""
    payload = snapshot.for_provider()
    return (
        "You are a calibrated forecaster. Estimate the probability that the "
        "market below resolves YES.\n\n"
        "Return ONE JSON object and nothing else. Do not wrap it in prose.\n"
        "Do NOT include any trading instruction, order, side, size, price or "
        "stake: a response containing any of those fields is discarded in "
        "full. You are producing a probability, not a decision.\n\n"
        "Echo `market_snapshot_id` and `contract_id` back EXACTLY as given.\n"
        "`probability_low` and `probability_high` must bracket `p_yes`.\n"
        "`status` is VALID, STALE, INSUFFICIENT_EVIDENCE or ERROR; use "
        "INSUFFICIENT_EVIDENCE rather than guessing.\n\n"
        "Required fields: schema_version, market_snapshot_id, contract_id, "
        "model, model_version, generated_at_utc, p_yes, probability_low, "
        "probability_high, confidence, evidence_quality, data_completeness, "
        "analysis_latency_ms, valid_until_utc, key_drivers, counterarguments, "
        "invalidation_triggers, assumptions, resolution_interpretation, "
        "status.\n"
        f"schema_version must be exactly \"{SCHEMA_VERSION}\".\n"
        "All timestamps ISO 8601 with an explicit UTC offset.\n\n"
        "MARKET SNAPSHOT:\n" + json.dumps(payload, indent=1, sort_keys=True))


def _default_valid_until(snapshot: MarketSnapshot) -> str:
    return snapshot.effective_valid_until().isoformat(timespec="seconds")


class AlphaProvider:
    """Base adapter. Subclasses implement `_call` and `_extract`.

    `analyze()` is the whole contract the dispatcher relies on: it returns
    `(raw_text, meta)` and NEVER raises for a vendor-side problem -- a
    provider failure is one excluded signal, not a dead cycle.
    """

    name = "base"
    env_key = None

    def __init__(self, *, session=None, model: str = None):
        #: Injectable transport. Tests pass a synthetic session; CI has no
        #: network at all (`tests/_netblock.py`).
        self.session = session
        self.model = model or self.default_model()

    # ── subclass surface ────────────────────────────────────────────────
    def default_model(self) -> str:
        raise NotImplementedError

    def _call(self, prompt: str, timeout: float) -> dict:
        raise NotImplementedError

    def _extract(self, response: dict) -> tuple:
        """(text, input_tokens, output_tokens)."""
        raise NotImplementedError

    # ── shared ──────────────────────────────────────────────────────────
    def configured(self) -> bool:
        """True when a key is present. Absence is reported as a provider
        failure, never as a neutral probability."""
        return bool(os.getenv(self.env_key or "", "").strip())

    def _api_key(self) -> str:
        key = os.getenv(self.env_key or "", "").strip()
        if not key:
            raise ProviderError(f"{self.env_key} is not set")
        return key

    def _post(self, url: str, *, headers: dict, payload: dict,
              timeout: float) -> dict:
        session = self.session
        if session is None:
            import requests
            session = requests.Session()
        response = session.post(url, headers=headers, json=payload,
                                timeout=timeout)
        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise ProviderError(f"HTTP {status}: {self._safe_body(response)}")
        try:
            return response.json()
        except Exception as e:                                # noqa: BLE001
            raise ProviderError(f"unreadable response body: {type(e).__name__}")

    @staticmethod
    def _safe_body(response) -> str:
        """A short, secret-free excerpt of an error body.

        Vendors echo request headers into error payloads often enough that
        printing one verbatim is a credible way to leak a key into a log
        that is shipped somewhere else.
        """
        try:
            text = response.text or ""
        except Exception:                                     # noqa: BLE001
            return "<unreadable>"
        lowered = text.lower()
        for marker in SECRET_HEADERS:
            if marker in lowered:
                return "<redacted: body referenced an authorization header>"
        return text[:200]

    def analyze(self, snapshot: MarketSnapshot, timeout: float) -> tuple:
        """(raw_text_or_None, meta). Measures latency and token cost.

        `meta` always carries `latency_ms` and a `cost` block, even on
        failure: a provider that times out still consumed a deadline, and
        section 12 wants that recorded.
        """
        started = time.monotonic()
        meta = {"provider": self.name, "model": self.model,
                "latency_ms": 0,
                "cost": {"provider": self.name, "model": self.model,
                         "input_tokens": 0, "output_tokens": 0,
                         "api_cost_usd": 0.0, "cost_priced": False,
                         "latency_ms": 0},
                "error": None}
        try:
            if not self.configured():
                raise ProviderError(f"{self.env_key} is not set")
            response = self._call(build_prompt(snapshot), timeout)
            text, tokens_in, tokens_out = self._extract(response)
        except ProviderError as e:
            meta["error"] = str(e)
        except Exception as e:                                # noqa: BLE001
            # A vendor SDK or a transport can raise anything at all. The
            # dispatcher's isolation guarantee is only as good as this line.
            meta["error"] = f"{type(e).__name__}: {e}"
        else:
            usd, priced = _price_usd(tokens_in, tokens_out)
            meta["cost"].update({"input_tokens": int(tokens_in),
                                 "output_tokens": int(tokens_out),
                                 "api_cost_usd": usd, "cost_priced": priced})
            latency = int(round((time.monotonic() - started) * 1000))
            meta["latency_ms"] = latency
            meta["cost"]["latency_ms"] = latency
            return text, meta
        latency = int(round((time.monotonic() - started) * 1000))
        meta["latency_ms"] = latency
        meta["cost"]["latency_ms"] = latency
        return None, meta


class _OpenAICompatible(AlphaProvider):
    """Shared shape for the chat-completions style APIs.

    xAI publishes an OpenAI-compatible surface, so one implementation
    covers both. If either vendor diverges, override `_call` in the
    subclass rather than adding a flag here.
    """

    base_url_attr = None

    def _call(self, prompt: str, timeout: float) -> dict:
        return self._post(
            f"{getattr(CFG, self.base_url_attr).rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}",
                     "Content-Type": "application/json"},
            payload={"model": self.model,
                     "messages": [{"role": "user", "content": prompt}],
                     "response_format": {"type": "json_object"}},
            timeout=timeout)

    def _extract(self, response: dict) -> tuple:
        if not isinstance(response, dict):
            raise ProviderError(f"response is {type(response).__name__}")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("no choices in response")
        message = (choices[0] or {}).get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("empty completion")
        usage = response.get("usage") or {}
        return (text,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0))


class GrokProvider(_OpenAICompatible):
    name = "grok"
    env_key = ENV_GROK
    base_url_attr = "ALPHA_GROK_BASE_URL"

    def default_model(self) -> str:
        return CFG.ALPHA_GROK_MODEL


class OpenAIProvider(_OpenAICompatible):
    name = "openai"
    env_key = ENV_OPENAI
    base_url_attr = "ALPHA_OPENAI_BASE_URL"

    def default_model(self) -> str:
        return CFG.ALPHA_OPENAI_MODEL


class GeminiProvider(AlphaProvider):
    """Gemini's generateContent surface differs enough to warrant its own
    adapter: the key travels in a header, the body is `contents`, and the
    token counts live under `usageMetadata`."""

    name = "gemini"
    env_key = ENV_GEMINI

    def default_model(self) -> str:
        return CFG.ALPHA_GEMINI_MODEL

    def _call(self, prompt: str, timeout: float) -> dict:
        base = CFG.ALPHA_GEMINI_BASE_URL.rstrip("/")
        return self._post(
            f"{base}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self._api_key(),
                     "Content-Type": "application/json"},
            payload={"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"responseMimeType": "application/json"}},
            timeout=timeout)

    def _extract(self, response: dict) -> tuple:
        if not isinstance(response, dict):
            raise ProviderError(f"response is {type(response).__name__}")
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("no candidates in response")
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text.strip():
            raise ProviderError("empty completion")
        usage = response.get("usageMetadata") or {}
        return (text,
                int(usage.get("promptTokenCount") or 0),
                int(usage.get("candidatesTokenCount") or 0))


class AtlasQuantProvider(AlphaProvider):
    """Atlas's own quantitative estimate.

    In-process, no key, no network, no token cost -- but it goes through the
    SAME interface, the same schema and the same validator as the LLMs. It
    is not privileged: if the quant model returns an out-of-range
    probability it is excluded exactly like a vendor that does.

    `estimator` is the injection point for the real quantitative model. It
    receives the snapshot dict and returns `(p_yes, low, high, confidence)`.
    The default is deliberately NOT a model: it returns None, which becomes
    INSUFFICIENT_EVIDENCE. A placeholder that returned the market price
    would manufacture agreement with the market and make the ensemble look
    calibrated while measuring nothing.
    """

    name = "atlas_quant"
    env_key = None

    def __init__(self, *, estimator=None, model_version: str = "unwired",
                 **kw):
        self.estimator = estimator
        self.model_version = model_version
        super().__init__(**kw)

    def default_model(self) -> str:
        return "atlas_quant"

    def configured(self) -> bool:
        return True                     # no credential; always reachable

    def analyze(self, snapshot: MarketSnapshot, timeout: float) -> tuple:
        started = time.monotonic()
        meta = {"provider": self.name, "model": self.model, "latency_ms": 0,
                "cost": {"provider": self.name, "model": self.model,
                         "input_tokens": 0, "output_tokens": 0,
                         "api_cost_usd": 0.0, "cost_priced": True,
                         "latency_ms": 0},
                "error": None}
        text = None
        try:
            estimate = None
            if self.estimator is not None:
                estimate = self.estimator(snapshot.for_provider())
            now = datetime.now(timezone.utc)
            if estimate is None:
                body = {"status": "INSUFFICIENT_EVIDENCE",
                        "resolution_interpretation":
                            "no quantitative model is wired for this market"}
                p = low = high = confidence = None
            else:
                p, low, high, confidence = estimate
                body = {"status": "VALID"}
            text = json.dumps({
                "schema_version": SCHEMA_VERSION,
                "market_snapshot_id": snapshot.market_snapshot_id,
                "contract_id": snapshot.contract_id,
                "model": self.name, "model_version": self.model_version,
                "generated_at_utc": now.isoformat(timespec="seconds"),
                "p_yes": p, "probability_low": low, "probability_high": high,
                "confidence": confidence,
                "evidence_quality": confidence,
                "data_completeness": confidence,
                "analysis_latency_ms": 0,
                "valid_until_utc": _default_valid_until(snapshot),
                "key_drivers": [], "counterarguments": [],
                "invalidation_triggers": [], "assumptions": [],
                **body})
        except Exception as e:                                # noqa: BLE001
            meta["error"] = f"{type(e).__name__}: {e}"
        latency = int(round((time.monotonic() - started) * 1000))
        meta["latency_ms"] = latency
        meta["cost"]["latency_ms"] = latency
        return text, meta


def default_providers(*, session=None, quant_estimator=None) -> list:
    """The four adapters, in a fixed order so reports are comparable."""
    return [GrokProvider(session=session),
            GeminiProvider(session=session),
            OpenAIProvider(session=session),
            AtlasQuantProvider(estimator=quant_estimator)]
