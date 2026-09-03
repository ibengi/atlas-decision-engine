# Independent security review — B1/B2 order gating

**Subject:** `ibengi/atlas-decision-engine`, branch `claude/railway-atlas-inspection-1gtkyf`, head `e0e2f83`
**Commits under review:** `151860a`, `5757c09`, `e0e2f83`
**Reviewer:** independent session with no prior history on these commits. Nothing in a commit message, code comment, docstring or `docs/order-gating-verification.md` was accepted as evidence; every claim below was reproduced.

**Method.** Static reading of the full diff, plus behavioural probes written for this review: a 130-case environment-value matrix run in scrubbed subprocesses; a 68-case money-path matrix calling `OrderManager.place_and_track` directly with a fresh state directory per case; a 56-case engine probe with tripwires on `fresh_book`, `risk`, `posmgr`, `orders`, `tlog` and `stats`; a 16-mutation battery against the full suite; and six collection orders. No orders were placed, no credentials read or printed, no Railway variable read or changed, no LIVE path touched, no PR-D activity.

**Environment note.** `pytest` was not importable against the repo's runtime dependencies as provisioned, so a clean venv was built from the repo's own `requirements.txt` + `requirements-dev.txt` (pytest 9.1.1, cryptography 50.0.1, requests 2.34.2). `pytest-randomly` is **not** installed — confirmed independently, which corroborates the erratum in `e0e2f83`. Egress to exchange hosts is blocked; no test needed it.

**Working-tree integrity.** Mutation testing modified `config.py`, `execution_engine.py`, `order_manager.py` and `tests/conftest.py`. Every mutation was reverted from a pre-taken backup. Two mutation runs were killed mid-flight by tool timeouts, leaving a mutation in `order_manager.py` and later in `config.py`; both were detected by checksum and restored immediately. **Final state verified: `git status` clean, `git diff e0e2f83` empty, all four SHA-256 checksums match the committed blobs.**

---

## 1. Chain and scope

`git rev-parse HEAD` → `e0e2f83087f8190db333bdd444233f2ccea17426`. Linear chain confirmed: `c815818` → `151860a` → `5757c09` → `e0e2f83`.

`origin/main` is `44134db`. Its **tree is byte-identical to `c815818`** (`git diff c815818 origin/main` is empty — `44134db` is a merge whose content was already an ancestor), so the diff against current main is exactly these three commits.

Seven files changed, +1129/−3:

| File | + | − |
| --- | --- | --- |
| `.gitignore` | 17 | 0 |
| `config.py` | 139 | 1 |
| `docs/order-gating-verification.md` | 115 | 0 |
| `execution_engine.py` | 84 | 1 |
| `order_manager.py` | 30 | 1 |
| `tests/conftest.py` | 47 | 0 |
| `tests/test_daily_quarantine.py` | 700 | 0 |

The **complete** set of deleted lines across all three source files is three lines:

```
-    ALLOW_ORDER_SUBMISSION = _env_b("ALLOW_ORDER_SUBMISSION", default=True)
-from config import CFG, _env_b, _p, contract_cap_config
-from config import CFG, _p, contract_cap_config
```

The two import lines are replaced by widened imports. Everything else is pure addition.

Confirmed **not** touched: probability/model calculation (`btc_probability_model.py`, `ml_model.py`, `calibration.py`, `model_calibration.py` — not in the diff); thresholds and edge/EV gates; `PositionSizer` and all sizing constants; `RiskManager` limits (`MAX_CATEGORY_RISK_PCT`, `MAX_SINGLE_MARKET_RISK_PCT`, `MAX_TRADES_CYCLE`, Kelly); credentials (no `KEY_ID`/`PRIVATE_KEY`/`SECRET`/`TOKEN` line added or removed); Railway configuration; LIVE gates (`model_gatekeeper.py` untouched); `MODEL_APPROVED` — the string appears **zero** times in the diff, and `model_validation.json` still carries `approved: false`.

`.gitignore` additions are twelve runtime-state filename globs and a comment block — all runtime-state artifacts, no source, config or evidence file. Three of the twelve (`positions_state.json*`, `seen_fill_ids.json*`, `reconciliation_report.json*`) duplicate entries already present a few lines above; harmless.

**HEAD_MATCH=YES**
**SCOPE_CLEAN=YES**
**UNRELATED_CHANGES=NONE**

---

## 2. B1 — fail-closed config gate

`_env_gate` (config.py:33) reads `os.getenv`; `None` → the caller's `default`; otherwise `strip().lower()` matched against `GATE_TRUE_WORDS = ("true","1","yes","y","on")` then `GATE_FALSE_WORDS = ("false","0","no","n","off","non")`; anything else appends to `GATE_PARSE_WARNINGS`, logs `[CONFIG_GATE_INVALID]` at ERROR, and returns `False`.

Verified behaviourally, not by reading: 65 values × 2 variables = **130 subprocess runs**, each with both gate variables removed from the child's environment before setting the one under test. **0 deviations.**

- unset → False; `""` → False; `" "`, `"   \t  "`, `"\n"`, `"\t\n\r "` → False; every one of `enabled`, `disabled`, `fasle`, `ture`, `null`, `none`, `None`, `nil`, `2`, `-1`, `1.0`, `00`, `01`, `t`, `f`, `true false`, `false;true`, `TRUE!`, `truthy`, `yesss`, `oui`, `si`, `[true]`, `"true"`, `'true'`, `+1`, `ALLOW`, `allow`, `undefined` → **False, with the warning raised**.
- `" true "`, `"\ttrue\n"`, `TRUE`, `TrUe`, `"1 "`, `" 1"`, `ON` → True, no warning. `" false "`, `FALSE`, `OFF`, `non` → False, no warning.
- Zero-width space adjacent to `true` (`"​true"`, `"true​"`) → **False + warning** (`strip()` does not remove U+200B). Fullwidth `ｔｒｕｅ` → False + warning. Cyrillic-е `yеs` → False + warning. `TRUEİ` → False + warning.
- Every case also asserted that the *other* gate stayed `False` and that both values are real `bool`.

**No fall-through path exists.** `_env_gate` is called at exactly two sites, both in `config.py` (lines 258 and 263). `ALLOW_ORDER_SUBMISSION` has no second assignment anywhere; `grep` finds no `os.getenv("ALLOW_ORDER_SUBMISSION")` outside `config.py` and no `_env_b(` reading either gate. No production module assigns `CFG.ALLOW_ORDER_SUBMISSION` or `CFG.DAILY_RESEARCH_ORACLE_APPROVED` at runtime (only tests do). The old `default=True` is gone; deleting the Railway variable now closes the gate rather than opening it.

One observation, LOW: `"\xa0true\xa0"` (non-breaking spaces) reads as **True**, because Python's `str.strip()` removes U+00A0. This is the permissive direction, but only for a value an operator deliberately wrote as `true`; junk still fails closed. Noted, not a defect.

Because `_env_gate` is used only for these two policy booleans, `GATE_PARSE_WARNINGS` — which `log_execution_banner` re-prints raw at start-up — can never contain a credential.

**B1_FAIL_CLOSED=YES**
**OLD_FAIL_OPEN_PATH=REMOVED** (the single `_env_b("ALLOW_ORDER_SUBMISSION", default=True)` line is deleted; no replacement reintroduces it)
**CONFIG_BYPASS=NONE FOUND**

---

## 3. Ticker canonicalization

`canonical_ticker` returns `""` for `None`, decodes `bytes`/`bytearray` with `errors="replace"`, coerces other objects with `str()`, then `.strip().upper()`. `ticker_is_wellformed` requires `isinstance(ticker, str)` and matches `^[A-Z0-9][A-Z0-9._-]*$` against the canonical form. `is_daily_ticker` is `canonical.startswith("KXBTCD")`. `daily_quarantine_blocks` = `is_daily_ticker and not daily_oracle_approved()`.

**Normalize safely — all confirmed daily and quarantined:** `KXBTCD-…`, `kxbtcd-…`, `" KXBTCD-…"`, `"\tKXBTCD-…"`, `"\nKXBTCD-…"`, `"  kxbtcd-…  "`, trailing newline, CRLF-wrapped, NBSP-wrapped, mixed case, vertical tab, form feed.

**Fail closed — all refused with `blocked:ticker_malformed`, zero broker calls:** `None`; `""`; `"   "`; `"\t\n "`; `b"KXBTCD-X"`; `bytearray(b"KXBTCD-X")`; `42`; `["KXBTCD-X"]`; `{"ticker": …}`; a hostile object whose `__str__`/`upper`/`strip` all return a benign-looking 15m ticker; U+200B leading and embedded; U+2060 leading; RTL override U+202E; embedded space / tab / newline; NUL; `/`; Cyrillic-К homoglyph; Kelvin-sign K; fullwidth.

**Embedded whitespace is never laundered.** `"KX BTCD-X"` stays `"KX BTCD-X"` and is refused — it does not become a valid ticker. Verified directly.

I independently checked the upstream classifier for a divergence. `market_classifier.series_root` does `str(ticker).split("-",1)[0].strip().upper()` — it strips *after* splitting. For every leading-whitespace form I could construct, the classifier and `canonical_ticker` agree on "daily", so the whitespace bypass described in `e0e2f83` is real and is closed. I could not construct a string the classifier calls daily that `is_daily_ticker` calls non-daily.

### Finding — LOW: `.upper()` launders 10 non-ASCII code points into the ticker alphabet

`ticker_is_wellformed` applies the ASCII grammar to the **case-folded** form. Python's `str.upper()` performs full Unicode case mapping, so exactly ten non-ASCII code points map entirely into `[A-Z0-9._-]`: `ß`→`SS`, `ı`→`I`, `ſ`→`S`, `ﬀ ﬁ ﬂ ﬃ ﬄ`→`FF FI FL FFI FFL`, `ﬅ ﬆ`→`ST` (enumerated over the whole code space, not guessed).

Observed consequence: `"ſKXBTCD-X"` canonicalizes to `"SKXBTCD-X"`, passes `ticker_is_wellformed`, classifies as **not daily**, and with `ALLOW_ORDER_SUBMISSION=true` **reached `create_order`** in my probe — carrying the original non-ASCII string, since the broker call keeps the raw ticker. `"KXBTCDı-X"` → `"KXBTCDI-X"` is the same mechanism in the fail-closed direction (correctly quarantined).

Why I rate this LOW, not a gate bypass:
- It does **not** let a genuine daily market through. Prefixing any character — ASCII or not — makes a string non-daily; `"ZKXBTCD-X"` behaves identically. This is the prefix design (§8), not a Unicode hole.
- The engine's `market_type` allowlist blocks such a ticker upstream: `market_classifier` cannot map it to any executable type.
- Kalshi tickers are ASCII; the exchange would reject the string.

What it *is*: the `ticker_is_wellformed` docstring's claim that the function is False for anything carrying a character outside the Kalshi grammar is inaccurate for these ten. Matching the regex against the **stripped raw** string rather than the case-folded one would close it.

**CANONICALIZATION_SAFE=YES**
**MALFORMED_TICKER_FAIL_CLOSED=YES** (every value tested refused before `create_order`)
**UNICODE_EDGE_CASES=ONE LOW FINDING** — 10 code points launder through `ticker_is_wellformed`; no daily-quarantine bypass demonstrated
**NORMALIZATION_BYPASSES=NONE FOUND** for the daily quarantine

---

## 4. Engine quarantine

`_execute_decision` (execution_engine.py:778) runs, before anything else: (a) `ticker_is_wellformed` → `return 0`; (b) `if daily_quarantine_blocks(ticker) or mtype == DAILY_MARKET_TYPE:` → if `not daily_oracle_approved()` → `return 0`; (c) `if mtype not in executable_market_types():` → `return 0`. Only then `fresh_book`.

I drove this with `ExecutionEngine.__new__` and **tripwires** on `fresh_book`, `risk`, `posmgr`, `orders`, `tlog`, `stats` — any contact raises. 28 cases × 2 approval states = **56 runs, 0 invariant failures.**

With the daily gate closed, every one of these returned 0 with **no tripwire fired at all**: daily ticker with `market_type` = `btc_above_strike_daily`, `None`, `""`, `"unknown"`, a future alias `btc_daily_v2`, the *wrong* type `btc_15m_above_strike`, `sports_moneyline`, whitespace-padded `" btc_above_strike_daily "`, uppercase `BTC_ABOVE_STRIKE_DAILY`; lowercase daily ticker; space-, tab- and newline-led daily tickers; and — the reverse direction — a **15m ticker and a sports ticker carrying `market_type="btc_above_strike_daily"`**. Rejection counters recorded `daily_oracle_unapproved`. Malformed tickers recorded `ticker_malformed`. Non-daily tickers with `market_type` `None` / `btc_other` / `eth_above_strike_daily` recorded `market_type_not_executable`.

The allowlist is genuinely an allowlist: `Decision.market_type` defaults to `None` (confirmed in `strategy_router.py:91`) and `None not in executable_market_types()`, so a hand-built `Decision` fails closed. `executable_market_types()` returns exactly the five non-daily registered types and adds `btc_above_strike_daily` **only** when `daily_oracle_approved()` — one switch, verified by calling it in both states. The five listed types are exactly the registered strategies minus daily, so nothing else is silently de-listed.

`return 0` does not consume the cycle budget: the caller is `_finish_cycle`'s `placed += self._execute_decision(...)` guarded by `if placed >= CFG.MAX_TRADES_CYCLE`, so a refused daily candidate cannot starve a 15m candidate.

**15m unchanged:** `KXBTC15M-… / btc_15m_above_strike`, sports and election controls all reached `fresh_book` normally, in both approval states, and behaved identically. The three controls were asserted to *not* be blocked, so the probe cannot pass by blocking everything.

**ENGINE_GATE_SAFE=YES**
**MARKET_TYPE_FAIL_CLOSED=YES**
**15M_UNCHANGED=YES**

---

## 5. Money-path secondary guard

`place_and_track` order of gates: persistence sentinel → resolution halt → contract cap → price/side/count invariants → `assert_real_demo_integrity` → `ALLOW_ORDER_SUBMISSION` → `ticker_is_wellformed` → `daily_quarantine_blocks` → 503 cooldown → pending intent → dedup → … → `create_order` (order_manager.py:770). **No state is written before the daily guard** — I read every statement above it; all are reads.

68 cases (17 tickers × 4 gate combinations), **fresh `DATA_DIR` per case**, real `OrderManager`, `MagicMock` client. **0 invariant failures.**

With `ALLOW_ORDER_SUBMISSION=true` and `daily_oracle_approved()=false`, all ten KXBTCD variants (plain, lowercase, leading space/tab/newline, both-blanks, NBSP, bytes, ZWSP, embedded space) returned `blocked:daily_oracle_unapproved` or `blocked:ticker_malformed` with:

- `create_order.call_count == 0`
- `session_submitted`, `pending_intents`, `open_orders` deep-equal to the pre-call snapshot
- the `DATA_DIR` file listing unchanged — **no submission-guard, pending-intent, risk or capital file created or modified**
- no submission counter incremented (the engine returns before `report["orders_submitted"]`, proved by the §4 tripwires)

Negative control: `KXBTC15M-…`, `KXNFLGAME-…` and `KXBTC-D-…` each produced exactly one `create_order` call in the same configuration, so the matrix is not passing by blocking everything.

### Other entry points

- **`execution_engine.py:922`** — the only production caller. Covered by both guards.
- **`tools/restart_harness.py`** (3 call sites) — uses `KXTEST-CANARY-T1`, not daily, with a `MagicMock` client. No daily bypass. But see the MEDIUM finding below.
- **`kalshi_demo_execution_check.py:109`** — calls `client.create_order(...)` **directly**, bypassing `OrderManager` and therefore **both** new guards *and* the pre-existing `ALLOW_ORDER_SUBMISSION` gate. Its `CANDIDATE_SERIES = ("KXBTCD", "KXBTC15M", "KXETHD")` searches **KXBTCD first**, so it preferentially targets the quarantined daily series. This is **pre-existing and outside this diff**, and it is not unguarded: it requires `ENABLE_DEMO_INTEGRATION_TEST=true`, refuses any non-`demo-api.kalshi.co` URL, refuses a LIVE context, refuses a non-genuine client, and caps at 1 contract ≤ 30¢ of DEMO funds. It nonetheless means the statement "no KXBTCD order can reach the broker from this process tree" is **not** true repository-wide. Rated MEDIUM: DEMO-only, explicit opt-in, cannot touch LIVE, not introduced here — but the B2 quarantine should arguably be applied there too, or the series list reordered.
- No other `create_order` or `place_and_track` call site exists outside `tests/`.

**SECONDARY_GUARD_SAFE=YES**
**DIRECT_BYPASS=NONE** (direct `place_and_track` calls are fully covered, including with no `market_type` at all)
**TOOL_BYPASS=ONE (MEDIUM, pre-existing)** — `kalshi_demo_execution_check.py` bypasses `OrderManager` entirely
**BROKER_WRITES=ZERO** in every blocked case across all 68 runs

---

## 6. Guard independence

Full matrix, measured (`create_order` call count):

| `ALLOW_ORDER_SUBMISSION` | daily approved | daily ticker | 15m / sports ticker |
| --- | --- | --- | --- |
| false | false | **blocked**, 0 calls | **blocked**, 0 calls |
| false | true | **blocked**, 0 calls | **blocked**, 0 calls |
| true | false | **blocked** (`daily_oracle_unapproved`), 0 calls | proceeds → 1 call |
| true | true | proceeds → 1 call | proceeds → 1 call |

Neither guard can override the other. The global gate is read from `CFG.ALLOW_ORDER_SUBMISSION`; the daily guard from `daily_oracle_approved()` → `CFG.DAILY_RESEARCH_ORACLE_APPROVED`. Separate `_env_gate` calls, separate variables; the 130-case matrix in §2 asserted on every run that setting one leaves the other `False`. Approving the daily oracle does **not** open the global gate (row 2 blocks everything), and opening the global gate does **not** approve daily (row 3). In `true/true` the daily ticker still faces every pre-existing gate — my probe observed `blocked:duplicate_submission_guard` on a repeat, i.e. the ordinary machinery, not a new bypass.

Guard order is deliberate and confirmed: the global gate reports first, so a KXBTCD ticker under `ALLOW_ORDER_SUBMISSION=false` yields `blocked:submission_disabled`, preserving the existing state5 restore assertion.

**GUARD_MATRIX=ALL FOUR CELLS AS SPECIFIED**
**GUARDS_INDEPENDENT=YES**

---

## 7. Test infrastructure — high attention

`tests/conftest.py` sets `ALLOW_ORDER_SUBMISSION=true` and `DAILY_RESEARCH_ORACLE_APPROVED=true` via `os.environ.setdefault` at collection time, before `config` is imported. In-process, therefore, **both production gates are open for the entire pytest suite**. I measured the dependency: neutering just those two lines produces **135 failures** (the commit's "~130" is accurate).

**Is a production-default regression masked? No — proven, not assumed.**

`CleanEnvironmentDefaults._probe` builds `env` by *removing* both gate vars from `os.environ` and passes that as the child's complete environment to `subprocess.run([sys.executable, "-c", _PROBE], env=env, cwd=root)`. `python -c` never imports `conftest.py` (that is a pytest collection mechanism), so the child observes the shipped defaults. `test_the_probe_sees_a_scrubbed_environment` additionally asserts the parent *does* carry both vars, so if conftest stopped setting them the guard itself fails rather than the assertions passing vacuously. I read this code rather than trusting its docstring, and confirmed the isolation is real.

I then confirmed empirically **which** tests kill the default flips:

| Mutation | Killed by |
| --- | --- |
| global default `False`→`True` | `StrictGateParser::test_order_gates_are_parsed_strictly_and_default_false` (source-text) **plus** `CleanEnvironmentDefaults::test_absent_variables_ship_closed`, `::test_the_two_gates_are_read_independently`, `ImportOrderIsIrrelevant::test_config_state_is_identical_whatever_is_imported_first` (all behavioural subprocess) |
| daily default `False`→`True` | same four |
| malformed env read as TRUE | `StrictGateParser::test_empty_and_whitespace_are_false`, `::test_malformed_values_are_false_and_warn`, `CleanEnvironmentDefaults::test_blank_and_whitespace_are_false`, `::test_malformed_values_are_false_and_reported` |

The source-text assertion is honestly labelled as such in its own docstring, and **behavioural subprocess tests kill the same mutations independently** — so the claim in `docs/order-gating-verification.md` that the four source-text-catchable mutations are separately caught behaviourally checks out.

### Mutation battery — 16 mutations, all killed

Each applied alone to a backed-up file, full suite run, file restored, `git status` asserted clean between mutations.

| # | Mutation | Result |
| --- | --- | --- |
| M1 | `ALLOW_ORDER_SUBMISSION` default `False`→`True` | **KILLED** (4 failed) |
| M2 | `DAILY_RESEARCH_ORACLE_APPROVED` default `False`→`True` | **KILLED** (4 failed) |
| M3 | engine daily quarantine block deleted | **KILLED** (3 failed) |
| M4 | money-path daily guard deleted | **KILLED** (4 failed) |
| M5 | ticker normalization removed (`strip`+`upper`) | **KILLED** (8 failed) |
| M5a | `.strip()` removed | **KILLED** (7 failed) |
| M5b | `.upper()` removed | **KILLED** (7 failed) |
| M6 | malformed env value read as TRUE | **KILLED** (4 failed) |
| M7 | engine malformed-ticker guard deleted | **KILLED** (2 failed) |
| M8 | money-path malformed-ticker guard deleted | **KILLED** (2 failed) |
| M9 | allowlist turned into a denylist | **KILLED** (1 failed) |
| M10 | daily admitted to the allowlist unconditionally | **KILLED** (2 failed) |
| M11 | `daily_oracle_approved()` forced open | **KILLED** (16 failed) |
| M12 | `ticker_is_wellformed` accepts non-`str` | **KILLED** (4 failed) |
| M13 | daily prefix test loosened to equality | **KILLED** (11 failed) |
| M14 | conftest scrub-guard: gates no longer set | **KILLED** (11 failed) |

All six mutations the review specifically required (global default, daily default, engine guard removal, money-path guard removal, ticker normalization removal, malformed-env-read-as-true) are among these and all fail the suite.

### Collection order

`pytest-randomly` is **not installed** (verified by import probe in both the uv toolchain and the purpose-built venv), so `-p no:randomly` would be silently accepted and change nothing — the erratum in `e0e2f83` is correct. I shuffled the **file arguments**, which genuinely changes pytest's collection order.

| Run | Result |
| --- | --- |
| Ordinary full run #1 | 795 passed, 142 subtests |
| Ordinary full run #2 | 795 passed, 142 subtests |
| Shuffle seed 1 | 795 passed |
| Shuffle seed 7 | 795 passed |
| Shuffle seed 31337 | 795 passed |
| Adversarial: the modules that do not `import _bootstrap` first, then the rest, quarantine module **last** | 795 passed |
| Adversarial: quarantine module **first**, then non-bootstrap, then rest | 795 passed |
| Adversarial: strict reverse-alphabetical | 795 passed |
| Quarantine module alone | 50 passed |
| A single non-bootstrap order-path module alone | 16 passed |

(I count **22 of 53** modules that do not `import _bootstrap` in their first 25 lines; the docs say "ten" and `151860a` says "11 of 51". The discrepancy is in the counting, not in the outcome — order independence holds regardless. Minor.)

### Finding — HIGH: this branch breaks the Docker build

This is the masking that matters, and it runs the opposite way from the one the review anticipated. `conftest.py` is a **pytest-only** mechanism. The repository's primary automated runner is not pytest:

- `run_tests.py` uses `unittest.TestLoader().discover("tests")` and writes `test_report.json`, which `model_gatekeeper.check_live_allowed()` reads as the last lock before real money.
- `Dockerfile` stage `tests` contains `RUN python run_tests.py`, and its own comment states the intent: *"une suite rouge casse le build, donc une image dont les tests echouent n'existe jamais."*

Measured, same machine, same venv, gate variables absent:

| Tree | `python run_tests.py` |
| --- | --- |
| `c815818` (baseline) | **Ran 740 tests — OK**, exit 0 |
| `e0e2f83` (this branch) | **Ran 790 tests — FAILED (failures=88, errors=47)**, exit **1** |
| `e0e2f83` with `ALLOW_ORDER_SUBMISSION=true DAILY_RESEARCH_ORACLE_APPROVED=true` | **Ran 790 tests — OK**, exit 0 |

The third row isolates the cause completely: the breakage is entirely the two gate variables that only `conftest.py` supplies. Consequences:

1. **`RUN python run_tests.py` fails, so no image can be built from this commit.** The branch is not deployable as it stands.
2. `test_report.json` cannot be regenerated for the deployed commit, so the live gatekeeper has no fresh evidence artifact.
3. The invocation documented in `requirements-dev.txt`, in `pytest.ini`'s own header, and in `README.md` (`python -m unittest discover -s tests`) is broken.

This is **fail-closed** — a failed build ships nothing, and a missing or red report makes the gatekeeper refuse LIVE — so it is not a security hole. It is a **merge blocker**. The fix is small: set the same defaults in a place both runners honour (e.g. `tests/_bootstrap.py`, or a `sitecustomize`/`run_tests.py` preamble) rather than in `conftest.py` alone.

**CONFTEST_MASKING=NO for production defaults** (subprocess tests are genuinely isolated and independently kill every default mutation) — **BUT YES for runner compatibility**: conftest hides that the suite now requires an environment the Dockerfile's runner does not provide
**DEFAULT_TEST_ISOLATED=YES** (verified by reading the isolation code and by mutation)
**IMPORT_ORDER_SAFE=YES** (10 orders, all green)
**FAIL_OPEN_MUTATIONS_CAUGHT=16/16**

---

## 8. Residual prefix risk

Question asked: is there a **current** path where an unknown daily ticker naming *and* a missing/unknown `market_type` together reach `place_and_track`?

**No.** Traced exhaustively:

- The only production caller of `place_and_track` is `_execute_decision`, which requires `mtype in executable_market_types()`. A **missing or unknown** `market_type` is refused by the allowlist before the call. So the two conditions are mutually exclusive on the current path — that is precisely what makes the allowlist the primary protection.
- To reach `place_and_track` at all you need a *known executable* `market_type`. So the dangerous shape is a renamed daily series that `market_classifier` maps to an executable type while `config.DAILY_TICKER_PREFIX` is not updated. Today it does not exist: `market_classifier._PREFIX_RULES` maps `KXBTCD` → `btc_above_strike_daily` (not in the allowlist) and `KXBTC` → `btc_other` (also not in the allowlist).
- I checked the two named evolutions concretely. `KXBTCDAILY-…` → `series_root` `KXBTCDAILY`, still `startswith("KXBTCD")` → still daily → blocked by **both** guards. `KXBTC-D-…` → `series_root` `KXBTC` → `btc_other` → **blocked by the engine allowlist**; the money-path guard alone does *not* catch it (I confirmed empirically that `KXBTC-D-26SEP-T1` reaches `create_order` through `place_and_track` with the global gate open).

So the residual is exactly as the author documents: the money-path guard's prefix match is a naming bet, and the allowlist is what actually holds. It becomes live only if someone adds a renamed daily series to `market_classifier` *and* to `EXECUTABLE_MARKET_TYPES` without updating `DAILY_TICKER_PREFIX` — a future-change risk requiring two deliberate edits, not a current bypass. A guard keyed on the resolved `market_type` in `place_and_track` as well as the ticker would remove the bet; that is a hardening suggestion, not a blocker.

**PREFIX_RESIDUAL_SEVERITY=LOW — naming-schema risk only**
**CURRENT_BYPASS_FROM_PREFIX_EVOLUTION=NONE FOUND**

---

## 9. Approval boolean risk

- **Shipped/default value is false**: proved behaviourally in the 130-case subprocess matrix — absent, blank, whitespace and every malformed value all resolve `False`. Not read from source.
- **No permissive env default exists**: `DAILY_RESEARCH_ORACLE_APPROVED = _env_gate(..., default=False)` is the only assignment; `_env_gate` never enables on an unrecognised value; `_env_b` is not used for it.
- **Single resolver**: `daily_oracle_approved()` is the sole reader of the flag, and both guards plus `executable_market_types()` plus the start-up banner call it. Forcing it open (M11) fails 16 tests.
- **Nothing interprets a missing artifact as approved**: `daily_oracle_approved()` returns `bool(CFG.…)`, which is `False` when unset. There is no file, cache or artifact whose absence is read as approval. `model_validation.json` independently carries `approved: false`.
- **Future replacement is unobstructed**: the resolver is a function with one call path, so substituting a hash-pinned derived artifact is a single-function change. The docstring names the intended criteria.

The temporary boolean is acceptable **only** under the current hold: `ALLOW_ORDER_SUBMISSION` closed *and* daily quarantined by both guards. It is not acceptable as the thing that authorizes daily execution — flipping one environment variable would today admit `btc_above_strike_daily` to the allowlist and open the money-path guard, with no validation evidence required. Nothing in this branch does that, and nothing should until the derived artifact exists.

Scope caveat, stated plainly: **I did not read the Railway environment**, so I cannot independently confirm the deployed value of `ALLOW_ORDER_SUBMISSION`. The review was constrained to static + test-based work, and reading Railway variables risks exposing credentials. The claim that it is `"false"` in production appears in the commit messages and the docs; I treat it as **unverified**.

**TEMP_BOOLEAN_SAFE_FOR_HOLD=YES** (only while both conditions above hold)
**DERIVED_ARTIFACT_STILL_REQUIRED=YES** (mandatory before any daily execution)

---

## 10. Runtime-state hygiene

Twelve globs added in `5757c09`, all runtime-state filenames written by `persistence.JsonStore`. Verified:

- **No newly-ignored path was ever tracked.** Checked at `c815818` and at `e0e2f83` (`git ls-tree -r`, exact-name match: 0 hits each) and across the entire history (`git log --all -- <name>`: 0 commits touching any of the twelve). Nothing was accidentally hidden.
- **No state artifact was committed.** The tracked-file list changes by exactly three additions between `c815818` and `e0e2f83`: `docs/order-gating-verification.md`, `tests/conftest.py`, `tests/test_daily_quarantine.py`.
- **Tests do not depend on ignored state surviving between runs.** Two consecutive full-suite runs both produced 795 passes; the shuffled and adversarial orders and the single-module runs all pass from a cold start.
- The additions are limited to runtime-state artifacts — no source, config, evidence or documentation file is newly ignored. `test_report.json` remains ignored deliberately and is unaffected.

Two observations, both minor and neither a defect:

- I **could not reproduce** the symptom stated in the commit message. Running the full suite twice from the repo root left `git status` clean with none of `pending_intents.json`, `risk_state.json`, `submission_guard.json` etc. present on disk. The patterns are therefore correct and defensive, but the "clean checkout goes dirty" claim is not reproducible in this environment — likely `DATA_DIR`-dependent. This does not argue against the change.
- `shadow_predictions.json`, also written via `_p(...)`/`JsonStore`, is **not** covered by the new list (`git check-ignore` reports NOT IGNORED). A small completeness gap in an otherwise correct change.

**RUNTIME_STATE_HYGIENE=CLEAN**

---

## Additional findings outside the ten sections

### MEDIUM — `tools/restart_harness.py` silently stops proving what it claims

The harness imports `tests/_bootstrap`, which does **not** set `ALLOW_ORDER_SUBMISSION`. With B1's new fail-closed default it now short-circuits at the global gate. Measured, clean environment:

| Tree | Result |
| --- | --- |
| `c815818` | **16/16 checks passed** |
| `e0e2f83` | **15/16** — `FAILED: [VOLUME] restart cannot duplicate a submitted order (resubmit -> status=blocked:submission_disabled create_order_calls=0)` |

The duplicate-submission invariant — the defence against the 2026-07-25 eight-fill incident — is no longer exercised by its own harness; it is masked by an earlier gate. Fail-closed in direction, but a safety-verification tool that no longer verifies is a real loss. The suite's `test_restart_harness_ticker_is_not_a_daily_ticker` shows the author considered this harness for B2 but not for B1. One line (`bot.CFG.ALLOW_ORDER_SUBMISSION = True`, as `conftest.py` does) restores it.

### MEDIUM (pre-existing, out of diff scope) — the same fail-open parser still guards two safety flags

`151860a` deliberately limited its blast radius to the two order gates, leaving `_env_b` for "non-safety preferences". Two of the flags still read by `_env_b` are safety-relevant and fail **open**:

| Variable | `""` | `"off"` | `"fasle"` | `"disabled"` |
| --- | --- | --- | --- | --- |
| `ALLOW_FRESH_STATE` | True | True | True | True |
| `ALLOW_FALLBACK_CAPITAL` | True | True | True | True |

`ALLOW_FRESH_STATE` is the "explicit one-time operator acknowledgement" that suppresses the persistence-continuity sentinel; a typo or a blanked variable enables it. `ALLOW_FALLBACK_CAPITAL` permits a fallback capital figure. Both are exactly the B1 defect on a different variable. (`KILL_SWITCH`, `SHADOW_MODE`, `REQUIRE_PERSISTENT_STATE`, `DRY_RUN` also read True on garbage, but for those True is the safe direction.) Not introduced by this branch and explicitly out of its declared scope — flagged because the defect class is not closed, and `ALLOW_FRESH_STATE` in particular gates a LIVE-capable safety check.

### LOW — documentation and message accuracy

The erratum in `e0e2f83` is correct and I verified it independently (`pytest-randomly` absent). The remaining inaccuracies are small: the "ten modules that do not `import _bootstrap`" count (docs) vs "11 of 51" (`151860a`) vs the 22 of 53 I measure; and the non-reproducible dirty-tree claim in `5757c09`. Neither affects any security conclusion. `docs/order-gating-verification.md`'s mutation table is otherwise consistent with what I measured independently.

---

## FINAL VERDICT

**B1_SECURITY=PASS** — strict fail-closed parser, verified over 130 subprocess cases with zero deviations; no fall-through path; old permissive default removed
**B2_SECURITY=PASS** — engine allowlist and money-path guard both hold; 56 engine cases and 68 money-path cases with zero broker writes and zero state mutation when blocked
**CANONICALIZATION=PASS WITH ONE LOW FINDING** — whitespace and case handled correctly, inner whitespace never laundered, malformed values fail closed; 10 non-ASCII code points pass `ticker_is_wellformed` via `.upper()` case mapping, with no demonstrated daily bypass
**GUARD_INDEPENDENCE=PASS** — all four matrix cells as specified; neither guard can override or disable the other
**CONFTEST_SAFETY=PASS FOR PRODUCTION DEFAULTS / FAIL FOR RUNNER COMPATIBILITY** — subprocess isolation is genuine and independently kills every default mutation, but conftest hides that the suite now requires an environment `run_tests.py` (and therefore the Dockerfile) does not supply
**15M_UNCHANGED=YES** — 15m, sports and election paths behave identically before and after, in both approval states
**RUNTIME_STATE_HYGIENE=CLEAN** — nothing previously tracked was hidden, no state artifact committed, no test depends on ignored state

**BLOCKERS=1**
1. **The Docker build fails on this branch.** `RUN python run_tests.py` exits 1 (88 failures, 47 errors) because `run_tests.py` is unittest-based and never loads `tests/conftest.py`. Baseline `c815818` is green in the same environment; setting the two gate variables makes `e0e2f83` green. No image can be built, and `test_report.json` cannot be regenerated for the live gatekeeper. Fail-closed, but it blocks merge.

**HIGH_FINDINGS=0** (the blocker above is a build/pipeline breakage, not a security bypass; no security finding rose to HIGH)

**MEDIUM_FINDINGS=3**
1. `tools/restart_harness.py` degrades 16/16 → 15/16; the restart duplicate-submission invariant is no longer exercised, masked by the new global gate.
2. `kalshi_demo_execution_check.py` calls `create_order` directly, bypassing `OrderManager` and both new guards, and searches `KXBTCD` first. Pre-existing, DEMO-only, explicit opt-in, cannot touch LIVE.
3. `_env_b` still reads `ALLOW_FRESH_STATE` and `ALLOW_FALLBACK_CAPITAL` as TRUE on empty/typo/`"off"` — the B1 defect class survives on two safety-relevant flags. Pre-existing, out of the commit's declared blast radius.

**RESIDUAL_RISKS=5**
1. Prefix matching is a naming bet; the `market_type` allowlist is the real protection. No current bypass — two deliberate future edits would be needed to open one.
2. Daily approval is a manual boolean; a single environment variable would today admit daily execution with no validation evidence. The derived, hash-pinned artifact remains mandatory.
3. `ticker_is_wellformed` validates the case-folded form; 10 non-ASCII code points launder into the ticker alphabet and the raw string is forwarded to `create_order`. LOW.
4. NBSP-wrapped gate values read as TRUE (`str.strip()` removes U+00A0). LOW, permissive direction only for a deliberate `true`.
5. `shadow_predictions.json` is written by `JsonStore` but not covered by the new ignore list.

**Not proven.** I could not verify the deployed Railway value of `ALLOW_ORDER_SUBMISSION` — reading the environment was outside the sanctioned static + test-based scope and risks credential exposure. The claim that it is `"false"` in production is recorded as **unverified**, and every conclusion above that mentions the deployed hold is conditional on it.

Where I found nothing wrong, I say so plainly: the B1 parser, the B2 engine allowlist, the money-path guard, guard independence, the 15m path, and runtime-state hygiene each survived every adversarial probe I could construct, and all 16 mutations were killed by the suite.

**PR_MERGE_READY=NO** — the Docker build fails; fix the runner/conftest split first
**PR_DEPLOY_READY=NO**
**PR_D_STATUS=DO_NOT_DEPLOY**
**MODEL_APPROVED=false**
**READY_FOR_DEMO_CANARY=NO**
**READY_FOR_LIVE=NO**
