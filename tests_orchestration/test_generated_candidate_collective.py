"""Additional policy regressions for collective and compound typed turns.

The default target is the archived, generated V13 graph. Set
GENERATED_DIALOGUE_CANDIDATE to test a repaired or newly generated graph.
These are extraction/native-response fixtures, not speech or Freya tests.
"""
import os
from pathlib import Path
import sys
import unittest

# Keep direct execution usable alongside ``python -m unittest``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests_orchestration.generated_workflow_harness import (
    NativeGraphHarness, ORIGINAL, candidate_path, event, products,
)


class GeneratedCandidateCollectiveTests(unittest.TestCase):
    def setUp(self):
        candidate_path()

    def make(self, current_date=None, **kwargs):
        path = candidate_path()
        harness = NativeGraphHarness(path, **kwargs)
        if current_date is not None:
            harness.vars['current_date'] = current_date
        return harness

    def test_collective_amount_proposal_without_explicit_per_product_needs_allocation(self):
        for kind in ('omitted', None):
            with self.subTest(amount_kind=kind):
                h = self.make().ready()
                proposal = event(scope='all_considered', amount=180, date='2026-11-13')
                if kind is None:
                    proposal['amount_kind'] = None
                h.turn(events=[proposal])
                h.turn(decision='yes')
                self.assertEqual(
                    h.promise_calls(), [],
                    'A collective total cannot become the same amount for every product.',
                )
                self.assertFalse(h.ended)

    def test_collective_amount_change_without_explicit_per_product_needs_allocation(self):
        for kind in ('omitted', None):
            with self.subTest(amount_kind=kind):
                h = self.make().ready().propose_all()
                change = event('change', 'all_considered', amount=200)
                if kind is None:
                    change['amount_kind'] = None
                h.turn(decision='yes', events=[change])
                self.assertEqual(h.promise_calls(), [])
                h.turn(decision='yes')
                self.assertEqual(
                    h.promise_calls(), [],
                    'An unspecified collective total requires an explicit allocation.',
                )
                self.assertFalse(h.ended)

    def test_explicit_collective_per_product_amount_is_permitted(self):
        h = self.make().ready()
        h.turn(events=[event(
            scope='all_considered', amount=180, amount_kind='per_product',
            date='2026-11-13',
        )])
        self.assertEqual(h.promise_calls(), [])
        h.turn(decision='yes')
        self.assertEqual(
            [(p['product_id'], p['promised_total'], p['due_on']) for p in h.promise_calls()],
            [('CARD', 180, '2026-11-13'), ('LOAN', 180, '2026-11-13'),
             ('CAR', 180, '2026-11-13')],
        )

    def test_collective_date_only_change_preserves_distinct_amounts_and_reconfirms(self):
        h = self.make().ready().propose_all()
        h.turn(decision='yes', events=[event(
            'change', 'all_considered', date='2026-11-14',
        )])
        self.assertEqual(h.promise_calls(), [])
        for amount in (180, 190, 200):
            self.assertIn('amount ' + str(amount), h.speech[-1])
        self.assertIn('CARD', h.speech[-1])
        self.assertIn('LOAN', h.speech[-1])
        self.assertIn('CAR', h.speech[-1])
        h.turn(decision='yes')
        self.assertEqual(
            [(p['promised_total'], p['due_on']) for p in h.promise_calls()],
            [(180, '2026-11-14'), (190, '2026-11-14'), (200, '2026-11-14')],
        )

    def test_removal_or_payment_claim_alongside_yes_requires_remaining_set_confirmation(self):
        for intent in ('remove', 'payment_claim'):
            with self.subTest(intent=intent):
                h = self.make().ready().propose_all()
                h.turn(decision='yes', events=[event(intent, 'CARD')])
                self.assertEqual(h.promise_calls(), [])
                readback = h.speech[-1].split('Please confirm this complete payment-promise set:')[-1]
                self.assertNotIn('CARD', readback)
                self.assertIn('LOAN', readback)
                self.assertIn('CAR', readback)
                if intent == 'payment_claim':
                    self.assertIn('cannot confirm', h.speech[-1])
                h.turn(decision='yes')
                self.assertEqual([p['product_id'] for p in h.promise_calls()], ['LOAN', 'CAR'])

    def test_nine_business_day_deadline_binds_before_outer_date_window(self):
        # After Nov 1, Tue-Sat business days excluding the Nov 12 holiday are
        # Nov 3, 4, 5, 6, 7, 10, 11, 13, 14. Nov 17 is business day ten.
        h = self.make(current_date='2026-11-01', rows=products(1)).ready()
        h.turn(events=[event(amount=180, date='2026-11-17')])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls(), [])
        self.assertIn('that date is invalid', h.speech[-1])
        h.turn(events=[event(amount=180, date='2026-11-14')])
        self.assertEqual(h.promise_calls(), [])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls()[0]['due_on'], '2026-11-14')


if __name__ == '__main__':
    unittest.main(verbosity=2)
