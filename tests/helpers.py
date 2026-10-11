from pathlib import Path

from builder.api_contract import BankContract
from builder.extract import extract_policy
from builder.ingest import collect_sources
from builder.runtime import CallState, GuardedSession, MockBank

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "data/freya-hackathon/kit-v3/build-the-builder"


def fixture(multi=False):
    contract = BankContract.from_path(KIT / "tools/openapi.json")
    paths = [KIT / "briefs/brief-a.md"] + ([KIT / "briefs/brief-b.md"] if multi else [])
    docs = collect_sources(paths)
    return docs, extract_policy(docs, contract), contract


def account(identifier="p1", *, days_late=6, past_due=420, floor=126):
    return {"product_id": identifier, "type": "credit_card", "card_last4": "4821",
            "past_due": past_due, "days_late": days_late, "minimum_payment": floor}


def session(*, multi=False, products=None, responses=None, verified=True):
    docs, policy, contract = fixture(multi)
    scripted = {"get_account": [{"ok": True, "products": products or [account()]}],
                "create_promise": [{"ok": True, "promise_id": "mock_promise"}],
                "add_note": [{"ok": True}] * 3, "close_call": [{"ok": True}],
                "transfer_to_agent": [{"ok": True}],
                "verify_identity": [{"ok": True, "verified": True}, {"ok": True, "verified": True}]}
    scripted.update(responses or {})
    state = CallState("synthetic_local_customer", "2026-10-14T11:00", tuple(policy.facts["identity_order"].value))
    if verified:
        state.verified = set(state.required_identity_fields)
    return GuardedSession(contract, policy, state, MockBank(scripted))
