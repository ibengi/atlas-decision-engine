# ATLAS — Investor Demo Evidence Package

Generated 2026-09-04 from the actually deployed system. Every figure below was
read back from the running service, the build that produced it, or the source
bytes the image was built from. Nothing here is a projection.

**The system does not trade with real money today, and this document does not
argue that it should.** It argues that the machinery which *decides whether it
may* is built, deployed, and demonstrably refusing.

---

## 1. Release snapshot

| Field | Value | Where it came from |
|---|---|---|
| `MAIN_SHA` | `9b906e8d0a0a4e183291aa439855519c2d6e19e7` | `git rev-parse origin/main` |
| `DEPLOYMENT_ID` | `2a3da86f-d925-4c4e-9eba-fd48d41ba9c9` | Railway deployment record |
| Deployment created | 2026-09-04 16:24:39 UTC | Railway |
| Deployment live | 2026-09-04 16:26:24 UTC | Railway |
| Deployment status | `SUCCESS` | Railway |
| Environment | `DEMO` | boot banner: `Environnement : DEMO -> https://demo-api.kalshi.co/trade-api/v2` |
| Test count | **794** | Docker `tests` stage, inside the Railway build |
| Test failures | **0** | same |
| Test errors | **0** | same |
| Model approval | **`false`** | `model_validation.json` → `approved: false` |
| Order submission | **`false`** | boot banner: `order_submission_enabled=false` |
| Daily oracle approval | **`false`** | `DAILY_RESEARCH_ORACLE_APPROVED` unset → fail-closed default |
| Reconciliation | `MATCH` | `[RECONCILE_STARTUP]` and `[RECONCILE_VERIFY]`, broker=0 local=0 |
| Persistence | `HEALTHY` | volume mounted, `DATA_DIR=/data/state5`, no `[PERSISTENCE_HALT]` |
| Broker writes | **0** | 22 consecutive cycles, 16:26:21–16:47:35 UTC |
| Fills | **0** | same |
| Cancels | **0** | same |

The zero counters describe **this release's observation window**, not all time.
The local journal carries 7 historical settled DEMO trades from earlier
releases; they are DEMO funds and are reconciled.

---

## 2. Safety proof

Each proof below was executed against the deployed commit `9b906e8`. All seven
files touched by this release are byte-identical between the independently
reviewed tree and the deployed tree — in fact the merge changed zero bytes
(`44134db` and `c815818` share tree `aaaa8fca…`), so "reviewed" and "deployed"
are the same source.

### 2.1 The order gate fails closed on malformed or absent input

A configuration flag that is misspelled, empty, or missing must never be read as
permission. Executed against the deployed source:

```
'true'      -> True        'false'    -> False
'TRUE'      -> True        '0'        -> False
' yes '     -> True        ''         -> False   [CONFIG_GATE_INVALID] … lu comme FALSE (fail-closed)
'1'         -> True        'maybe'    -> False   [CONFIG_GATE_INVALID] …
'on'        -> True        'tru'      -> False   [CONFIG_GATE_INVALID] …
                           '2'        -> False   [CONFIG_GATE_INVALID] …
                           'yes;drop' -> False   [CONFIG_GATE_INVALID] …
                           (unset)    -> False
```

A typo does not open the gate; it closes it *and says so*. In a clean
environment with no gate variables set at all — which is exactly how the
production service is configured — both flags read `false`:

```
clean_env_ALLOW_ORDER_SUBMISSION          : false
clean_env_DAILY_RESEARCH_ORACLE_APPROVED  : false
daily_oracle_approved()                   : false
```

### 2.2 Daily (KXBTCD) execution is quarantined

Kalshi's daily BTC markets settle against an index the system cannot yet verify
independently, so it refuses to trade them at all. The proof below **forces the
global submission gate open**, so any refusal is attributable to the daily
quarantine alone and not to the global gate answering on its behalf. Two
separate processes, because the flag is frozen at import:

```
### Control: oracle FORCED APPROVED (not the production state) ###
--- oracle_approved=True   ALLOW_ORDER_SUBMISSION=True ---
  'KXBTCD-26SEP0517-T90749.99'   daily=True   QUARANTINED=False
  'kxbtcd-26sep0517'             daily=True   QUARANTINED=False
  '  KXBTCD-X  '                 daily=True   QUARANTINED=False
  'KXBTC15M-26SEP041230-30'      daily=False  QUARANTINED=False

### Production state: DAILY_RESEARCH_ORACLE_APPROVED unset ###
--- oracle_approved=False  ALLOW_ORDER_SUBMISSION=True ---
  'KXBTCD-26SEP0517-T90749.99'   daily=True   QUARANTINED=True
  'kxbtcd-26sep0517'             daily=True   QUARANTINED=True
  '  KXBTCD-X  '                 daily=True   QUARANTINED=True
  'KXBTC15M-26SEP041230-30'      daily=False  QUARANTINED=False
```

The control matters: it shows the quarantine tracks the oracle flag rather than
blocking everything unconditionally, and that the 15-minute market is
unaffected. Case and surrounding whitespace do not evade it.

A ticker that cannot be classified is refused outright rather than guessed at —
the system never edits an input until it becomes valid:

```
None            wellformed=False      '​KXBTCD-X'  wellformed=False
''              wellformed=False      'KX BTCD-X'       wellformed=False
'   '           wellformed=False      b'KXBTCD-X'       wellformed=False
                                      12345             wellformed=False
```

Both guards call one shared function, so the engine-level allowlist and the
money-path guard cannot drift apart.

### 2.3 Global order submission is disabled

The running container reports `order_submission_enabled=false` at boot. Across
22 observed cycles the engine submitted nothing (`orders_submitted: 0`,
`risk_passed: 0`). On the previous release, when candidates did reach the money
path, every one was refused:

```
[ORDER_SUBMIT_ATTEMPT] bloque: ALLOW_ORDER_SUBMISSION=false
NON EXECUTE (rejected: blocked:submission_disabled) -- AUCUN trade enregistre.
```

### 2.4 The model gate refuses LIVE

The last lock before real money was asked for permission with the promotion
variables set to their **most permissive** values. It still said no — both from
inside the built image and from the deployed source:

```
check_live_allowed() -> False
refusal reasons     -> ['model_validation.json absent ou non approuve']
```

### 2.5 Reconciliation matches broker and local state

```
[RECONCILE_STARTUP] MATCH (tickers broker=0 local=0)   16:26:21Z
[RECONCILE_VERIFY]  MATCH (tickers broker=0 local=0)   16:42:19Z
```

The broker reports no open positions and the local ledger agrees. A mismatch
halts rather than being reconciled away.

### 2.6 Persistence is healthy

The volume mounts, state lives on it (`DATA_DIR=/data/state5`), and state
survived this redeploy: reconciliation matched at startup against the restored
ledger. No `[PERSISTENCE_HALT]` appeared in any observed cycle. A critical write
that cannot be persisted halts the engine rather than continuing with state it
cannot prove.

### 2.7 The build gate is not decorative

The image cannot be produced from a red test suite. A dedicated CI job
(`a-red-suite-blocks-the-build`) deliberately breaks the suite and confirms the
build fails. The `test_report.json` that the LIVE gatekeeper reads is generated
by the tests that actually ran during that build — it describes the deployed
commit, not a developer's laptop.

---

## 3. Independent review summary

The engineer who wrote this change did not review it. Two separate reviewer
sessions, each with no authoring history on the commits under review, were
given the branch as unfamiliar code and told not to trust commit messages,
comments, or documentation as evidence.

| | Security review | Delta review |
|---|---|---|
| Scope | `e0e2f83` | `e0e2f83..5d6e994` |
| Branch | `claude/independent-security-review-e0e2f83` | `claude/independent-delta-review-5d6e994` |
| Commit | `568fd98` | `9c5ffbe` |
| Document | `docs/reviews/independent-security-review-e0e2f83.md` | `docs/reviews/independent-delta-review-5d6e994.md` |
| Blockers | **1** (resolved by `5d6e994`) | **0** |
| High findings | 0 | **0** |
| Medium / Low | — | 1 / 3 |
| Verdict | blocker must be fixed | `PR_MERGE_READY=YES` (delta only), `PR_DEPLOY_READY=NO` |

**The first review found a real blocker, and that is the point.** The author had
reported the suite green using one test runner; the Docker build uses a
different one, and under that runner the suite did not pass. The image gate
would have failed. The reviewer reproduced it independently, the defect was
fixed in `5d6e994`, and the second reviewer re-derived the original failure from
scratch before confirming the fix.

**The Docker limitation, and how it was closed.** Both reviewers recorded
`DOCKER_TEST_STAGE_PASS=NOT_RUN`: the review sandbox could not reach the Docker
CDN, so neither could execute the real image build. They said so rather than
inferring it. That gap is now closed — the merge triggered a genuine Railway
Docker build which ran the real test stage end to end (`Ran 794 tests in
55.986s / OK`) and verified both gatekeeper artifacts inside the finished image.
The unproven step is proven, by the production builder itself.

Findings F1 (medium) and F2–F4 (low) remain open and are recorded in
`release_evidence.json`. F1 — a five-test discrepancy between the two test
runners — must be closed before capital-live, because the artifact the LIVE
gatekeeper reads is produced by the runner that counts fewer.

---

## 4. Machine-readable evidence

`release_evidence.json` at the repository root carries the same facts in
structured form, including the open findings and the unmet capital-live gates.

---

## 5. Investor summary

### What is ready

The **safety and execution infrastructure** is built, deployed, and running in
production against Kalshi's DEMO environment. It scans ~200 markets per cycle,
prices them, applies risk limits, and reconciles its ledger against the broker
on every pass. The deployment pipeline is reproducible: a commit becomes an
image only if its full test suite passes inside that image's own build.

### What has been independently verified

- **794 tests, 0 failures, 0 errors**, executed by the production builder inside
  the image that is deployed — not on a developer machine.
- **Two independent security reviews** by reviewers with no authorship of the
  code. The first found a genuine blocker that was fixed before merge; the
  second found **no blockers and no high-severity findings**.
- **Deployed bytes equal reviewed bytes** — verified file by file.
- **Reconciliation `MATCH`** between broker and local ledger, at startup and on
  a recurring cycle.

### What the system safely refuses to do

- It **will not submit an order.** The global gate is off, and a malformed value
  for that gate is read as "off", never as "on".
- It **will not trade daily BTC markets**, because it cannot yet independently
  verify how they settle. Two separate guards enforce this.
- It **will not act on a ticker it cannot classify.** It refuses rather than
  normalising an invalid input into a valid-looking one.
- It **will not go live**, even when the promotion switches are set to their
  most permissive values. The model gate refuses, and names its reason.
- It **will not ship from a red build**, and **will not continue** when it
  cannot persist state or when its ledger disagrees with the broker.

### What remains before capital-live

1. **A model with demonstrated out-of-sample edge.** `MODEL_APPROVED=false`
   today. A walk-forward study across twelve candidates found **none** that beat
   the market baseline by more than the noise in the sample. This is an honest
   negative result, and it is the binding constraint.
2. **A verifiable daily settlement oracle (BRTI).** Until it exists, KXBTCD
   stays quarantined.
3. **Finding F1 closed**, so the artifact the LIVE gatekeeper reads counts every
   test.
4. **A DEMO canary** under live gates, then a staged capital introduction.

### Why this is a safety feature, not a failure

A trading system that cannot yet prove an edge has exactly two honest options:
refuse to trade, or trade anyway and call the result strategy. This one refuses,
and the refusal is enforced in code by independent mechanisms rather than by
convention or by an operator remembering.

The most valuable evidence in this package is not that the tests pass. It is
that **the gates were tested against their permissive case and still said no**:
the model gate refused with promotion variables set to YES; the daily quarantine
refused with the global gate forced open. A gate that only ever sees inputs it
would reject anyway has not been tested. These were.

The negative model result was produced by the same discipline. It would have
been easy to report a favourable in-sample number; the harness was built
specifically to make that impossible, and it ruled the model out. An
infrastructure that reports "no edge yet" when there is no edge yet is the
prerequisite for trusting it the day it reports otherwise.

**No claim of profitability is made. No claim of live-capital readiness is made.
`MODEL_APPROVED=false`.**

---

*Environment: Kalshi DEMO. No real capital is at risk. All figures are
reproducible from `MAIN_SHA` `9b906e8` and `DEPLOYMENT_ID`
`2a3da86f-d925-4c4e-9eba-fd48d41ba9c9`.*
