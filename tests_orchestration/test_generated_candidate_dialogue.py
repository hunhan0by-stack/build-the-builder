"""Policy assertions against the actual V13 graph, with typed customer turns.

Expectations come from its archived synthetic input brief, not model coverage.
No speech extraction accuracy or external platform behavior is asserted.
"""
import unittest

from tests_orchestration.generated_workflow_harness import NativeGraphHarness, ORIGINAL, ROOT, candidate_path, event, ok, products

# Normal regression runs exercise the repaired diagnostic candidate. The audit
# runner explicitly selects ORIGINAL to preserve the before/after comparison.
CANDIDATE = ROOT / 'project-management/p0-dialogue-tests-20261011/candidate/agent.workflow.json'


class GeneratedCandidateDialogueTests(unittest.TestCase):
    def setUp(self):
        candidate_path(CANDIDATE)

    def make(self, **kwargs):
        return NativeGraphHarness(candidate_path(CANDIDATE), **kwargs)

    def test_three_products_joint_confirmation_and_single_last_close(self):
        h = self.make().ready().propose_all()
        self.assertEqual(h.state['stage'], 'confirm')
        self.assertEqual(h.promise_calls(), [])
        h.turn(decision='yes')
        self.assertEqual([p['product_id'] for p in h.promise_calls()], ['CARD', 'LOAN', 'CAR'])
        self.assertEqual(h.calls[-1], dict(operation='close_call', body=dict(customer_id='offline-synthetic', outcome='commitment')))
        self.assertEqual(sum(c['operation']=='close_call' for c in h.calls), 1)
        self.assertIn('Goodbye.', h.speech[-1])
        self.assertTrue(h.ended)

    def test_yes_but_amount_correction_needs_new_complete_confirmation(self):
        h = self.make().ready().propose_all()
        h.turn(decision='yes', events=[event('change', 'LOAN', amount=222.25)])
        self.assertEqual(h.promise_calls(), [])
        self.assertIn('amount 222.25', h.speech[-1])
        self.assertIn('CARD', h.speech[-1]); self.assertIn('CAR', h.speech[-1])
        h.turn(decision='yes')
        self.assertEqual([p['promised_total'] for p in h.promise_calls()], [180, 222.25, 200])
        self.assertEqual([p['due_on'] for p in h.promise_calls()], ['2026-11-13']*3)

    def test_date_only_correction_preserves_its_own_amount(self):
        h = self.make().ready().propose_all()
        h.turn(events=[event('change', 'CAR', date='2026-11-14')])
        self.assertEqual(h.promise_calls(), [])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls()[2]['promised_total'], 200)
        self.assertEqual(h.promise_calls()[2]['due_on'], '2026-11-14')

    def test_ambiguous_change_survives_bare_yes_and_product_only_answer(self):
        h = self.make().ready().propose_all()
        h.turn(events=[dict(intent='change', amount=225.5)])
        self.assertEqual(h.state['question'], 'scope')
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls(), [])
        h.turn(scope_answer='LOAN')
        self.assertIn('225.5', h.speech[-1])
        self.assertEqual(h.promise_calls(), [])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls()[1]['promised_total'], 225.5)

    def test_incomplete_change_blocks_stale_yes(self):
        h = self.make().ready().propose_all()
        h.turn(events=[event('change', 'CARD')])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls(), [])
        self.assertEqual(h.state['question'], 'proposal')

    def test_interruption_retains_change_but_continue_yes_is_not_payment_consent(self):
        h = self.make().ready().propose_all()
        h.turn(interrupt='inconvenient', events=[event('change', 'CARD', amount=201)])
        self.assertEqual(h.state['question'], 'continue')
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls(), [])
        self.assertIn('amount 201', h.speech[-1])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls()[0]['promised_total'], 201)

    def test_decline_continuation_never_creates_promise(self):
        h = self.make().ready().propose_all()
        h.turn(interrupt='inconvenient'); h.turn(decision='no')
        self.assertFalse(h.promise_calls())
        self.assertEqual(h.calls[-1]['body']['reason'], 'refused')

    def test_later_product_information_waits_for_current_product(self):
        h = self.make().ready()
        h.turn(events=[event(scope='LOAN', amount=210)])
        self.assertEqual(h.state['current'], 'CARD')
        self.assertIn('credit card CARD', h.speech[-1])
        h.turn(events=[event(scope='CARD', amount=180, date='2026-11-13')])
        self.assertEqual(h.state['current'], 'LOAN')
        self.assertEqual(h.state['products'][1]['amount'], 210)
        self.assertIn('exact future payment date', h.speech[-1])

    def test_compound_refusal_and_later_proposal_preserve_both(self):
        h = self.make().ready()
        h.turn(events=[event('refuse', 'CARD')])
        h.turn(decision='no', events=[event(scope='LOAN', amount=210, date='2026-11-13')])
        self.assertEqual(h.state['products'][0]['state'], 'skipped')
        self.assertEqual(h.state['products'][1]['amount'], 210)
        self.assertEqual(h.state['current'], 'CAR')

    def test_cannot_today_answers_persuasion_without_inventing_tomorrow(self):
        h = self.make().ready()
        h.turn(events=[event('refuse')]); h.turn(events=[event('cannot_today')])
        self.assertEqual(h.state['question'], 'proposal')
        self.assertIn('exact future payment date', h.speech[-1])
        self.assertIsNone(h.state['products'][0]['date'])
        self.assertEqual(h.state['products'][0]['persuasion'], 1)

    def test_collective_refusal_has_one_persuasion_per_product(self):
        h = self.make().ready()
        h.turn(events=[event('refuse', 'all_considered')])
        for product in ('CARD', 'LOAN', 'CAR'):
            self.assertEqual(h.state['current'], product)
            self.assertEqual(h.state['question'], 'persuade')
            h.turn(decision='no')
        self.assertTrue(h.ended); self.assertFalse(h.promise_calls())
        self.assertEqual([p['persuasion'] for p in h.state['products']], [1,1,1])

    def test_collective_total_is_not_multiplied_or_allocated_implicitly(self):
        h = self.make().ready()
        h.turn(events=[event(scope='all_considered', amount=600, amount_kind='total', date='2026-11-13')])
        self.assertIn('allocated', h.speech[-1]); self.assertFalse(h.promise_calls())
        self.assertTrue(all(p['amount'] is None for p in h.state['products']))
        h.turn(events=[event(scope='CARD', amount=180)])
        self.assertEqual(h.state['current'], 'LOAN')
        self.assertIn('allocated', h.speech[-1])

    def test_exact_decimal_readback_matches_payment_body(self):
        h = self.make(rows=products(1)).ready()
        h.turn(events=[event(amount=123.456, date='2026-11-13')])
        self.assertIn('123.456', h.speech[-1])
        h.turn(decision='yes')
        self.assertEqual(h.promise_calls()[0]['promised_total'], 123.456)

    def test_floor_cap_holiday_and_weekend_are_not_silently_adjusted(self):
        for amount,date in [(79,'2026-11-13'),(276.36,'2026-11-13'),(180,'2026-11-12'),(180,'2026-11-15'),(180,'2026-11-10')]:
            with self.subTest(amount=amount,date=date):
                h = self.make(rows=products(1)).ready()
                h.turn(events=[event(amount=amount,date=date)])
                h.turn(decision='yes')
                self.assertFalse(h.promise_calls())
                self.assertEqual(h.state['products'][0]['amount'], amount)
                self.assertEqual(h.state['products'][0]['date'], date)

    def test_fourth_product_not_considered(self):
        h = self.make(rows=products(4)).ready().propose_all()
        h.turn(decision='yes')
        self.assertEqual([p['product_id'] for p in h.promise_calls()], ['CARD','LOAN','CAR'])

    def test_paid_empty_and_ineligible_accounts(self):
        for rows in ([], [dict(product_id='PAID',type='credit_card',arrears_balance=0)], [dict(products(1)[0],days_late=71)], [dict(products(1)[0],required_minimum=300)]):
            with self.subTest(rows=rows):
                h = self.make(rows=rows).ready()
                self.assertTrue(h.ended); self.assertFalse(h.promise_calls())
                self.assertEqual(h.calls[-1]['body']['outcome'], 'paid' if not rows or rows[0]['arrears_balance']==0 else 'no_promise')

    def test_claimed_payment_does_not_establish_paid_status(self):
        h = self.make().ready()
        h.turn(events=[event('payment_claim','all_considered')])
        self.assertTrue(h.ended); self.assertFalse(h.promise_calls())
        self.assertEqual(h.calls[-1]['body']['outcome'], 'no_promise')
        self.assertIn('cannot confirm', h.speech[-1])

    def test_definitive_negative_retries_once_then_continues(self):
        negative = dict(status='success',status_code=200,data=dict(ok=False))
        h = self.make(responses={'create_promise':[negative,negative]}).ready().propose_all()
        h.turn(decision='yes')
        self.assertEqual([p['product_id'] for p in h.promise_calls()], ['CARD','CARD','LOAN','CAR'])
        self.assertEqual(h.state['products'][0]['state'], 'failed')
        self.assertIn('Actually confirmed promises:', h.speech[-1])
        self.assertEqual(h.calls[-1]['body']['outcome'],'commitment')

    def test_uncertain_responses_never_retry_but_other_products_continue(self):
        variants = [dict(status='error',status_code=500,data={},header={},message='engine failure'), dict(status='error',message='timeout'), dict(status='success',status_code='200',data={'ok':True}), dict(status='success',status_code=200,data={'ok':'true'}), dict(status='success',status_code=200,data={'ok':1}), dict(status='success',status_code=200,data={}), None]
        for response in variants:
            with self.subTest(response=response):
                h=self.make(responses={'create_promise':[response]}).ready().propose_all()
                h.turn(decision='yes')
                self.assertEqual([p['product_id'] for p in h.promise_calls()],['CARD','LOAN','CAR'])
                self.assertEqual(h.state['products'][0]['state'],'uncertain')
                self.assertIn('may or may not have been recorded', h.speech[-1])
                self.assertNotIn('CARD', h.speech[-1].split('Actually confirmed promises:')[1])

    def test_single_uncertain_write_closes_service_error_not_success(self):
        h=self.make(rows=products(1),responses={'create_promise':[dict(status='error',message='timeout')]}).ready().propose_all()
        h.turn(decision='yes')
        self.assertEqual(h.calls[-1]['body']['reason'],'service_error')
        self.assertEqual(len(h.promise_calls()),1)

    def test_identity_and_account_failure_are_not_retried(self):
        for operation in ('verify_identity','get_account'):
            h=self.make(responses={operation:[dict(status='error',message='timeout')]}).advance()
            h.turn(identity_answer='1234')
            if not h.ended: h.turn(identity_answer='1988-03-21')
            self.assertTrue(h.ended); self.assertFalse(h.promise_calls())
            self.assertEqual(sum(c['operation']==operation for c in h.calls),1)
            self.assertEqual(h.calls[-1]['body']['reason'],'service_error')

    def test_unreadable_identity_and_swap_hint_do_not_pass_verification(self):
        h=self.make(responses={'verify_identity':[dict(status='success',status_code=200,data={'ok':False,'hint':'day_month_swapped'})]}).advance()
        h.turn(identity_answer='1234')
        self.assertEqual(h.state['wrong'],0); self.assertFalse(h.state['identity_passed'])
        self.assertIn('will not swap',h.speech[-1])
        self.assertFalse(any(c['operation']=='get_account' for c in h.calls))

    def test_four_wrong_identity_answers_end_without_account_disclosure(self):
        wrong=ok(verified=False,all_verified=False)
        h=self.make(responses={'verify_identity':[wrong]*4}).advance()
        for unused in range(4): h.turn(identity_answer='9999')
        self.assertTrue(h.ended); self.assertEqual(h.state['wrong'],4)
        self.assertEqual(h.calls[-1]['body']['outcome'],'not_verified')
        self.assertFalse(any(c['operation']=='get_account' for c in h.calls))

    def test_uncertain_close_is_not_retried_or_announced_as_recorded(self):
        h=self.make(rows=[],responses={'close_call':[dict(status='error',message='timeout')]}).ready()
        self.assertTrue(h.ended)
        self.assertEqual(h.vars['closing_record_state'],'unconfirmed')
        self.assertEqual(sum(c['operation']=='close_call' for c in h.calls),1)

    def test_malformed_extraction_must_not_authorize_old_readback(self):
        invalid = [dict(decision='yes',events={}), dict(decision='yes',events=[None]), dict(decision='yes',events=[dict(intent='unknown_change')]), dict(decision='yes',correction={'amount':222}), dict(decision='yes',events='change amount')]
        for u in invalid:
            with self.subTest(extraction=u):
                h=self.make().ready().propose_all(); h.turn(u)
                self.assertFalse(h.promise_calls(), 'Malformed extractor output must not grant consent')
                self.assertFalse(h.ended)

    def test_simultaneous_changes_keep_only_final_explicit_value(self):
        h=self.make().ready().propose_all()
        h.turn(decision='yes',events=[event('change','CARD',amount=220),event('change','CARD',amount=230)])
        self.assertFalse(h.promise_calls()); self.assertIn('amount 230',h.speech[-1])
        h.turn(decision='yes'); self.assertEqual(h.promise_calls()[0]['promised_total'],230)

    def test_top_level_changed_slots_at_confirmation_cannot_be_ignored(self):
        for change in (dict(amount=225), dict(date='2026-11-14')):
            with self.subTest(change=change):
                h=self.make().ready().propose_all()
                h.turn(decision='yes',**change)
                self.assertFalse(h.promise_calls())
                self.assertEqual(h.state['question'],'scope')
                h.turn(scope_answer='LOAN')
                self.assertFalse(h.promise_calls())
                h.turn(decision='yes')
                self.assertEqual(h.promise_calls()[1]['promised_total'],change.get('amount',190))
                self.assertEqual(h.promise_calls()[1]['due_on'],change.get('date','2026-11-13'))

    def test_duplicate_json_keys_cannot_erase_a_correction(self):
        h=self.make().ready().propose_all()
        h.turn_raw('{"decision":"yes","events":[{"intent":"change","scope":"CARD","amount":220}],"events":[]}')
        self.assertFalse(h.promise_calls())
        h.turn(decision='yes')
        self.assertFalse(h.promise_calls())

    def test_malformed_turn_requires_restated_request_before_fresh_consent(self):
        h=self.make().ready().propose_all()
        h.turn(dict(decision='yes',events='change CARD amount'))
        self.assertFalse(h.promise_calls())
        h.turn(decision='yes')
        self.assertFalse(h.promise_calls())
        h.turn(events=[event('change','CARD',amount=215)])
        self.assertFalse(h.promise_calls()); self.assertIn('amount 215',h.speech[-1])
        h.turn(decision='yes'); self.assertEqual(h.promise_calls()[0]['promised_total'],215)

    def test_malformed_identity_recovers_without_consuming_wrong_answer(self):
        h=self.make().advance()
        h.turn(dict(identity_answer=1234))
        self.assertFalse(h.calls); self.assertEqual(h.state['wrong'],0)
        h.turn(identity_answer='1234')
        self.assertEqual(h.calls[-1]['body']['field'],'family_code')
        self.assertEqual(h.state['field'],'due_on_of_birth')

    def test_irrelevant_identity_or_scope_answer_cannot_clear_recovery_hold(self):
        for irrelevant in (dict(identity_answer='1234'),dict(scope_answer='LOAN'),dict(scope_answer='unknown')):
            with self.subTest(irrelevant=irrelevant):
                h=self.make().ready().propose_all()
                h.turn(dict(decision='yes',events={}))
                self.assertFalse(h.promise_calls())
                h.turn(irrelevant); h.turn(decision='yes')
                self.assertFalse(h.promise_calls())

    def test_continuation_does_not_erase_malformed_turn_hold(self):
        h=self.make().ready().propose_all()
        h.turn(dict(decision='yes',events={}))
        self.assertFalse(h.promise_calls())
        h.turn(interrupt='inconvenient')
        self.assertEqual(h.state['question'],'continue')
        h.turn(decision='yes'); h.turn(decision='yes')
        self.assertFalse(h.promise_calls())
        h.turn(events=[event('change','CARD',amount=215)])
        self.assertFalse(h.promise_calls())
        h.turn(decision='yes'); self.assertEqual(h.promise_calls()[0]['promised_total'],215)


if __name__ == '__main__':
    unittest.main()
