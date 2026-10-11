"""Hand-translated local turns for the public scripts; this is not a live official simulation."""
import copy
import json
import unittest

from builder.conversation import Conversation, conversation_plan
from builder.runtime import MockBank
from builder.turns import Turn
from tests.conversation_helpers import flush
from tests.helpers import KIT, fixture


def make_public_replay(case):
    docs, policy, contract = fixture(case["brief"] == "B")
    variables = dict(case["variables"], customer_id="synthetic_" + case["id"])
    products = [dict(record, product_id=f"p{index}") for index, record in enumerate(case["products"], 1)]
    verify = [{"ok": True, "verified": True}] * 2
    if case["id"] == "PUB-03":
        verify = [{"ok": True, "verified": False}] * 3
    if case["id"] == "PUB-12":
        verify = [{"ok": True, "verified": True}, {"ok": True, "verified": False}, {"ok": True, "verified": True}]
    responses = {"verify_identity": verify, "get_account": [{"ok": True, "products": products}],
                 "create_promise": [{"ok": True, "promise_id": "mock_" + str(i)} for i in range(3)],
                 "add_note": [{"ok": True}] * 3, "transfer_to_agent": [{"ok": True}],
                 "schedule_callback": [{"ok": True}], "close_call": [{"ok": True}]}
    if case["id"] == "PUB-10":
        responses["create_promise"] = [{"ok": False}, {"ok": True, "promise_id": "mock_after_retry"}]
    c = Conversation(docs, policy, contract, variables, MockBank(responses))
    c.start()
    if case["id"] == "PUB-02":
        c.receive(Turn(("third_party",)))
        c.say("Tomorrow at 11:00")
        return c
    c.say("yes")
    if case["id"] == "PUB-03":
        for text in ("12 July 1991", "21 August 1992", "21 July 1990"):
            c.say(text)
        return c
    c.say(case["identity"]["date_of_birth"])
    if case["id"] == "PUB-12":
        c.say("4401")
    c.say(case["identity"]["phone_last4"])
    actions = {
        "PUB-01": ["yes", "yes"],
        "PUB-04": ["I can pay on 6 November", "Are you a robot?", "yes", "yes"],
        "PUB-05": ["I lost my job", "Can I get a discount?", "no"],
        "PUB-06": ["The full amount is too much", "I can pay this Friday", "yes"],
        "PUB-07": ["The week after next", "26 October", "yes", "yes"],
        "PUB-08": ["I paid it in the app this morning"],
        "PUB-09": ["I want lower instalments"],
        "PUB-10": ["no", "yes", "yes"],
        "PUB-11": [Turn(("proposal",), date_text="this Friday", all_products=True), "yes"],
        "PUB-12": [Turn(("proposal",), date_text="tomorrow", product_id="p1"), "no", "no", "no", "no", "yes"],
        "PUB-13": [Turn(("proposal",), date_text="30 October", all_products=True), "yes"],
    }
    for action in actions[case["id"]]:
        if isinstance(action, Turn):
            c.receive(action)
        else:
            c.say(action)
    return flush(c)


def local_ledger(c):
    evidence = c.evidence()
    promises = [{key: value for key, value in row.items() if key != "customer_id"} for row in evidence["recorded_promises"]]
    notes, callbacks, transfers = [], [], []
    for action in c.session.bank.calls:
        value = {key: item for key, item in action.payload.items() if key != "customer_id"}
        if action.operation == "add_note":
            notes.append(value)
        elif action.operation == "schedule_callback":
            callbacks.append(value)
        elif action.operation == "transfer_to_agent":
            transfers.append(value)
    return {"outcome": c.outcome["outcome"] if c.outcome else None, "reason": c.outcome["reason"] or None if c.outcome else None,
            "promises": promises, "notes": notes, "callbacks": callbacks, "transfers": transfers}


class PublicLocalReplayTests(unittest.TestCase):
    def test_all_thirteen_hand_translated_local_replays_match_public_ledgers(self):
        cases = json.loads((KIT / "public-test-set.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["id"]):
                c = make_public_replay(case)
                self.assertEqual(local_ledger(c), case["expected"])
                self.assertEqual(c.phase, "ended")
                self.assertTrue(c.outcome["tool_confirmed"])
                self.assertEqual(c.session.bank.calls[-1].operation, "close_call")
                plan = conversation_plan(c.semantics)
                transitions = {state["id"]: state["next"] for state in plan["states"]}
                for event in c.trace:
                    if event["event"] == "transition":
                        self.assertIn(event["phase"], transitions[event["previous"]])
                self.assertEqual(sum(call.operation == "close_call" for call in c.session.bank.calls), 1)
                for message in c.messages:
                    if "A.17" in message["rules"]:
                        self.assertGreaterEqual(message["bank_call_count"], 3)
                for message in c.messages:
                    if "A.37" in message["rules"] and "is recorded" in message["text"]:
                        prior = c.session.events[:message["bank_call_count"]]
                        self.assertTrue(any(event["operation"] == "create_promise" and event["business_result"] == "succeeded" for event in prior))
