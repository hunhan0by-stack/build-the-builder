from builder.conversation import Conversation
from builder.runtime import MockBank
from builder.turns import Turn
from tests.helpers import account, fixture


def conversation(*, multi=False, products=None, responses=None, now="2026-10-14T11:00"):
    docs, policy, contract = fixture(multi)
    scripted = {"get_account": [{"ok": True, "products": products or [account()]}],
                "verify_identity": [{"ok": True, "verified": True}, {"ok": True, "verified": True}],
                "create_promise": [{"ok": True, "promise_id": f"mock_{index}"} for index in range(6)],
                "add_note": [{"ok": True} for _ in range(3)], "close_call": [{"ok": True}],
                "schedule_callback": [{"ok": True}], "transfer_to_agent": [{"ok": True}]}
    scripted.update(responses or {})
    variables = {"customer_id": "local_synthetic", "first_name": "Daniel", "last_name": "Reyes", "title": "Mr", "now": now}
    result = Conversation(docs, policy, contract, variables, MockBank(scripted))
    result.start()
    return result


def verified(conversation):
    conversation.say("yes")
    conversation.say("14 March 1988")
    conversation.say("4471")
    return conversation


def flush(conversation):
    for _ in range(10):
        if conversation.phase not in {"recording", "post_record"}:
            return conversation
        conversation.advance()
    raise AssertionError("Offline internal steps did not terminate")


def confirm(conversation):
    return conversation.receive(Turn(("yes",), consent_token=conversation.confirmation_token))
