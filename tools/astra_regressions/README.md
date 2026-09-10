# Defensive regression harnesses

All scenarios use temporary economic state and synthetic broker data. Do not use
live credentials or endpoints. CAPITAL stays OFF and real broker writes stay 0.

To reproduce all **59 historical unsafe BEFORE cases** and their AFTER assertions,
use the preserved baseline fixtures and the comparison runner:

```
python tools/astra_regressions/reproduce_historical.py --before /path/to/local-3af848e-tree --after . --output /tmp/atlas-historical-reproduction
```

Both trees must already exist locally; the BEFORE tree must be a clean checkout
of `3af848e6aebc8769ea59f878d68674f89e04af0d`. The runner executes all 304 scenarios
on each selected tree, checks the exact 59 original failure assertions, verifies
the loaded production source, and writes fresh logs and source hashes. It does
not consume retained result JSON as proof. All AFTER scenarios must pass. See
[`historical-reproduction.md`](../../docs/audits/historical-reproduction.md) for
the complete identifier mapping, commands, seven original uncertainties, and
fixture adaptations. Use `--phase before` or `--phase after` for a single side.

Run from the repository root with installed test dependencies. The launcher
starts Python in a credential-free environment, creates a fresh DATA_DIR and
blocks external DNS/connect/sendto. The repository suite alone may use loopback
for its local dashboard tests. Set ATLAS_AUDIT_DEPENDENCIES to an existing local
site-packages directory only when the selected Python needs it; nothing is
installed or downloaded by this runner.

```
python tools/astra_regressions/run_isolated.py . /tmp/atlas-repo.log run_tests.py
python tools/astra_regressions/run_isolated.py . /tmp/atlas-108.log tools/astra_regressions/original108_remediated.py /tmp/atlas-108.json
python tools/astra_regressions/run_isolated.py . /tmp/atlas-190.log tools/astra_regressions/reliability_review.py /tmp/atlas-190.json
python tools/astra_regressions/run_isolated.py . /tmp/atlas-global.log tools/astra_regressions/confirm_global_guards.py /tmp/atlas-global.json
python tools/astra_regressions/run_isolated.py . /tmp/atlas-new.log tools/astra_regressions/before_after_interactions.py /tmp/atlas-new.json
```

The 96 new repository tests are in test_engine_authority.py (66) and
test_authority_cross_component.py (30). The 4 before/after interactions are
additional same-code comparisons against the rejected tree.

original108.py preserves the first independent harness. The completeness-only
508899b-to-3af848e fixture adaptation is preserved in original108_contract_adapted.py.
original108_remediated.py keeps all 108 scenarios and their economic assertions,
with current identity/independent-checkpoint fixtures, private-object API handling,
explicit recovery, historical timestamps established at ingestion, and a separate
synthetic demo root for the transport positive control. Broker responses never
leave in-memory adapters.

The 190-case reliability harness retains its scenarios. Its adaptations declare
complete current intent fields and gatekeeper criteria, recognize a duplicate
refused at ingestion while retaining the drawdown assertion, explicitly complete
verified recovery, and rebuild manager authority in forked crash workers.
Those workers explicitly install the test host's existing independent public-key
pins in their own process before loading managers; they retain the original
external checkpoint and never bootstrap it from the local economic files.
The original baseline `reliability_review.py`, `fixture.py`, and global controls
are separately preserved byte for byte in `baseline_3af848e/`; their hashes are
recorded in `historical_cases.json`. Use those baseline files when reviewing the
rejected commit, rather than importing the current contract fixture there.

A process exit, a stale fixture or an unhandled exception is not evidence of a
passing safety assertion. All controls must run to completion. The before/after
4-case file uses identical code on both versions and retains the failures on the
rejected tree.
