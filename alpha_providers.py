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
#: `GEMINI_API_KEY` is the name the deployment uses; the longer form is
#: accepted as a fallback so an existing environment keeps working. Both are
#: read at call time and neither value is ever logged or returned.
ENV_GEMINI = "GEMINI_API_KEY"
ENV_GEMINI_FALLBACK = "GOOGLE_GEMINI_API_KEY"
ENV_OPENAI = "OPENAI_API_KEY"

#: Header names whose values must never be logged or persisted.
SECRET_HEADERS = frozenset({"authorization", "x-goog-api-key", "api-key",
                            "x-api-key"})

#: Every environment variable whose VALUE is a credential.
SECRET_ENV_VARS = (ENV_GROK, ENV_GEMINI, ENV_GEMINI_FALLBACK, ENV_OPENAI)


def redact(text) -> str:
    """Remove any live credential VALUE from a string.

    `_safe_body` covers a vendor echoing our header back inside a response
    body, but that is not the only way a key escapes: a transport, a proxy
    or a vendor SDK can raise an exception whose message quotes the request
    -- URL, headers and all -- and that message goes straight into
    `meta["error"]`, which is returned, logged and printed by the smoke
    test. Every string leaving this module on the failure path passes
    through here, so the leak is closed once rather than at each call site.

    Every configured key is stripped, not just this provider's: a shared
    session or a proxy error can quote a different vendor's header.
    """
    out = str(text)
    for name in SECRET_ENV_VARS:
        value = os.getenv(name, "").strip()
        if len(value) >= 8 and value in out:
            out = out.replace(value, f"<redacted:{name}>")
    return out


class ProviderError(RuntimeError):
    """A provider could not answer. Carries no secret and no probability."""


#: One shared pricing table per process. Reloaded explicitly by the service
#: on demand, never silently re-read mid-cycle: two providers in the same
#: analysis must be costed under the same version or the total means nothing.
_PRICING = None


def pricing_table():
    global _PRICING
    if _PRICING is None:
        from alpha_cost import PricingTable
        _PRICING = PricingTable()
    return _PRICING


def reload_pricing():
    global _PRICING
    _PRICING = None
    return pricing_table()


def set_pricing_table(table):
    """Bind the process to ONE table.

    The budget guard prices the pre-call ESTIMATE and the adapters price the
    post-call ACTUAL. If those two read different tables, the arithmetic that
    decides whether a call is affordable is not the arithmetic that records
    what it cost -- so a cap could be enforced against rates nobody is being
    billed at. The service binds both to the same object at construction.
    """
    global _PRICING
    _PRICING = table
    return _PRICING


def _cost_row(provider: str, model: str, input_tokens: int,
              output_tokens: int, *, cached_input_tokens: int = 0,
              tool_calls: int = 0, search_queries: int = 0,
              latency_ms: int = 0, billed: dict = None) -> dict:
    """A complete usage row: what was used, what it cost, under WHICH rate
    card -- and, when the vendor supplied one, what it says it billed.

    Both figures are kept. Preferring ours silently would hide a billing
    surprise; preferring theirs silently would hide a rate-card error.
    `reconcile_billed_cost` flags a disagreement instead of resolving it.
    """
    from alpha_cost import reconcile_billed_cost
    row = pricing_table().price(
        provider, model, input_tokens, output_tokens,
        cached_input_tokens=cached_input_tokens, tool_calls=tool_calls,
        search_queries=search_queries)
    row["latency_ms"] = int(latency_ms)
    return reconcile_billed_cost(row, billed or {})


def _xai_billed(usage: dict) -> dict:
    """xAI's authoritative billed amount, from `usage.cost_in_usd_ticks`.

    The RAW tick count is always recorded. A USD figure is derived only when
    `ALPHA_XAI_COST_TICKS_PER_USD` is configured, because the tick
    denomination is not something this repository can verify and a factor-of
    -ten error would misprice every call. Recording the raw value keeps the
    evidence; refusing to convert keeps the arithmetic honest.
    """
    ticks = usage.get("cost_in_usd_ticks")
    if not isinstance(ticks, (int, float)) or isinstance(ticks, bool):
        return {}
    out = {"billed_cost_raw": {"unit": "cost_in_usd_ticks", "value": ticks},
           "billed_cost_source": "xai.usage.cost_in_usd_ticks"}
    scale = float(CFG.ALPHA_XAI_COST_TICKS_PER_USD)
    if scale > 0:
        out["billed_cost_usd"] = round(float(ticks) / scale, 10)
    else:
        out["billed_cost_note"] = (
            "ALPHA_XAI_COST_TICKS_PER_USD is unset, so the tick count is "
            "recorded but not converted; set it once a real call lets you "
            "compute ticks / estimated-USD")
    return out


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
        return redact(text[:200])

    def analyze(self, snapshot: MarketSnapshot, timeout: float) -> tuple:
        """(raw_text_or_None, meta). Measures latency and token cost.

        `meta` always carries `latency_ms` and a `cost` block, even on
        failure: a provider that times out still consumed a deadline, and
        section 4 wants that recorded.
        """
        started = time.monotonic()
        meta = {"provider": self.name, "model": self.model,
                "latency_ms": 0,
                "cost": _cost_row(self.name, self.model, 0, 0),
                "error": None}
        try:
            if not self.configured():
                raise ProviderError(f"{self.env_key} is not set")
            response = self._call(build_prompt(snapshot), timeout)
            text, usage = self._extract(response)
        except ProviderError as e:
            meta["error"] = redact(e)
        except Exception as e:                                # noqa: BLE001
            # A vendor SDK or a transport can raise anything at all. The
            # dispatcher's isolation guarantee is only as good as this line,
            # and the message can quote the request headers, so it is
            # redacted rather than trusted.
            meta["error"] = redact(f"{type(e).__name__}: {e}")
        else:
            latency = int(round((time.monotonic() - started) * 1000))
            meta["latency_ms"] = latency
            meta["cost"] = _cost_row(
                self.name, self.model,
                usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                cached_input_tokens=usage.get("cached_input_tokens", 0),
                tool_calls=usage.get("tool_calls", 0),
                search_queries=usage.get("search_queries", 0),
                latency_ms=latency, billed=usage.get("billed"))
            return text, meta
        latency = int(round((time.monotonic() - started) * 1000))
        meta["latency_ms"] = latency
        meta["cost"]["latency_ms"] = latency
        return None, meta

    # ── startup validation (section 3) ──────────────────────────────────
    def health_check(self, timeout: float = None) -> dict:
        """Is this provider usable RIGHT NOW, with the model configured?

        Returns a report; never raises. A provider that fails its health
        check is not silently swapped for another model or another vendor --
        section 3 is explicit that an unavailable provider/model is EXCLUDED.
        The service logs the report at startup and keeps dispatching to the
        healthy ones, because no single provider is mandatory (section 18).
        """
        report = {"provider": self.name, "model": self.model,
                  "configured": False, "priced": False, "reachable": None,
                  "detail": ""}
        if not self.configured():
            report["detail"] = f"{self.env_key} is not set"
            return report
        report["configured"] = True
        estimate = pricing_table().estimate(self.name, self.model)
        report["priced"] = estimate["cost_priced"]
        if not report["priced"]:
            report["detail"] = estimate["pricing_missing_reason"] or ""
        if not CFG.ALPHA_HEALTHCHECK_ENABLED:
            report["detail"] = (report["detail"] + "; reachability check "
                                "disabled").strip("; ")
            return report
        try:
            self._probe(float(timeout or CFG.ALPHA_HEALTHCHECK_TIMEOUT_S))
            report["reachable"] = True
        except ProviderError as e:
            report["reachable"] = False
            report["detail"] = (report["detail"] + f"; {e}").strip("; ")
        except Exception as e:                                # noqa: BLE001
            report["reachable"] = False
            report["detail"] = (report["detail"]
                                + f"; {type(e).__name__}: {e}").strip("; ")
        return report

    def _probe(self, timeout: float) -> None:
        """A minimal round trip that proves the credential and the model id
        are both accepted. Subclasses override; the default probes with the
        real request shape and a trivial prompt, because a probe that uses a
        different endpoint proves nothing about the one we will use."""
        self._extract(self._call("Reply with the single character: 1", timeout))

    def healthy(self, report: dict = None) -> bool:
        report = report or self.health_check()
        return bool(report["configured"]
                    and (report["priced"] or CFG.ALPHA_ALLOW_UNPRICED_CALLS)
                    and report["reachable"] is not False)


class _ChatCompletions(AlphaProvider):
    """The chat-completions shape.

    Kept as a ready fallback surface: both xAI and OpenAI still publish it,
    and a vendor-side problem with the Responses API should be one
    configuration change away from being routed around, not a rewrite. No
    provider uses it by default.
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
        details = usage.get("prompt_tokens_details") or {}
        return text, {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_input_tokens": int(details.get("cached_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "tool_calls": len(message.get("tool_calls") or []),
            "search_queries": int(
                (usage.get("num_sources_used")
                 or usage.get("num_search_queries") or 0)),
            "billed": _xai_billed(usage),
        }


class _ResponsesAPI(AlphaProvider):
    """The Responses-API shape, shared by OpenAI and xAI.

    Both publish `POST /responses` with an `input` field, an `output` array
    whose message items carry `content[].text`, an `output_text`
    convenience field, and usage named `input_tokens` / `output_tokens`.
    One implementation covers both; the vendor-specific parts are the base
    URL, the path, the model, and how billed cost is reported.

    As everywhere in this module the defaults are STARTING POINTS this
    repository cannot verify. A wrong shape produces a provider failure --
    an EXCLUDED signal, never a probability.
    """

    base_url_attr = None
    path_attr = None

    def _billed(self, usage: dict) -> dict:
        return {}

    def _call(self, prompt: str, timeout: float) -> dict:
        base = getattr(CFG, self.base_url_attr).rstrip("/")
        path = getattr(CFG, self.path_attr)
        return self._post(
            f"{base}{path if path.startswith('/') else '/' + path}",
            headers={"Authorization": f"Bearer {self._api_key()}",
                     "Content-Type": "application/json"},
            payload={"model": self.model, "input": prompt,
                     # The budget was checked against this ceiling, so the
                     # provider is held to it rather than trusted to be
                     # typical.
                     "max_output_tokens": int(CFG.ALPHA_MAX_OUTPUT_TOKENS)},
            timeout=timeout)

    def _extract(self, response: dict) -> tuple:
        if not isinstance(response, dict):
            raise ProviderError(f"response is {type(response).__name__}")
        status = response.get("status")
        if status in ("failed", "cancelled"):
            detail = ((response.get("error") or {}).get("message")
                      if isinstance(response.get("error"), dict) else "")
            raise ProviderError(f"response status {status}: {detail}"[:200])
        if status == "incomplete":
            reason = ((response.get("incomplete_details") or {}).get("reason")
                      if isinstance(response.get("incomplete_details"), dict)
                      else "")
            # A truncated answer is not a partial probability: the JSON will
            # not parse, and pretending otherwise is how half an opinion
            # becomes a whole one.
            raise ProviderError(f"response incomplete: {reason}"[:200])
        text = response.get("output_text")
        tool_calls = 0
        if not isinstance(text, str) or not text.strip():
            parts, output = [], response.get("output")
            if not isinstance(output, list) or not output:
                raise ProviderError("no output in response")
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") and item.get("type") != "message":
                    tool_calls += 1          # reasoning, tool and search items
                    continue
                for chunk in item.get("content") or []:
                    if isinstance(chunk, dict) and isinstance(
                            chunk.get("text"), str):
                        parts.append(chunk["text"])
            text = "".join(parts)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("empty completion")
        usage = response.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        return text, {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cached_input_tokens": int(details.get("cached_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "tool_calls": tool_calls,
            "search_queries": 0,
            "billed": self._billed(usage),
        }


class OpenAIProvider(_ResponsesAPI):
    """OpenAI through the Responses API."""

    name = "openai"
    env_key = ENV_OPENAI
    base_url_attr = "ALPHA_OPENAI_BASE_URL"
    path_attr = "ALPHA_OPENAI_RESPONSES_PATH"

    def default_model(self) -> str:
        return CFG.ALPHA_OPENAI_MODEL


class GrokProvider(_ResponsesAPI):
    """xAI through its Responses API.

    Preferred over the chat-completions surface because it is where xAI
    reports `usage.cost_in_usd_ticks` -- an authoritative billed amount,
    which is better evidence than any token estimate of ours.
    """

    name = "grok"
    env_key = ENV_GROK
    base_url_attr = "ALPHA_GROK_BASE_URL"
    path_attr = "ALPHA_GROK_RESPONSES_PATH"

    def default_model(self) -> str:
        return CFG.ALPHA_GROK_MODEL

    def _billed(self, usage: dict) -> dict:
        return _xai_billed(usage)


class GeminiProvider(AlphaProvider):
    """Gemini's generateContent surface differs enough to warrant its own
    adapter: the key travels in a header, the body is `contents`, and the
    token counts live under `usageMetadata`."""

    name = "gemini"
    env_key = ENV_GEMINI

    def default_model(self) -> str:
        return CFG.ALPHA_GEMINI_MODEL

    def configured(self) -> bool:
        return bool(os.getenv(ENV_GEMINI, "").strip()
                    or os.getenv(ENV_GEMINI_FALLBACK, "").strip())

    def _api_key(self) -> str:
        key = (os.getenv(ENV_GEMINI, "").strip()
               or os.getenv(ENV_GEMINI_FALLBACK, "").strip())
        if not key:
            raise ProviderError(f"{ENV_GEMINI} is not set")
        return key

    def _call(self, prompt: str, timeout: float) -> dict:
        base = CFG.ALPHA_GEMINI_BASE_URL.rstrip("/")
        return self._post(
            f"{base}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self._api_key(),
                     "Content-Type": "application/json"},
            payload={"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {
                         "responseMimeType": "application/json",
                         # Held to the ceiling the budget was checked against.
                         "maxOutputTokens": int(CFG.ALPHA_MAX_OUTPUT_TOKENS)}},
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
        grounding = (candidates[0] or {}).get("groundingMetadata") or {}
        return text, {
            "input_tokens": int(usage.get("promptTokenCount") or 0),
            "cached_input_tokens": int(usage.get("cachedContentTokenCount") or 0),
            # Thinking tokens are billed at the output rate, so they are
            # counted as output rather than quietly omitted.
            "output_tokens": int((usage.get("candidatesTokenCount") or 0)
                                 + (usage.get("thoughtsTokenCount") or 0)),
            "tool_calls": len((candidates[0] or {}).get("toolCalls") or []),
            "search_queries": len(grounding.get("webSearchQueries") or []),
            "billed": {},
        }


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

    def _probe(self, timeout: float) -> None:
        """In-process: reachable by construction. The health report still
        distinguishes 'wired' from 'reachable' -- an unwired quant model is
        healthy AND produces INSUFFICIENT_EVIDENCE, which is the honest
        state, not a failure."""
        return None

    def health_check(self, timeout: float = None) -> dict:
        report = super().health_check(timeout)
        report["wired"] = self.estimator is not None
        if not report["wired"]:
            report["detail"] = (report["detail"] + "; no quantitative "
                                "estimator wired: every signal will be "
                                "INSUFFICIENT_EVIDENCE").strip("; ")
        return report

    def analyze(self, snapshot: MarketSnapshot, timeout: float) -> tuple:
        started = time.monotonic()
        meta = {"provider": self.name, "model": self.model, "latency_ms": 0,
                "cost": _cost_row(self.name, self.model, 0, 0),
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
            # In-process and credential-free, but the redaction is uniform
            # so no failure path in this module is the exception.
            meta["error"] = redact(f"{type(e).__name__}: {e}")
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
