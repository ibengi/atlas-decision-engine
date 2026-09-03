# Order gating — what is guarded, and what was actually verified

Scope: the two safety gates added for B1/B2 (`151860a`) and the ticker
normalization fix that follows it. Nothing here changes model logic, risk
sizing, thresholds, or the deployed service. `ALLOW_ORDER_SUBMISSION` stays
`false` in production and no variable was touched.

## The gates

| Gate | Where | Keyed on |
| --- | --- | --- |
| Global submission hold | `order_manager.place_and_track` | `CFG.ALLOW_ORDER_SUBMISSION` |
| Daily (KXBTCD) quarantine — engine | `execution_engine._execute_decision` | ticker **and** `market_type` allowlist |
| Daily (KXBTCD) quarantine — money path | `order_manager.place_and_track` | ticker only |
| Unclassifiable ticker | both of the above | ticker shape |

Both daily guards call one function, `config.daily_quarantine_blocks`, which
calls one resolver, `config.daily_oracle_approved`. Approval is therefore
necessary *and* sufficient: there is no second list to edit, and editing a
list without approving the oracle changes nothing.

## Ticker canonicalization

`config.canonical_ticker` trims **border** whitespace and folds case. That is
all it does, and the limit is deliberate:

* Trimming borders closes the real bypass. Before the fix the classifier was a
  bare `str(ticker or "").upper().startswith("KXBTCD")`, so `" KXBTCD-…"` — one
  leading space, a tab, a newline — classified as *not daily* and walked
  through **both** guards to `create_order`. The upstream classifier
  (`market_classifier._root`) already tolerated that whitespace, so a decision
  could carry `market_type="btc_above_strike_daily"` while the gates saw a
  ticker they did not recognise.
* Trimming *inside* a ticker is refused. Kalshi tickers contain no blanks, so
  collapsing `"KX BTCD-…"` into `"KXBTCD-…"` would manufacture a valid ticker
  out of an invalid string — precisely the transformation a bypass would want.

Anything that cannot be classified at all — `None`, `""`, blanks, a non-`str`
object, or a leading invisible character `strip()` does not remove (U+200B,
U+2060) — is refused before `create_order` rather than passed to the broker.
Bytes are refused there too, and are *still* classified as daily by
`is_daily_ticker`, so the quarantine holds at both layers.

The canonical form is a decision key only. The broker call keeps the original
ticker; nothing is rewritten on the way out.

## Verification performed

Repository state: branch `claude/railway-atlas-inspection-1gtkyf`.

* `pytest tests` — **795 passed, 142 subtests**, run twice back to back.
* Three genuinely shuffled module orders (seeds 1, 7, 31337) — 795 passed each.
  `pytest-randomly` is **not installed**; the shuffle is done by shuffling the
  file arguments, which is the order pytest collects in.
* Adversarial collection orders — the ten modules that do not `import
  _bootstrap` first, with `tests/test_daily_quarantine.py` last; then the same
  set with it first; then that file alone. 795/795/50 passed.
* Clean-environment defaults are asserted **behaviourally**, in a subprocess
  whose environment has both gate variables removed, so the permissive
  `tests/conftest.py` cannot mask a regression in the shipped defaults.
* 12 mutations applied one at a time, each expected to fail the safety tests:

  | # | Mutation | Killed |
  | --- | --- | --- |
  | M1 | ticker `.strip()` removed | yes |
  | M2 | ticker `.upper()` removed | yes |
  | M3 | engine daily quarantine removed | yes |
  | M4 | order-manager daily quarantine removed | yes |
  | M5 | `ALLOW_ORDER_SUBMISSION` default `False`→`True` | yes |
  | M6 | daily approval default `False`→`True` | yes |
  | M7 | malformed env value read as TRUE | yes |
  | M8 | order-manager malformed-ticker guard removed | yes |
  | M9 | engine malformed-ticker guard removed | yes |
  | M10 | non-`str` ticker accepted as well-formed | yes |
  | M11 | daily prefix test loosened to exact match | yes |
  | M12 | engine allowlist turned into a denylist | yes |

  M5, M6, M8 and M9 are each caught by a source-text assertion *and*, checked
  separately with that assertion allowed to pass, by a behavioural test
  (`test_absent_variables_ship_closed`, `test_the_two_gates_are_read_independently`,
  `test_config_state_is_identical_whatever_is_imported_first`,
  `test_place_and_track_refuses_every_unclassifiable_value`,
  `test_engine_refuses_unclassifiable_before_reading_a_book`).

## Erratum — commit `151860a`

That commit's message ends: *"770 tests pass, three full runs including
randomised order."*

**The "randomised order" half is false.** `pytest-randomly` is not installed in
this environment, and `-p no:randomly` is accepted silently for a plugin that
does not exist, so all three of those runs were in pytest's ordinary
alphabetical collection order. The 770-pass count is accurate.

The message is left as written rather than rewritten, so the record shows what
was claimed and this note shows what was true. The shuffled and adversarial
orders listed above are the first genuine ones, and they were run against the
current head, not against `151860a`.

## Residual risks

* **Prefix matching is a naming bet.** `KXBTCD` covers today's series. A future
  Kalshi renaming (`KXBTC-D-…`, `KXBTCDAILY-…`) would not match, and the daily
  quarantine would go quiet without failing. The engine's `market_type`
  allowlist covers that case as long as `market_classifier` is updated too;
  the money-path guard would not.
* **`tests/conftest.py` enables both gates in-process** so ~130 unrelated
  order-plumbing tests that use a KXBTCD ticker as a generic example keep
  working. Every assertion about a shipped default is therefore made in a
  subprocess with those variables scrubbed; `CleanEnvironmentDefaults` also
  asserts that the scrub really happened, so the guard cannot rot silently.
* **Approval is still a placeholder flag.** `daily_oracle_approved()` reads one
  boolean. It must become a derived, hash-pinned validation artifact (BRTI feed
  provenance, per-market contract rules, reproducible computation, ladder
  monotonicity, clean OOS sample) before the daily strategy executes anything.
