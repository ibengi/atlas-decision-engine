# Candle integrity HIGH remediation

Scope: candidate remediation of deployed base
`033fb43e7858594e7f3d62844830f935bd0275c5`. This document records offline
verification only. It does not approve a model, authorize deployment, qualify a
settlement oracle, or establish live eligibility.

## Independent reproduction and root cause

At fixed synthetic time `1800748800`, thirty varying positive closes on each of
these timestamp sequences yielded both `BtcMarketContext.valid=True` and a valid
`BtcModelStrategy` output on the base release:

| Sequence | Last candle open | Cadence |
| --- | --- | --- |
| Valid timing control | now - 60 seconds | 60 seconds |
| Future | now + 86400 seconds | 60 seconds |
| Hourly interpreted as minute | now - 60 seconds | 3600 seconds |
| Wrong cadence | now - 60 seconds | 17 seconds |

The fallback accepted and cached any response with at least 11 entries before
validating it. Context construction filtered bad closes and checked only
monotonic timestamps and a one-sided latest-age bound. It did not establish
OHLCV completeness, exact cadence, completed intervals or provider provenance.
The cycle context memo returned old valid objects without checking expiration;
a separate stale fallback deliberately fed stale volatility into the model.

## Narrow data-boundary changes

`btc_context.py` now validates the entire input batch before computing model
features. It requires finite numeric OHLCV, positive prices, nonnegative volume,
consistent high/low ranges, UTC minute-aligned opens, consecutive 60-second
intervals, exact exclusive close times, closed intervals, the existing fresh
age limit of 180 seconds, and at least the existing 11-candle model history.
Malformed rows are never filtered, repaired, interpolated or sorted into shape.

Each normalized row carries a binding to the adapter's fixed provider, endpoint,
product, 60-second interval, open-time semantics and request-start observation
time. The entire batch must share that binding, and each interval must already
have closed at observation time. Provider transport accepts HTTP 200 only,
disables redirects, rejects Content-Range even with HTTP 200, checks the actual
response origin, and rejects transport
errors or unsupported metadata. JSON decoding recursively rejects duplicate
object members and nonstandard NaN/Infinity tokens before they can erase a
provider error or yield an ambiguous envelope. Kraken requires its complete known response
envelope and an empty explicit error array.

Adapters validate complete raw response structure before taking the requested
history. Wire timestamps require their documented integer JSON types before
float conversion; fractional strings, floats and booleans cannot be rounded
into an apparently valid minute boundary. JSON numeric decimals and exchange
numeric strings retain exact Decimal values until OHLCV sign/range validation;
nonzero underflow is rejected before converting validated values to model floats. Only the recognized current open tail is excluded; Kraken's final
uncommitted row is always excluded. They reject malformed open tails too. A
malformed source may fall through to an independently valid provider. It cannot
poison the last-good cache. Unavailable providers no longer authorize stale
cache use. Both raw cache hits and context memo expiration preserve data age
limits; slow candle fetches also trigger spot freshness/consensus recomputation.

Provider schema sources, checked September 24, 2026:

- [Binance spot klines](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints): one-minute request, UTC opens and millisecond inclusive close time.
- [Kraken OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data): minute interval and final uncommitted row.
- [Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles): bucket-start timestamps, 60-second granularity, OHLCV layout; missing-tick intervals are not filled in.

These public candles are model inputs, not BRTI settlement authority.

## Permanent regressions and mutation checks

`tests/test_candle_integrity.py` exercises raw adapter responses, fallback,
context caches, and both BTC strategy boundaries. Negative strategy tests spy
on the actual probability function and require zero calls; positive controls
require a call for both strategies. Existing provider/cache fixtures now supply
realistically aligned closed synthetic candles with explicit provenance. The
old test that required stale data to keep decisions running now requires
rejection. An accidental real network call in the old fallback test was removed.

Local targeted command:

```sh
python -m pytest tests/test_candle_integrity.py tests/test_btc_provider.py tests/test_btc_context_ttl.py tests/test_performance.py -q
python tools/candle_integrity_mutations.py
```

Result before integration: 80 tests and 121 subtests passed; 24/24 targeted
mutants killed. Mutations run in disposable copies and separately remove
cadence, alignment, closure, close-time, freshness, count, provenance bindings,
cache expiration, stale refusal, uncommitted-tail exclusion, Kraken envelope,
transport origin/status, metadata, duplicate-JSON-member and nonstandard-JSON
constant, raw timestamp type, exact raw decimal bounds/precision and nonzero
underflow guards. The mutation launcher first
requires an unmodified passing baseline and does not count process errors or
timeouts as kills.

Canonical full suite and exact candidate-SHA hosted CI remain integration gates
and are recorded by the integrating operator separately. No broker-write code,
risk limit, approval flag, credential, ledger or production configuration is
changed by this remediation.
