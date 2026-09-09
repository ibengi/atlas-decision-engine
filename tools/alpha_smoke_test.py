#!/usr/bin/env python3
"""ONE bounded real call per provider. SHADOW ONLY, NO BROKER OPERATION.

    python tools/alpha_smoke_test.py                 # all three
    python tools/alpha_smoke_test.py --provider grok
    python tools/alpha_smoke_test.py --json

WHAT THIS IS FOR
    Section 4: after credentials are installed, prove each provider is
    actually usable before turning on an automatic shadow session. It makes
    exactly ONE call per provider against a fixed, harmless fixture -- a
    settled question about a market that does not exist -- and reports, per
    provider:

        reachable / model exists / request schema accepted
        structured response parsable
        usage captured / latency captured / cost captured

    Every failure is reported as it happened. Nothing here retries, falls
    back to another model, or substitutes a default: an unusable provider is
    EXCLUDED, and knowing that BEFORE a shadow session starts is the whole
    point of running this.

WHAT IT COSTS
    One call per provider, bounded by `ALPHA_MAX_OUTPUT_TOKENS`, charged
    against the same budget ledger the service uses -- so a smoke test run
    counts toward the daily cap exactly like a real analysis, and cannot be
    used to sidestep it.

THE xAI TICK SCALE
    xAI reports `usage.cost_in_usd_ticks`. This repository cannot verify the
    tick denomination, so the raw count is recorded and no USD figure is
    derived until `ALPHA_XAI_COST_TICKS_PER_USD` is set. When a grok call
    succeeds, this tool prints the observed ratio

        cost_in_usd_ticks / (our estimated USD)

    which is what that constant should be set to. One real call answers a
    question no amount of guessing would.

SAFETY
    This process holds no broker credential (startup refuses if one is
    visible) and no broker client. It performs no order operation of any
    kind.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_cost import BudgetGuard, budgeted_cost                # noqa: E402
from alpha_providers import (default_providers, set_pricing_table)  # noqa: E402
from alpha_schema import validate_signal                         # noqa: E402
from alpha_service import (BrokerCredentialsPresent,              # noqa: E402
                           assert_no_broker_credentials)
from alpha_snapshot import build_snapshot                         # noqa: E402
from logging_config import setup_logging                          # noqa: E402

#: A fixed, harmless fixture. Deliberately a market that does not exist, so
#: no vendor answer can be mistaken for a real forecast and no real contract
#: is described to a third party.
FIXTURE_CONTRACT = "ATLAS-SMOKE-TEST-0001"


def fixture_snapshot():
    now = datetime.now(timezone.utc)
    return build_snapshot(
        contract_id=FIXTURE_CONTRACT, event_id="ATLAS-SMOKE",
        question=("Synthetic connectivity fixture: will a fair coin flipped "
                  "once at the stated time land heads?"),
        resolution_rules=("Resolves YES if the flip lands heads. This is a "
                          "connectivity fixture, not a real market."),
        resolution_source="synthetic",
        yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51,
        volume=0.0, open_interest=0.0,
        market_close_time_utc=(now + timedelta(hours=6)).isoformat(),
        expected_resolution_time_utc=(now + timedelta(hours=7)).isoformat())


def probe(provider, snapshot, guard) -> dict:
    """One bounded call. Returns a verdict; never raises."""
    result = {
        "provider": provider.name, "model": provider.model,
        "credential_present": provider.configured(),
        "pricing_configured": False, "budget_allowed": False,
        "called": False, "reachable": None, "http_ok": None,
        "response_parsable": None, "schema_valid": None,
        "usage_captured": None, "latency_captured": None,
        "cost_captured": None, "verdict": "NOT_EXECUTED", "detail": "",
        "usage": None, "cost": None, "latency_ms": None,
        "p_yes": None, "xai_ticks_per_usd_observed": None,
    }
    if not result["credential_present"]:
        result["detail"] = f"{provider.env_key} is not set"
        return result

    from alpha_providers import build_prompt
    verdict = guard.check(provider.name, provider.model,
                          prompt_chars=len(build_prompt(snapshot)))
    result["pricing_configured"] = verdict["estimate"]["cost_priced"]
    result["worst_case_cost_usd"] = verdict["estimated_cost_usd"]
    if not verdict["allowed"]:
        result["verdict"] = "REFUSED_" + str(verdict["reason"]).upper()
        result["detail"] = verdict["detail"]
        return result
    result["budget_allowed"] = True

    result["called"] = True
    raw, meta = provider.analyze(snapshot, float(os.getenv(
        "ALPHA_SMOKE_TIMEOUT_S", "60")))
    result["latency_ms"] = meta.get("latency_ms")
    result["latency_captured"] = isinstance(meta.get("latency_ms"), int)
    cost = meta.get("cost") or {}
    result["cost"] = cost
    guard.record_actual({**cost, "outcome": "SMOKE_TEST"})

    if meta.get("error"):
        result["reachable"] = "connection" not in str(meta["error"]).lower()
        result["http_ok"] = False
        result["verdict"] = "FAIL"
        result["detail"] = str(meta["error"])[:400]
        return result

    result["reachable"] = True
    result["http_ok"] = True
    result["response_parsable"] = raw is not None
    signal = validate_signal(raw, snapshot, provider=provider.name,
                             model=provider.model,
                             latency_ms=meta.get("latency_ms", 0), cost=cost)
    result["schema_valid"] = signal.valid
    result["p_yes"] = signal.p_yes
    usage_fields = ("input_tokens", "output_tokens")
    result["usage"] = {k: cost.get(k) for k in
                       ("input_tokens", "cached_input_tokens",
                        "output_tokens", "tool_calls", "search_queries")}
    result["usage_captured"] = any(int(cost.get(f) or 0) > 0
                                   for f in usage_fields)
    result["cost_captured"] = bool(cost.get("cost_priced")) or \
        cost.get("billed_cost_usd") is not None
    result["billed_cost_usd"] = cost.get("billed_cost_usd")
    result["billed_cost_raw"] = cost.get("billed_cost_raw")
    result["estimated_cost_usd"] = cost.get("api_cost_usd")
    result["budgeted_cost_usd"] = budgeted_cost(cost)

    # The one number a real xAI call can tell us that nothing else can.
    raw_billed = cost.get("billed_cost_raw") or {}
    ticks = raw_billed.get("value")
    estimated = cost.get("api_cost_usd")
    if isinstance(ticks, (int, float)) and isinstance(estimated, (int, float)) \
            and estimated > 0:
        result["xai_ticks_per_usd_observed"] = round(ticks / estimated, 2)

    if not result["schema_valid"]:
        result["verdict"] = "REACHABLE_BUT_INVALID"
        result["detail"] = (f"{signal.rejected_reason}: "
                            f"{signal.rejected_detail}")[:400]
        return result
    result["verdict"] = "PASS"
    return result


def main(argv=None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", action="append",
                    help="restrict to these providers (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        assert_no_broker_credentials()
    except BrokerCredentialsPresent as e:
        print(str(e), file=sys.stderr)
        return 78

    guard = BudgetGuard()
    set_pricing_table(guard.pricing)
    snapshot = fixture_snapshot()
    wanted = set(args.provider or [])
    results = []
    for provider in default_providers():
        if provider.name == "atlas_quant":
            continue                     # in-process; nothing to smoke test
        if wanted and provider.name not in wanted:
            continue
        results.append(probe(provider, snapshot, guard))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixture_contract": FIXTURE_CONTRACT,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "broker_operations": 0,
        "pricing_version": guard.pricing.version,
        "results": results,
        "budget_after": guard.snapshot(),
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"fixture {FIXTURE_CONTRACT}  snapshot "
              f"{snapshot.market_snapshot_id}\n")
        for row in results:
            print(f"{row['provider'].upper()} ({row['model']})")
            print(f"  verdict            {row['verdict']}")
            print(f"  credential present {row['credential_present']}")
            print(f"  pricing configured {row['pricing_configured']}")
            print(f"  called             {row['called']}")
            print(f"  reachable          {row['reachable']}")
            print(f"  response parsable  {row['response_parsable']}")
            print(f"  schema valid       {row['schema_valid']}")
            print(f"  usage captured     {row['usage_captured']}  "
                  f"{row['usage']}")
            print(f"  latency captured   {row['latency_captured']}  "
                  f"{row['latency_ms']} ms")
            print(f"  cost captured      {row['cost_captured']}  "
                  f"estimated={row.get('estimated_cost_usd')} "
                  f"billed={row.get('billed_cost_usd')} "
                  f"raw={row.get('billed_cost_raw')}")
            if row.get("xai_ticks_per_usd_observed"):
                print(f"  >>> set ALPHA_XAI_COST_TICKS_PER_USD="
                      f"{row['xai_ticks_per_usd_observed']}")
            if row["detail"]:
                print(f"  detail             {row['detail']}")
            print()
        print(f"broker operations: 0    daily spend now: "
              f"${report['budget_after'].get('spent_today_usd')}")
    return 0 if all(r["verdict"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
