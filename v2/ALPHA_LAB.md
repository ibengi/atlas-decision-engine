# Phase 2 Alpha Lab

Five fixed formulas are defined in `atlas_v2.alpha_lab.plan()`. There is no model
search, autonomous approval, external API call, financial mutation or service
import of this research module. Historical diagnostics are not final OOS.

## Evidence access

The current public collector stores raw responses, complete scans and quotes.
It does **not** collect authoritative labels, underlying candles, synchronized
strike ladders, refreshed executable depth or fee/slippage receipts. Consequently
the native-cohort runner reports `NOT_TESTABLE`, with unavailable metrics null.
It does not reject a statistical hypothesis from absent data. `paired_metrics`
and `labelled_diagnostics` support historical diagnostics once inputs exist;
they do not authenticate a supplied label receipt or qualify an edge.

From a checkout of the recorded research revision:

```sh
PYTHONPATH=v2 python -m atlas_v2.alpha_lab preregister registration.json --code-sha RESEARCH_COMMIT_SHA
```

On the host with the dedicated V2 mounted volume, with the research package
available offline (no service replacement required):

```sh
PYTHONPATH=v2 python -m atlas_v2.research_export /data/atlas-v2/observations.sqlite snapshot.json
```

This opens SQLite in `mode=ro`, sets query-only and uses one read transaction.
It includes committed WAL contents and refuses partial export beyond 10,000
events/32 MiB. Existing output files cannot be overwritten. Retain an independent
collector log anchor matching the final export sequence/hash. Never copy only
the main file while SQLite WAL writes continue. Transfer the snapshot and the
native log receipt to research, then:

```sh
PYTHONPATH=v2 python -m atlas_v2.alpha_lab run snapshot.json anchor.json registration.json experiment.json --cutoff FIXED_UTC_CUTOFF
```

Every family uses the same immutable input and deterministic one-row-per-event
cohort. Midpoint is a **predictive** baseline, never an executable fill. Ask
baseline is also available in labelled diagnostics. No observed or future price
can replace a refreshed executable quote. Economics reuse `execution.reprice`;
portfolio reservations remain governed by the existing `reserve_shadow` path,
including durable one-intent-per-market, three positions and the 20% guard.

## Qualification still required

This commit adds research diagnostics, not a qualified candidate or a complete
prospective execution study. An authenticated feature/label/cost importer,
fixed independent validation window, multiplicity-controlled day-block test,
costed portfolio simulation and complete candidate lock must precede prospective
qualification. Diagnostic hashes establish integrity, not source authority.
Additional regime and lag-duration analyses are not claimed to have run.
Sub-minute lag cannot be identified from minute polling. Different maturities
are not assumed to be nested events.

No surviving candidate means no lock, no OOS start timestamp, no deployment and
no Claude candidate review. Retain failed runs and V1 consumed-data labels.

## Phase 2B private export

The V2 collector can create an export before its collection thread starts when
`ATLAS_V2_EXPORT_ON_START=1`. This exports the existing dedicated V2 public ledger
to `/data/atlas-v2/exports/<dataset_sha256>/` as deterministic gzip/base64 text
chunks of at most 60,000 bytes and `manifest.json`. It adds no HTTP route. The
manifest includes raw event and observation counts, distinct markets, UTC
coverage, kind counts, full snapshot SHA-256 and final chain anchor. Pagination
receipts, invalid rows and failed scans remain present; non-public event kinds
cause the entire export to fail. It never silently filters a mixed ledger.

The `RESEARCH_EXPORT_READY` native log binds the manifest hash, dataset hash,
anchor and exact runtime SHA. Retrieve every chunk and use `read_bundle` plus
`observations_from_snapshot`; truncated, changed or incomplete artifacts are
refused. An export failure is logged and does not stop public collection.

An export-only V2 collector release is distinct from a shadow model deployment.
It leaves all five formulas and the preregistered plan hash unchanged. The
drawdown helper now explicitly reports dollars, fraction, percentage and its
hypothetical equity base. Neither diagnostic calculations nor export grant
candidate qualification or financial authority.

Software verification (synthetic inputs only):

```sh
PYTHONPATH=v2 python -m unittest discover -s v2/tests
python v2/mutate.py
```
