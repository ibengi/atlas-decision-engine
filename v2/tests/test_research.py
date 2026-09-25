from decimal import Decimal
from unittest.mock import patch
from atlas_v2.domain import Refused, digest
from atlas_v2.validation import record_prediction, evaluate_future
from test_invariants import WithStore, H


class PairedScoringTests(WithStore):
    def cohort(self):
        with patch('atlas_v2.store.now', return_value='2026-09-25T18:01:00Z'):
            lock = self.lock()
        ids, labels = [], {}
        for number, (p, y) in enumerate((('0.8', 1), ('0.2', 0))):
            with patch('atlas_v2.store.now', return_value='2026-09-25T18:02:00Z'):
                obs = self.store.append('o:' + str(number), 'OBSERVATION', {
                    'observed_at': '2026-09-25T18:02:00Z', 'close_at': '2026-09-25T18:05:00Z',
                    'event_id': 'e:' + str(number), 'ticker': 't:' + str(number), 'baseline_probability': '0.5'})
            with patch('atlas_v2.store.now', return_value='2026-09-25T18:03:00Z'), patch('atlas_v2.validation.now', return_value='2026-09-25T18:03:00Z'):
                prediction = record_prediction(self.store, lock['event_id'], obs['event_id'], p, '0.5')
            ids.append(prediction['event_id'])
            labels[prediction['event_id']] = {'outcome': y, 'authority_receipt': H,
                'settled_at': '2026-09-25T18:06:00Z', 'period': 'synthetic-period:' + str(number),
                'fee': '0.01', 'slippage': '0.01', 'gross_pnl': '0.50'}
        dataset = digest({'lock': lock['hash'], 'predictions': ids, 'labels': labels})
        return lock, ids, labels, dataset

    def test_independent_paired_arithmetic_never_approves(self):
        lock, ids, labels, dataset = self.cohort()
        with patch('atlas_v2.validation.now', return_value='2026-09-25T18:07:00Z'):
            result = evaluate_future(self.store, lock['event_id'], ids, labels, dataset)
        self.assertEqual(Decimal(result['brier']), Decimal('0.04'))
        self.assertEqual(Decimal(result['market_brier']), Decimal('0.25'))
        self.assertEqual(Decimal(result['delta']), Decimal('-0.21'))
        self.assertEqual(Decimal(result['net_pnl']), Decimal('0.96'))
        self.assertFalse(result['approved'])

    def test_subsets_tampering_and_future_labels_rejected(self):
        lock, ids, labels, dataset = self.cohort()
        with self.assertRaises(Refused):
            evaluate_future(self.store, lock['event_id'], ids[:1], {ids[0]: labels[ids[0]]}, dataset)
        with patch('atlas_v2.validation.now', return_value='2026-09-25T18:07:00Z'), self.assertRaises(Refused):
            evaluate_future(self.store, lock['event_id'], ids, labels, H)
        with patch('atlas_v2.validation.now', return_value='2026-09-25T18:04:00Z'), self.assertRaises(Refused):
            evaluate_future(self.store, lock['event_id'], ids, labels, dataset)

    def test_prediction_crossing_close_boundary_rolls_back(self):
        with patch('atlas_v2.store.now', return_value='2026-09-25T18:01:00Z'):
            lock = self.lock()
        with patch('atlas_v2.store.now', return_value='2026-09-25T18:02:00Z'):
            obs = self.store.append('o', 'OBSERVATION', {'observed_at': '2026-09-25T18:02:00Z',
               'close_at': '2026-09-25T18:05:00Z', 'event_id': 'e', 'ticker': 't', 'baseline_probability': '0.5'})
        with patch('atlas_v2.validation.now', return_value='2026-09-25T18:04:59Z'), patch('atlas_v2.store.now', return_value='2026-09-25T18:05:00Z'), self.assertRaises(Refused):
            record_prediction(self.store, lock['event_id'], obs['event_id'], '0.6', '0.5')
        self.assertEqual(self.store.events('PREDICTION'), [])
