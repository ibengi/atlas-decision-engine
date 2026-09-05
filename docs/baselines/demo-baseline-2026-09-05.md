# DEMO baseline for Phase 10 (captured before the LIVE READ_ONLY cutover)

Source: Railway deploy logs, deployment 2a3da86f (main@9b906e8, DEMO),
window 2026-09-05T09:00Z .. 16:59Z. Raw extracts sit next to this file.
Nothing here is evidence of edge. It is the reference the LIVE shadow
stream will be compared against.

## Cycle shape (468 cycles, min / mean / max)

| metric | min | mean | max |
|---|---|---|---|
| scanned_raw | 202 | 202.0 | 203 |
| open_cached | 80 | 85.6 | 151 |
| liquid | 22 | 22.9 | 24 |
| supported | 2 | 8.3 | 22 |
| model_evaluated | 2 | 8.1 | 21 |
| positive_edge | 0 | 3.4 | 6 |
| positive_net_ev | 0 | 3.2 | 5 |
| risk_passed | 0 | 0 | 0 |
| orders_submitted | 0 | 0 | 0 |
| fills | 0 | 0 | 0 |
| cycle_duration_ms | 326 | 616 | 5594 |

Rejections, summed over the window: outside_time_window 53780,
no_liquidity 29463, cached 2462, no_positive_edge 2200,
daily_oracle_unapproved 1382, closed_or_settled 548, closes_too_soon 155,
insufficient_gross_edge 140, insufficient_net_edge 47, spread_too_wide 25.

risk_passed is zero in every cycle. Every positive-EV candidate is stopped
by daily_oracle_unapproved (the KXBTCD quarantine) or by 15m gates.

## Data providers

Binance klines: 468 of 468 cycles HTTP 451, accepted=false. No other
provider is logged at INFO, and no "all providers unavailable" warning
appears, so a fallback (kraken or coinbase, per btc_context.py) supplied
accepted data every cycle. Which one is UNOBSERVABLE from Railway logs:
accepted providers log at DEBUG only. data_quality_score, klines:source,
spot_sources and source_dispersion are computed on the context object but
never logged. Phase 7 provenance fields will therefore be MISSING on the
LIVE stream until telemetry is added (separate reviewed change).

## Liquidity

220 LIQUIDITY_REJECT lines sampled 16:40Z..17:00Z: 200 with an empty book
(best_bid=None, best_ask=None, volume 0, open_interest 0), 20 unparsed.
DEMO books for far strikes are empty. Spread/depth statistics on DEMO are
therefore not a usable baseline. LIVE books are the first real read.

## Candidates (15:00Z..17:00Z)

349 CANDIDAT lines over 8 tickers, all KXBTCD daily, zero KXBTC15M.
edge_net mean 0.201, median 0.215, min 0.03, max 0.501. Confidence is 9
on all 349. Side: yes 296, no 53. Most common prices 62c, 72c, 41c, 77c.

A confidence field that never varies carries no information. Edge values
of 0.2 to 0.5 on a daily contract with an unapproved settlement oracle are
model output, not edge. Do not read them as such.

## Model trace (16:00Z..17:00Z)

443 MODEL_TRACE lines: 441 btc_above_strike_daily, 2 btc_15m_above_strike,
all model=btc15m-v1.0-ref executed=true reason=ok. The daily market type
is being scored by the 15m reference model. The 15m strategy produced two
evaluations in an hour and zero candidates.

## What LIVE must show before any scientific claim

1. Which provider actually supplies klines when Binance returns 451.
2. Non-empty books on at least the near-strike daily and 15m contracts.
3. Decision frequency on 15m under LIVE books versus the two-per-hour DEMO rate.
4. Model probability versus market mid on LIVE, per contract, per cycle.
5. Settlement from a BRTI-based oracle for daily. DEMO market.result is not
   ground truth and is not used.

KXBTCD stays quarantined. The model is not promoted on the strength of
LIVE data existing.
