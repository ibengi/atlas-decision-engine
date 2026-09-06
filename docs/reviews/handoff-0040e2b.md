# Review handoff — candidate `0040e2b`

This brief exists so that a reviewer who has not worked on this change can
start from the change itself. It deliberately contains **no findings, no
grading, no conclusions, no suspected weak points and no prior reviewer
commentary**. Design your own adversarial tests; nothing here is a substitute
for them, and nothing here should steer where you look.

## 1. Base SHA

```
d5417535874531e51a7df112f1585f21e4066fa9   (main, currently deployed)
```

## 2. Candidate SHA

```
0040e2b7fba56024d2df16459a38d8befc239ce0
```

Parent: `054a2e1a8130b45447ae57ce48fab36413d7ee1c`. The candidate supersedes
that commit rather than adding to it — function signatures introduced there
changed — so review `d541753..0040e2b` as one change, not the two commits
separately.

## 3. Files changed

```
execution_engine.py                       +201  -15
tests/test_readonly_observation_scan.py   +859   -0   (new file)
```

Two files. No other production module, configuration file, workflow,
`Dockerfile`, `Procfile` or dependency manifest is in the diff.

## 4. Commands

Create a detached worktree at the candidate (leaves the checkout untouched):

```bash
cd /home/user/atlas-decision-engine
git worktree add --detach /tmp/review-0040e2b 0040e2b
cd /tmp/review-0040e2b
```

Inspect the full diff:

```bash
git diff d541753 0040e2b                          # everything
git diff d541753 0040e2b -- execution_engine.py   # production only
git diff --stat d541753 0040e2b
```

Commit metadata:

```bash
git log --format='%H%n%an <%ae>%n%ad%n%n%B' --date=iso -1 0040e2b
git log --format='%H %ad %s' --date=iso d541753..0040e2b
git cat-file -p 0040e2b | head -5
```

Canonical suite — this is the runner the Docker build and the release gate
use. It writes `test_report.json` and exits non-zero on any failure:

```bash
python3 run_tests.py
```

pytest — the second runner. `pytest.ini` sets `testpaths = tests`:

```bash
python3 -m pytest -q -p no:randomly
```

Individual targets:

```bash
python3 -m pytest tests/test_readonly_observation_scan.py -q -p no:randomly
python3 -m unittest tests.test_readonly_observation_scan -v
python3 -m pytest tests/<any_other_file>.py -q -p no:randomly
```

The suite runs offline. Tests that need it isolate `DATA_DIR` themselves; if
you write your own harness, isolate it yourself (`CFG.DATA_DIR`) so state does
not leak between cases.

Clean up:

```bash
cd /home/user/atlas-decision-engine
git worktree remove --force /tmp/review-0040e2b
```

## 5. Intended functional behavior

In PRODUCTION READ_ONLY, a cycle may continue OBSERVING the market after a
classified capital-risk guard fires, instead of stopping before the scan.
Broker writes remain impossible throughout.

## 6. Invariants to verify

- DEMO behavior is unchanged.
- CAPITAL behavior is unchanged.
- In READ_ONLY, broker mutation is impossible.
- Unknown guards and integrity guards fail closed.
- The durable evidence record accurately represents what the cycle observed.
- The sequential and parallel cycle paths behave consistently.
- No production state, credential, or Railway change is required to review
  this candidate; it can be assessed entirely from the repository.

## 7. Production context

```
main                = d541753          (deployed)
candidate 0040e2b   = NOT merged, NOT deployed
PROD_ACCESS_MODE    = READ_ONLY
reconciliation      = MATCH
broker writes       = 0
```

CAPITAL mode is not authorized. Approval of this candidate would authorize
PROD READ_ONLY full shadow only — not CAPITAL, not broker writes, not real
orders, not a LIVE canary.

## 8. Branch

```
claude/railway-atlas-readonly-shadow-scan   (origin and local, tip = 0040e2b)
```
