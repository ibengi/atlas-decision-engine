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

## Build-gate compatibility (the independent review's blocker)

An independent review found that the branch could not produce a Docker image:
`run_tests.py` uses `unittest.TestLoader().discover("tests")` and never loads a
`conftest.py`, so the gate defaults the suite needs lived in a file only pytest
reads. Measured, gate variables absent: `python run_tests.py` exited 1 with 88
failures and 47 errors, while `pytest` was green — the same suite red under the
runner that stops the build and green under the one that does not.

The fix adds **no production change**. `tests/_gates.py` holds the two
`os.environ.setdefault` calls plus the dummy-credential bootstrap, and both
entry points import it: `tests/conftest.py` for pytest, and `run_tests.py`
immediately before discovery (order matters — `config` freezes its class
attributes at import). `setdefault`, never assignment, so a run that
deliberately sets a gate to `false` stays closed.

| Runner, gate variables absent | Result |
| --- | --- |
| `python run_tests.py` | exit 0, ran 794, failures 0, errors 0 (twice) |
| `pytest tests` | 799 passed + 142 subtests (twice) |
| 3 shuffled module orders, 1 adversarial order | 799 passed each |

Four tests pin this so it cannot regress silently: importing `run_tests` in a
scrubbed subprocess must set both variables; setting them explicitly must not
be overridden; the import must appear before `loader.discover`; and the
plumbing may not import `config` or write `CFG`. Mutations M13–M16 (import
removed, import moved after discovery, either `setdefault` turned into an
assignment) are all killed.

## Restart harness

The same review found `tools/restart_harness.py` degraded from 16/16 to 15/16:
with the global gate closed by default, its seed order was refused and the
duplicate-submission invariant — the defence against the 2026-07-25 eight-fill
incident — never executed.

The harness now opts in **explicitly and narrowly**: `bot.CFG.ALLOW_ORDER_SUBMISSION
= True` on that one process's config object. No environment variable is written,
so nothing escapes to a subprocess; the broker is a `MagicMock`; the ticker is
the synthetic `KXTEST-CANARY-T1`; and `DAILY_RESEARCH_ORACLE_APPROVED` is left
alone, so the KXBTCD quarantine stays fully in force inside the harness.

A new `[PRE]` check asserts the seed order actually reached the broker
(`create_order` called once, the dedup guard armed). Without it the harness could
go **vacuous** rather than red — comparing an empty guard to an empty guard and
passing for the wrong reason. Verified by removing the opt-in: 15/17 with both
the `[PRE]` check and `restart cannot duplicate a submitted order` failing.
With the opt-in: **17/17**, the resubmit blocked by
`blocked:duplicate_submission_guard` with `create_order_calls=0`.

## Follow-up findings — deliberately NOT fixed here

Raised by the independent review, out of this patch's authorized scope:

* **MEDIUM** — `kalshi_demo_execution_check.py` calls `client.create_order`
  directly, bypassing `OrderManager` and therefore both new guards *and* the
  pre-existing global gate; its `CANDIDATE_SERIES` searches `KXBTCD` first.
  Pre-existing, DEMO-only, gated behind `ENABLE_DEMO_INTEGRATION_TEST=true`,
  cannot touch LIVE.
* **MEDIUM** — `_env_b` still reads `ALLOW_FRESH_STATE` and
  `ALLOW_FALLBACK_CAPITAL` as TRUE on `""`, `"off"`, a typo, or `"disabled"`.
  The B1 defect class on two safety-relevant flags; `ALLOW_FRESH_STATE` in
  particular suppresses the persistence-continuity sentinel.
* **LOW** — `ticker_is_wellformed` matches the ASCII grammar against the
  case-folded form, so ten non-ASCII code points (`ß ı ſ ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ`) launder
  into it. No daily-quarantine bypass was demonstrated.
* **LOW** — NBSP-wrapped gate values read TRUE, because `str.strip()` removes
  U+00A0. Permissive direction only for a deliberate `true`.
* **LOW** — `shadow_predictions.json` is written by `JsonStore` but is not in
  the runtime-state ignore list.

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
