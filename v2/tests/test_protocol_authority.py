"""Synthetic governance regressions; no training results or runtime authority."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from atlas_v2 import protocol_authority as a, reconstruction as r, service
from atlas_v2.domain import Refused, digest, utc
from atlas_v2 import training_protocol as old
from tools import shadow_pnl as pnl
import test_reconstruction as fixtures
legacy, AT, SHA = fixtures.legacy, fixtures.AT, fixtures.SHA


class AuthorityTests(unittest.TestCase):
    def claims(self):
        return json.loads(Path(a.__file__).with_name('PROTOCOL_AUTHORITY.json').read_bytes())['claims']

    def test_active_overlap_rejected_and_disjoint_allowed(self):
        claims=self.claims();claims[1]['status']='ACTIVE'
        with self.assertRaisesRegex(Refused,'^PROTOCOL_AUTHORITY_CONFLICT$'):a.assert_no_overlap(claims)
        claims[1]['windows']=[['2026-09-01T00:00:00Z','2026-09-27T00:00:00Z']]
        self.assertEqual(len(a.assert_no_overlap(claims)),2)
        claims[1]['windows']=[['2026-09-27T00:00:00Z',None]];claims[1]['markets']=['KXSPORTS']
        self.assertEqual(len(a.assert_no_overlap(claims)),2)

    def test_startup_checks_authority_before_any_state_or_network(self):
        with patch.object(a,'authority',side_effect=Refused('PROTOCOL_AUTHORITY_CONFLICT')),patch.object(service,'release_identity') as identity,patch.object(service,'Store') as store:
            with self.assertRaisesRegex(Refused,'PROTOCOL_AUTHORITY_CONFLICT'):service.run()
            identity.assert_not_called();store.assert_not_called()

    def test_historical_wrapper_cannot_reactivate_itself(self):
        original=Path.read_bytes
        def read(path):
            raw=original(path)
            if path.name=='TRAINING_PROTOCOL.json':
                data=json.loads(raw);data['authority_status']='ACTIVE';return json.dumps(data).encode()
            return raw
        with patch.object(Path,'read_bytes',read):
            with self.assertRaisesRegex(Refused,'PROTOCOL_AUTHORITY_CONFLICT'):a.authority()

    def test_historical_payload_hash_unchanged_and_superseded(self):
        wrapped=json.loads(Path(a.__file__).resolve().parents[1].joinpath('TRAINING_PROTOCOL.json').read_bytes())
        self.assertEqual(wrapped['authority_status'],a.SUPERSEDED)
        self.assertEqual(digest(wrapped['protocol']),wrapped['protocol_hash'])
        self.assertEqual(digest(old.protocol()),wrapped['protocol_hash'])
        with self.assertRaisesRegex(Refused,a.SUPERSEDED):a.require_active(a.PHASE2)
        a.require_active(a.MR)

    def test_superseded_protocol_cannot_consume_mr_rows(self):
        for row in ({'protocol_id':a.MR},{'protocol_hash':a.MR_HASH},{'candidate_family':'MR-STRUCTURAL-1'},{'feature_schema':'MR-FEATURES-1'}):
            with self.assertRaisesRegex(Refused,'SUPERSEDED_PROTOCOL_CANNOT_CONSUME_MR_ROWS'):a.reject_mr_rows([row])
            for operation in (lambda:old.train_family('time_structure_v1',[row],old.FIT_AT),
                              lambda:old.evaluate_oos({},[row],old.FIT_AT),
                              lambda:old.paired_gate([row],{})):
                with self.assertRaisesRegex(Refused,'SUPERSEDED_PROTOCOL_CANNOT_CONSUME_MR_ROWS'):operation()

    def test_direct_legacy_fit_and_oos_are_retired(self):
        for operation in (lambda:old.train_family('time_structure_v1',[],old.FIT_AT),lambda:old.evaluate_oos({},[],old.FIT_AT)):
            with self.assertRaisesRegex(Refused,a.SUPERSEDED):operation()

    def test_protocol_hash_mismatch_rejected(self):
        plan=deepcopy(a.authority());plan['baseline']='changed'
        with self.assertRaisesRegex(Refused,'MR_PROTOCOL_HASH_MISMATCH'):a.assert_registry(plan)

    def test_exact_dates_no_overlap_and_boundary_ownership(self):
        expected={'TRAIN':['2026-09-27T00:00:00Z','2026-10-11T00:00:00Z'],
                  'CALIBRATION':['2026-10-11T00:00:00Z','2026-10-18T00:00:00Z'],
                  'VALIDATION':['2026-10-18T00:00:00Z','2026-11-01T00:00:00Z']}
        self.assertEqual(a.authority()['windows'],expected)
        for stage,(start,end) in expected.items():
            self.assertEqual(a.stage_at(start),stage)
            self.assertEqual(a.stage_at((utc(end)-timedelta(microseconds=1)).isoformat()),stage)
            self.assertNotEqual(a.stage_at(end) if end!=expected['VALIDATION'][1] else None,stage)
        with self.assertRaises(Refused):a.stage_at('2026-09-26T23:59:59Z')
        with self.assertRaises(Refused):a.stage_at('2026-11-01T00:00:00Z')
        plan=deepcopy(a.authority());plan['windows']['TRAIN'][1]='2026-10-12T00:00:00Z'
        with patch.object(a,'digest',return_value=a.MR_HASH):
            with self.assertRaises(Refused):a.assert_registry(plan)

    def test_oos_next_midnight_strictly_after_lock_and_28_days(self):
        for lock,start in [('2026-11-01T00:00:00Z','2026-11-02T00:00:00+00:00'),('2026-11-01T23:59:59Z','2026-11-02T00:00:00+00:00')]:
            actual,end=a.oos_window(lock);self.assertEqual(actual,start)
            self.assertEqual((utc(end)-utc(actual)).days,28)
            self.assertEqual(a.stage_at(actual,lock),'FUTURE_OOS')
            with self.assertRaises(Refused):a.stage_at(end,lock)
        with self.assertRaises(Refused):a.oos_window('2026-10-31T23:59:59Z')

    def test_only_two_family_multiplicity(self):
        plan=deepcopy(a.authority())
        self.assertEqual([c['id'] for c in plan['candidates']],['MR-STRUCTURAL-1','MR-REGIME-1'])
        self.assertEqual(plan['acceptance']['block_ci_confidence'],.975)
        with patch.object(a,'digest',return_value=a.MR_HASH):
            three=deepcopy(plan);three['candidates'].append({'id':'MR-OTHER-1','family':'other'})
            with self.assertRaisesRegex(Refused,'TWO_FAMILY'):a.assert_registry(three)
            for field,value in [('block_ci_confidence',.95)]:
                other=deepcopy(plan);other['acceptance'][field]=value
                with self.assertRaisesRegex(Refused,'MULTIPLICITY'):a.assert_registry(other)

    def test_missing_fee_or_slippage_never_zero(self):
        for field in ('estimated_fee','estimated_slippage'):
            for value in (None,'bad',float('nan'),float('inf'),-.01,True):
                row=legacy();row[field]=value
                self.assertIsNone(pnl._costs(row));self.assertIsNone(pnl.replay(row))
                result=pnl._summarise([legacy(),row],'test')
                self.assertIsNone(result['total_net']);self.assertIsNone(result['mean_net_per_contract'])
                self.assertIsNone(pnl._by_date([legacy(),row])['2026-09-01'])
        self.assertAlmostEqual(pnl.replay(legacy()),.47)

    def test_missing_cost_whole_profitability_verdict_unavailable(self):
        rows=[legacy(i,ts=f'2026-09-01T00:{i:02d}:00Z') for i in range(10)]
        rows[-1]['estimated_fee']=None
        result=pnl.analyse(rows,'a'*64,brier_delta=-.1,min_traded=1)
        self.assertEqual(result['verdict'],'PROFITABILITY_UNAVAILABLE_MISSING_COSTS')
        self.assertIsNone(result['test']['total_net']);self.assertIsNone(result['selection_value_per_contract'])
        self.assertFalse(result['profitability_qualified'])


class MRDecisionAuthorityTests(unittest.TestCase):
    setUp=fixtures.ReconstructionTests.setUp
    tearDown=fixtures.ReconstructionTests.tearDown
    add=fixtures.ReconstructionTests.add
    model=fixtures.ReconstructionTests.model
    # Reuse native fixtures only, not inherited test methods twice.
    def test_decision_binding_and_row_protocol_checks(self):
        with patch.object(r,'now',return_value=AT),patch('atlas_v2.store.now',return_value=AT):
            decision=r.record_prediction(self.store,self.model(),self.features,SHA)
        p=decision['payload'];expected=a.binding(AT,'structural')
        for k,v in expected.items():self.assertEqual(p[k],v)
        row={**expected,'features':self.features,'source_protocol_id':a.MR,'prior_candidate_uses':[]}
        a.assert_row(row,'TRAIN','structural')
        for key,value in [('protocol_hash','0'*64),('protocol_id',a.PHASE2),('stage','VALIDATION'),('candidate_family','MR-REGIME-1'),('source_protocol_id',a.PHASE2),('prior_candidate_uses',['old-candidate'])]:
            with self.assertRaises(Refused):a.assert_row({**row,key:value},'TRAIN','structural')

    def test_phase2_evidence_not_relabelled_as_mr(self):
        self.store.append('used','L_TRAINING_BATCH',{'ticker':self.features['ticker'],'fit':True})
        with patch.object(r,'now',return_value=AT):
            with self.assertRaisesRegex(Refused,'MR_PHASE2_EXPOSURE'):r.record_prediction(self.store,self.model(),self.features,SHA)

    def test_foreign_native_decision_rejected_before_labels(self):
        row={'features':self.features,'mr_decision_id':'phase2-decision'}
        self.store.append('phase2-decision','L_DECISION',{'features':self.features})
        with self.assertRaisesRegex(Refused,'MR_NATIVE_DECISION_REQUIRED'):r.reconstruct_row(self.store,row)
