# LI-06: candle qualification — separate production review

**Status: READY_FOR_SEPARATE_PRODUCTION_REVIEW. Undeployed.**

Base: `5c4e7897a0b99065f3a23bc4ed834b44dba9580c` (the independently captured production revision). Local review branch: `review/li06-candle-qualification`.

This patch establishes synthetic refusal and explicit data provenance. It does not qualify a live fallback, resolve Binance HTTP 451 operationally, grant broker authority, or prove an atomic exchange-side market-data freeze.

## Reproduced defects and invariant-level changes

| Original synthetic case | Base | Review candidate |
|---|---|---|
| Fresh Binance complete minute window | Model valid | Valid |
| Binance 451, fresh Kraken fallback | Model valid | Valid |
| Complete outage, no cache | Refused | Refused |
| Complete outage, three-minute cached volatility | Model valid | Refused |
| Complete outage, expired cache | Refused | Refused |
| Candles 365 days in the future | Model valid | Refused |
| Hourly candles treated as minutes | Model valid | Refused |
| Stale Binance response precedes fresh Kraken | Stale first source prevents fallback | Bad first source rejected; fresh fallback evaluated |

`tools/li06_review/run_original_witnesses.py` reconstructs the original files from the local base Git object and runs all eight assertions before and after the patch. It never fetches Git objects or provider data. The original unsafe assertions remain in the permanent harness; they are not replaced with retained JSON claims.

The shared candle validator rejects malformed containers/members, missing OHLCV/timestamps, booleans, strings in the normalized contract, nonfinite/overflow values, nonpositive prices, negative volume, impossible OHLC ranges, off-grid timestamps, future observations, duplicates, reversed/missing intervals and any cadence other than 60 seconds. Numeric wire strings are accepted only in adapters, before strict normalized validation.

All relevant wire rows are parsed and validated before retaining the requested last 30 bars. Binance's explicit close timestamp must equal its open timestamp plus 59,999 milliseconds; its expected 12-column envelope is checked. Kraken requires an empty typed error list and the requested `XXBTZUSD` identity with its continuation value. Coinbase's documented descending order is verified and reversed; arbitrary sorting cannot repair contradictory order. Unknown/error envelopes cannot become successful data.

Closedness and freshness use distinct clocks. A bar must already have completed at the conservative request-start cutoff. A response received or parsed across a minute boundary cannot turn its then-partial bar into a completed observation. After provider I/O, the last closed bar must be at most 120 seconds old, and spot observations are revalidated under the existing 90-second bound. The minimum remains 11 closed consecutive bars. Outage cache never supplies executable volatility. Ordinary raw/cycle caches remain bounded by each observation's current validity; if one spot source expires, source count and quality are recomputed.

## Full deployed executable dependency graph

1. `btc_context.fetch_klines_binance/kraken/coinbase` → `fetch_klines_with_fallback` → `get_btc_context`: normalized closed bars produce realized one-minute volatility and five-minute return. Spot sources produce consensus/distance. No candle provider produces trading authority.
2. `strategy_router._BtcAboveStrikeBase.evaluate` is shared by `BtcModelStrategy` (15-minute) and `BtcDailyStrategy`. It consumes qualified context in `btc_probability_model.probability_yes`; the daily extended-horizon volatility floor and confidence caps are unchanged.
3. `strategy_router.evaluate_gates` rejects invalid model output. The production market pipeline collects accepted decisions; `ExecutionEngine._finish_cycle` later calls `_execute_decision`. Pipeline evaluation, balance waits, refreshed books, risk evaluation and logging can consume time, so initial model validity alone is insufficient.
4. `_execute_decision` validates the retained input proof before book access, before sizing, before READ_ONLY `WOULD_SUBMIT`, and before order-manager handoff. Dependency is determined from both market type and canonical BTC ticker prefix; renaming a strategy/model label cannot bypass it.
5. A qualification callback continues through `OrderManager.place_and_track`, after durable intent writes, into `KalshiClient.create_order` and `_req`. It is checked after signing, immediately before **every local `session.request` attempt**. Callback failure, exception or a merely truthy nonboolean result refuses that attempt. No external call occurs inside the callback.

`btc_strategy.signal/decide` are legacy paths that refuse because `evaluate_btc_trade` is absent from this exact deployed context implementation. `get_btc_price` provides spot selection only. `btc_daily_shadow` uses the same model after execution decisions for research evidence; it cannot submit. Repository research candle fetch/backtest paths do not supply this execution pipeline. Other registered sports/election providers do not consume these BTC candles. Production daily quarantine, execution allowlist, risk limits, kill switch and money authority guards remain in place.

## Provenance and transport uncertainty

Model features retain provider endpoint, instrument and quote currency, policy/schema, exact normalized candle preimage and SHA-256, observed window/count, validation time, and earliest input expiry. Binance BTC/USDT is explicitly distinguished from Kraken/Coinbase BTC/USD; the patch does not assert economic interchangeability. Before use, the validator checks source identity, reconstructs normalized evidence and digest, and binds the model's sigma/five-minute return to those exact rows. Missing legacy proof fails closed.

The data qualification callback is additional refusal authority only; it does not grant financial permission. Existing broker write checks still run first. Its local dispatch guarantee ends immediately before `session.request`; DNS/TLS/network transit and exchange acceptance are outside the observable atomic boundary.

If qualification expires before any transport attempt, the manager preserves the intent row with `resolution=CLOSED_ABSENT` and `closure_source=local_transport_not_started`. This is an explicit **local non-send** statement, not invented broker absence. Existing submission cooldown remains. Failed persistence still invokes existing persistence refusal behavior.

If any earlier attempt could have reached the broker, the expiry exception retains `request_started=True`; existing intent reconciliation remains authoritative, and no local-absence closure is written. The new result has `ambiguous:candle_expired_after_send:*` status and retains the half-open risk reservation. A proven pre-send expiry may release that reservation and does not count as a submitted order. No unresolved intent is deleted to regain authority. General pre-existing non-expiry ambiguous-result half-open handling is outside this patch and remains a separate review item.

## Exact affected money-path files

- `btc_context.py`: shared validation, observation cutoff, fresh fallback/cache policy, retained evidence and input-proof verification.
- `strategy_router.py`: retains candle proof in model output.
- `execution_engine.py`: input checks at use points, callback propagation, explicit new expiry accounting/risk distinction.
- `order_manager.py`: checks after persistence; propagates transport qualification; preserves local non-send versus prior-attempt ambiguity.
- `kalshi_client.py`: checks after signing before each local request attempt; explicit started/never-started expiry exception.

No `risk_manager.py`, position sizing, broker credentials, runtime authority, production configuration or historical ledger is modified. Existing fixture changes give the tests complete minute-grid candles and genuine synthetic proof so their unrelated positive paths continue to exercise their intended gates. The old stale-cache-acceptance test now asserts refusal. An accidental real-data call before provider injection in the old provider test was removed.

## Reproduction and validation

From the review checkout (with dependencies installed), all invocations below are synthetic. Use the repository's isolated test launcher supplied with the integration evidence when running the full canonical suite.

```bash
python -m unittest discover -s tests -p test_li06_candle_qualification.py -v
python tools/li06_review/run_original_witnesses.py /tmp/li06-original-witness-results
python tools/li06_review/recheck.py
python tools/li06_review/open_bar_timing.py
python tools/li06_review/transport_recheck.py
python tools/li06_review/manager_recheck.py
python tools/li06_review/run_mutations.py /tmp/li06-mutation-results.json
```

Final canonical suite: **1,000 passed, zero failures/errors/skips** (954 original tests plus 46 new regression methods). The eight original before/after cases and 33 independent reviewer witnesses are supplemental executions, with overlap; they are not added to inflate the canonical count.

All 16 final bounded mutations were KILLED_BEHAVIORALLY; zero effective survivors and zero inconclusive results remain. The mutation runner applies 16 bounded mutations, each against a named semantic assertion and a passing unchanged-copy control. Failure/assertion and error/setup/import paths are classified separately. A first broad transport mutation witness produced an unhandled simulated timeout and was classified **INCONCLUSIVE_SETUP**, never a behavioral kill; the retained final sign-stall witness directly tests that no local request occurs after expiry. These 16 mutations are not an exhaustive coverage claim.

Independent review discovered and retained additional counterexamples for response truncation, close-time contradiction, error envelopes, parse-time partial-bar promotion, provider-delay expiry, dispatch/signing/retry expiry and half-open risk reservation. All tests use local rows, memory doubles or disposable state. Real network requests, broker writes, credential changes, main changes, production changes and historical ledger rewrites during this work are zero.

## Remaining qualification work

LI-06 is **not CLOSED**. The five money-path files require separate review and authorization before any deployment. The deployed service still runs the old revision. Live source observation, pair/economic suitability, actual fallback availability and ongoing rejection behavior must be captured and qualified after an approved deployment. No `BINANCE_DEPENDENCY_RESOLVED` or `QUALIFIED_FALLBACK_PROVEN` claim is made from synthetic tests. Local code proves only the bounded synthetic dependent-decision refusal described above.
