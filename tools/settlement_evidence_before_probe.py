#!/usr/bin/env python3
"""Reproduce the three original settlement-integrity defects, SYNTHETIC ONLY.

Run under the archived baseline's committed audit_isolation launcher. The
repository argument must be an explicit archive/checkout of exact 6f0accc.
The probe pins the affected baseline files, imports its complete synthetic
source fixture, and writes only disposable ledgers plus the requested report.
It creates no real provider, market, settlement, authority, or billing evidence.
"""

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


BASE_SHA = "6f0accc551e06debfa1cef99ff72ddc01c6ccfaa"
BASE_FILES = {
    "alpha_ledger.py": "b0841641491ec267ff7c0a952041d2328f891dba126988ec92fed19ab4ef77f8",
    "alpha_resolution_ingest.py": "e8b54ad262ae8adf324780073db435f8ad1486c059e21ca8ca1e4953a3321dfa",
    "alpha_settlement_validation.py": "6cee51bcb6a7b0c41464c712a4b21ed1d6f59ab964035d6aa488b1edc1b420d3",
    "tests/_settlement.py": "37072384e2b075682054c5e663c9d6d352d535061908965fadbb05f8e2cb8cd5",
    "tests/_candidate.py": "ea3b492d19ca389347421455fc646658e8713c0647e297b91c88c15a1e79aa04",
}
AUTHORITY = "SYNTHETIC-ONLY-BASELINE-AUTHORITY"
MODEL = "synthetic-only-baseline-model"
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True,
                        help="explicit archived baseline directory")
    parser.add_argument("--output", required=True,
                        help="local synthetic reproduction JSON report")
    args = parser.parse_args(argv)
    repo = Path(args.repository).resolve()
    for name, expected in BASE_FILES.items():
        actual = hashlib.sha256((repo / name).read_bytes()).hexdigest()
        if actual != expected:
            parser.error("baseline file differs from exact " + BASE_SHA + ": " + name)
    sys.path.insert(0, str(repo))
    from alpha_ledger import AlphaLedger
    from alpha_learning import score_model
    from alpha_resolution_ingest import ingest_settlements
    from tests._candidate import raw_market, valid_record
    from tests._settlement import qualified_fixture

    # Complete canonical source evidence is constructed locally through the
    # baseline producer and consumer, with a deterministic synthetic clock.
    market = raw_market(
        ticker="KX-SYNTHETIC-BASELINE-ONLY", event_ticker="SYNTHETIC-EVENT",
        title="Fictional local settlement-integrity regression",
        rules_primary="SYNTHETIC ONLY: never a real settlement statement.",
        settlement_sources=[{"name": AUTHORITY}],
        close_time=(NOW + timedelta(hours=1)).isoformat(),
        expiration_time=(NOW + timedelta(hours=2)).isoformat())
    record = valid_record(market, observed_at_utc=(NOW - timedelta(seconds=3)).isoformat())
    _, _, prediction, settlement = qualified_fixture(
        now=NOW, record=record, source=AUTHORITY,
        prediction_id="SYNTHETIC-ONLY-BASELINE-PREDICTION",
        p_meta=.75, per_model={MODEL: {"p_yes": .75}},
        state="SYNTHETIC_ONLY_LOCAL_FORECAST", executed=False,
        authorized=False)
    receipt = {
        "synthetic_test_evidence": True, "authority": AUTHORITY,
        "contract_id": prediction["contract_id"], "outcome": 1,
        "resolved_at": settlement["resolved_at"],
        "note": "Fictional local witness; not an exchange statement.",
    }
    preimage = canonical(receipt).encode("utf-8")
    digest = hashlib.sha256(preimage).hexdigest()
    settlement.update({
        "settlement_evidence_id": "SYNTHETIC-sha256-" + digest,
        "settlement_response_sha256": digest,
        "settlement_response_bytes_base64": base64.b64encode(preimage).decode("ascii"),
    })
    variants = [
        ("positive_synthetic_control", None, None),
        ("different_well_formed_evidence_id", "settlement_evidence_id",
         "SYNTHETIC-UNKNOWN-EVIDENCE-NOT-IN-RECEIPT"),
        ("altered_response_digest", "settlement_response_sha256", "0" * 64),
        ("altered_response_preimage", "settlement_response_bytes_base64",
         base64.b64encode(b'{"synthetic_test_evidence":true,"outcome":0}').decode("ascii")),
    ]
    cases = []
    for name, field, value in variants:
        candidate = copy.deepcopy(settlement)
        if field is not None:
            candidate[field] = value
        with tempfile.TemporaryDirectory(prefix="atlas-synthetic-settlement-before-") as temporary:
            ledger_path = Path(temporary) / "synthetic-predictions.jsonl"
            cost_path = Path(temporary) / "synthetic-costs.jsonl"
            ledger = AlphaLedger(str(ledger_path), str(cost_path))
            with patch("alpha_settlement_validation._utc_now", return_value=NOW):
                ledger.record_prediction(copy.deepcopy(prediction))
                prefix = ledger_path.read_bytes()
                ingestion = ingest_settlements(ledger, [candidate], trusted_sources=[AUTHORITY])
                qualified = ledger.qualified_resolved()
                calibration = ledger.calibration(MODEL)
                fresh = AlphaLedger(str(ledger_path), str(cost_path))
                stored = fresh.find_resolution(prediction["prediction_id"])
                cases.append({
                    "case": name, "synthetic_only": True,
                    "appended": ingestion["appended"],
                    "qualified_resolved": len(qualified),
                    "learning_samples": score_model(qualified, MODEL)["samples"],
                    "calibration_samples": 0 if calibration is None else calibration["samples"],
                    "restart_qualified_resolved": len(fresh.qualified_resolved()),
                    "prediction_prefix_unchanged": ledger_path.read_bytes().startswith(prefix),
                    "stored_evidence_id": (stored or {}).get("settlement_evidence_id"),
                    "stored_response_digest": (stored or {}).get("settlement_response_sha256"),
                    "stored_response_preimage": "settlement_response_bytes_base64" in (stored or {}),
                })
    original_defect_reproduced = all(
        row["appended"] == row["qualified_resolved"] == row["learning_samples"] ==
        row["calibration_samples"] == row["restart_qualified_resolved"] == 1 and
        row["prediction_prefix_unchanged"] for row in cases)
    assert not any(name in sys.modules for name in
                   ("kalshi_client", "execution_engine", "order_manager", "risk_manager"))
    report = {
        "schema": "atlas-settlement-before-probe-v1", "base_sha": BASE_SHA,
        "verified_baseline_files": BASE_FILES, "synthetic_only": True,
        "synthetic_receipt": receipt, "synthetic_receipt_sha256": digest,
        "positive_controls": 1, "unsafe_variants": 3,
        "original_defect_reproduced": original_defect_reproduced,
        "cases": cases, "network_requests": 0, "provider_requests": 0,
        "broker_writes": 0, "historical_ledger_rewrites": 0,
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"original_defect_reproduced": original_defect_reproduced,
                      "positive_controls": 1, "unsafe_variants": 3,
                      "output": str(Path(args.output).resolve())}))
    return 0 if original_defect_reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
