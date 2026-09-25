"""Synthetic fixtures validate software only; never evidence of trading edge."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from atlas_v2.alpha_lab import (cohort, drawdown, economic_diagnostic, labelled_diagnostics,
    paired_metrics, plan, preregister, probability, run_experiments)
from atlas_v2.domain import Refused, digest
from atlas_v2.execution import Quote
from atlas_v2.research_export import export_database, observations_from_snapshot, verify_snapshot
from atlas_v2.store import Store


def observation(at="2026-09-25T12:10:00Z", ticker="KXBTC15M-A", **changes):
    row = {"hash":digest(at+ticker),"ticker":ticker,"event_id":ticker,"bid":"0.59","ask":"0.61",
           "observed_at":at,"close_at":"2026-09-25T12:15:00Z"}
    row.update(changes)
    return row


class AlphaLabTests(unittest.TestCase):
    def test_preregistration_cannot_overwrite_and_has_five_families(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"plan.json"
            value = preregister(path,"a"*40)
            self.assertEqual(value["plan_hash"],digest(plan()))
            self.assertEqual(len(value["plan"]["families"]),5)
            with self.assertRaises(FileExistsError): preregister(path,"a"*40)

    def test_cohort_is_outcome_blind_unique_event_and_derived_day(self):
        a = observation("2026-09-25T12:09:00Z",bid="0.49",ask="0.51")
        b = observation()
        c = observation("2026-09-25T12:11:00Z")
        rows, exclusions = cohort([c,b,a])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["day"],"2026-09-25")
        self.assertEqual(rows[0]["prior_mid"],"0.50")
        self.assertEqual(exclusions["later_observation_same_event"],1)
        self.assertEqual(probability("microstructure_dislocation_v1",rows[0]),Decimal("0.550"))
        with self.assertRaises(Refused): cohort([observation(candidate_probability="0.9")])

    def test_missing_reference_not_imputed_from_labels(self):
        row = cohort([observation()])[0][0]
        for name in ("underlying_kalshi_lag_v1","volatility_regime_v1","cross_market_consistency_v1"):
            with self.assertRaises(Refused): probability(name,row)
        with self.assertRaises(Refused):
            probability("underlying_kalshi_lag_v1",row,{"reference_now":{"price":"101","at":"2026-09-25T12:10:01Z","receipt":"a"},
                "reference_previous":{"price":"100","at":"2026-09-25T12:09:01Z","receipt":"b"}})

    def test_market_baseline_is_paired_not_replaced_by_candidate(self):
        result = paired_metrics(["0.8","0.2"],["0.5","0.5"],[1,0])
        self.assertEqual(result["candidate"]["brier"],"0.04")
        self.assertEqual(result["market"]["brier"],"0.25")
        self.assertEqual(result["brier_delta"],"-0.21")
        with self.assertRaises(Refused): paired_metrics(["0.8"],["0.5","0.5"],[1])

    def test_missing_label_blocks_complete_cohort_no_selected_subset(self):
        rows = cohort([observation()])[0]
        with self.assertRaises(Refused): labelled_diagnostics("time_structure_v1",rows,{}, {})

    def test_drawdown_units_are_explicit(self):
        a = drawdown(["-1"],"10")
        b = drawdown(["-1"],"100")
        self.assertEqual(a["maximum_drawdown_dollars"],"1")
        self.assertEqual(a["maximum_drawdown_fraction"],"0.1")
        self.assertEqual(b["maximum_drawdown_fraction"],"0.01")
        with self.assertRaises(Refused): drawdown(["-1"],"0")

    def test_shared_economics_refuses_stale_spread_cost_and_no_liquidity(self):
        q = Quote("KXBTC15M-A","yes","0.49","0.50","1","2026-09-25T12:10:00Z","2026-09-25T12:15:00Z","a"*64)
        result = economic_diagnostic(q,"0.8","10","0.001","0.01",q.observed_at)
        self.assertEqual(result["economics"]["fee_bound"],"0.01")
        self.assertFalse(result["would_submit"])
        for bad in (replace(q,bid="0.1"),replace(q,available="0"),replace(q,observed_at="2026-09-25T12:09:00Z")):
            with self.assertRaises(Refused): economic_diagnostic(bad,"0.8","10","0.01","0.01",q.observed_at)
        with self.assertRaises(Refused): economic_diagnostic(q,"0.8","10","0.30","0.01",q.observed_at)

    def test_absent_evidence_is_null_not_zero_pnl_or_edge(self):
        registration={"registered_at":"2026-09-25T12:00:00Z","plan":plan(),"plan_hash":digest(plan())}
        result=run_experiments([],registration,digest([]),"2026-09-25T13:00:00Z")
        self.assertIsNone(result["candidate_lock"])
        for experiment in result["experiments"]:
            self.assertIsNone(experiment["net_hypothetical_pnl"])
            self.assertFalse(experiment["scientific_rejection"])
            self.assertFalse(experiment["model_approved"])
        altered=deepcopy(registration); altered["plan"]["scope"]="winner subset"
        with self.assertRaises(Refused): run_experiments([],altered,digest([]),"2026-09-25T13:00:00Z")

    def test_export_includes_wal_without_mutating_ledger_and_requires_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"data.sqlite"
            store=Store(path)
            store.append("first","EVIDENCE",{"native":False})
            before=store.anchor()
            snapshot=export_database(path)
            self.assertEqual(verify_snapshot(snapshot,before),before)
            self.assertEqual(store.anchor(),before)
            bad=deepcopy(snapshot); bad["events"][0]["payload"]={"changed":True}
            with self.assertRaises(Refused): verify_snapshot(bad,before)
            with self.assertRaises(Refused): verify_snapshot(snapshot,None)
            store.append("second","EVIDENCE",{})
            with self.assertRaises(Refused): verify_snapshot(snapshot,store.anchor())
            newer=export_database(path)
            with self.assertRaises(Refused): observations_from_snapshot(newer,before)
            store.close()
            missing=Path(directory)/"does-not-exist.sqlite"
            with self.assertRaises(FileNotFoundError): export_database(missing)
            self.assertFalse(missing.exists())


if __name__ == "__main__": unittest.main()
