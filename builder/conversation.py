"""Version-bound practice conversation state machine. All tool execution is MockBank-only."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .calendar import iso_date
from .models import FoundationError
from .runtime import GuardedSession, money
from .semantics import compile_practice_policy
from .turns import Turn, parse_turn, resolve_date

PHASES = ("new", "person", "third_party", "identity", "offer", "negotiate", "exact_date",
          "adjustment", "support", "human_help", "readback", "recording", "post_record", "ended", "fault")


@dataclass
class Negotiation:
    amount: str
    date: str
    attempts: int = 0
    floor_offered: bool = False
    full_too_much: bool = False
    date_clarifications: int = 0
    hardship: bool = False
    support_offered: bool = False
    offers: set[tuple[str, str]] = field(default_factory=set)
    last_offer: tuple[str, str] | None = None


class Conversation:
    """Structured turns are authoritative local inputs; say() is a bounded text adapter.

    Each advance() performs at most one promise write. Customer interruptions can therefore
    arrive between writes. Final notes/close form the existing guarded finalization batch.
    """
    def __init__(self, documents, policy, contract, variables, bank):
        semantics = compile_practice_policy(documents, policy, contract)
        if not semantics["complete_for_supplied_practice"]:
            raise FoundationError("Conversation needs the reviewed practice source versions; changed policy is not silently accepted")
        for name in ("customer_id", "first_name", "last_name", "title", "now"):
            if not isinstance(variables.get(name), str) or not variables[name].strip():
                raise FoundationError(f"Explicit nonempty call variable required: {name}")
        from .runtime import CallState
        datetime.fromisoformat(variables["now"])
        state = CallState(variables["customer_id"], variables["now"], tuple(policy.facts["identity_order"].value))
        self.session = GuardedSession(contract, policy, state, bank)
        self.variables = dict(variables)
        self.semantics = semantics
        self.phase = "new"
        self.messages = []
        self.trace = []
        self.negotiations: dict[str, Negotiation] = {}
        self.pending: dict[str, dict] = {}
        self.active = ""
        self.person_asks = self.callback_invalid = self.identity_declines = self.dont_talk = 0
        self.human_offered = False
        self.resume_phase = ""
        self.confirmation_token = ""
        self.write_queue: list[str] = []
        self.outcome = None
        self.fault = ""
        self.preidentity_hardship = False
        self.multi = bool(policy.facts["notes_enabled"].value)

    def _log(self, event, rules=(), **details):
        self.trace.append({"step": len(self.trace), "event": event, "phase": self.phase,
                           "rules": list(rules), "bank_call_count": len(self.session.bank.calls), **details})

    def _say(self, text, *rules):
        if not self.session.state.identity_verified:
            if any(term in text.lower() for term in ("collections", "debt", "payment", "credit card", "personal loan", "car loan")):
                raise FoundationError("Unverified speech would disclose protected collection information")
        self.messages.append({"speaker": "assistant", "text": text, "phase": self.phase,
                              "rules": list(rules), "bank_call_count": len(self.session.bank.calls)})
        self._log("speech", rules, message_index=len(self.messages) - 1)

    def _move(self, phase, *rules):
        if phase not in PHASES:
            raise FoundationError("Unknown conversation phase")
        previous = self.phase
        self.phase = phase
        self._log("transition", rules, previous=previous)

    def start(self):
        if self.phase != "new":
            raise FoundationError("Call was already started")
        self._say("I am Freya Bank's digital assistant, and this call is recorded.", "A.1")
        self._move("person", "A.2")
        self._ask_person()
        return self.snapshot()

    def _ask_person(self):
        self.person_asks += 1
        self._say(f"Am I speaking with {self.variables['title']} {self.variables['last_name']}?", "A.2", "A.3")

    def _identity_question(self, *, month_word=False):
        remaining = [name for name in self.session.state.required_identity_fields if name not in self.session.state.verified]
        if not remaining:
            return self._load_account()
        if remaining[0] == "date_of_birth":
            self._say("Please say your date of birth with the month as a word." if month_word else "What is your date of birth?", "A.11", *( ["A.13"] if month_word else []))
        else:
            self._say("What are the last four digits of the phone number on file?", "A.11")

    def say(self, text):
        remaining = [name for name in self.session.state.required_identity_fields if name not in self.session.state.verified]
        turn = parse_turn(text, phase=self.phase, now=self.variables["now"], first_name=self.variables["first_name"],
                          last_name=self.variables["last_name"], identity_field=remaining[0] if remaining else "",
                          token=self.confirmation_token)
        return self.receive(turn)

    def receive(self, turn: Turn):
        if self.phase in {"new", "ended", "fault"}:
            raise FoundationError("Customer turn is not allowed in the current phase")
        if not isinstance(turn, Turn):
            raise FoundationError("Use a validated Turn or the bounded say() adapter")
        self._log("customer_turn", intents=list(turn.intents))
        intents = set(turn.intents)
        # A correction, refusal or interruption revokes consent before any write opportunity.
        if self.phase in {"readback", "recording"} and intents != {"yes"} and (intents & {"proposal", "no", "hardship", "dont_talk", "restructuring", "human", "cancel"}):
            self.session.state.approved_digest = ""
        if "wrong_number" in intents:
            return self._close("not_reached", "wrong_number", "A.8") if not self.session.state.identity_verified else self._unknown_question()
        if "deceased" in intents:
            if self.session.state.identity_verified:
                return self._unknown_question()
            self._say("I am sorry for your loss.", "A.9")
            return self._close("not_reached", "deceased", "A.9")
        # Third parties never enter security or negotiation, even when they offer money.
        if self.phase == "third_party" or ("third_party" in intents and not self.session.state.identity_verified):
            if self.phase != "third_party":
                self._move("third_party", "A.5", "A.10")
                self._say("When can the customer be reached?", "A.5")
                if "callback" not in intents:
                    return self.snapshot()
            return self._third_party(turn)
        for intent, response, rule in (("robot", "I am Freya Bank's digital assistant.", "A.4"),
                                      ("scam", "You can verify this call by phoning Freya Bank's customer service number.", "A.43"),
                                      ("discount", "I cannot offer a discount, waiver or new terms.", "A.33"),
                                      ("question", "I do not have that information.", "A.44")):
            if intent in intents:
                self._say(response, rule)
        if "hardship" in intents:
            return self._hardship(turn)
        if "human" in intents:
            if self.human_offered or self.phase == "human_help":
                return self._transfer("general", "human", "A.42")
            self.human_offered = True
            self.resume_phase = self.phase
            self._move("human_help", "A.42")
            self._say("Can I help you first?", "A.42")
            return self.snapshot()
        if "dont_talk" in intents:
            self.dont_talk += 1
            if self.dont_talk >= 2:
                if not self.session.state.identity_verified:
                    return self._close("not_verified", "declined", "A.45")
                return self._stop_all("refused", "A.45")
            if self.phase == "identity":
                return self._identity_decline()
            self._say("I understand. Please let me know if you would like to continue.", "A.45")
            return self.snapshot()
        if "restructuring" in intents:
            return self._transfer("restructuring", "restructuring", "A.41", *( ["B.10"] if self.multi else []))
        if self.phase == "human_help":
            if "no" in intents:
                return self._transfer("general", "human", "A.42")
            self._move(self.resume_phase, "A.42")
            if self.phase == "person":
                if self.person_asks >= 2:
                    return self._close("not_reached", "unclear", "A.3")
                self._ask_person()
            elif self.phase == "identity":
                self._identity_question()
            else:
                self._say("Please continue with the question we were discussing.", "A.42")
            return self.snapshot()
        if self.phase == "support":
            if intents & {"yes", "support_accept"} and not intents & {"proposal", "vague_date"}:
                return self._transfer("support", "support", "A.32")
            if intents & {"no", "support_decline"} and not intents & {"proposal", "vague_date"}:
                if self.preidentity_hardship:
                    return self._close("no_promise", "hardship", "A.32")
                return self._resolve_product("hardship", "A.32")
            if not intents & {"proposal", "vague_date"}:
                self._say("Please say whether you want to speak with customer support.", "A.32")
                return self.snapshot()
            self._move("negotiate", "A.32")
            if self.preidentity_hardship:
                self._move(self.resume_phase, "A.32", "A.10", "A.16")
                if self.phase == "identity":
                    self._identity_question()
                elif self.person_asks < 2:
                    self._ask_person()
                else:
                    return self._close("not_reached", "unclear", "A.3")
                return self.snapshot()
        if self.phase == "person":
            if intents == {"yes"}:
                self._move("identity", "A.3", "A.11")
                self._identity_question()
                return self.snapshot()
            if self.person_asks >= 2:
                return self._close("not_reached", "unclear", "A.3")
            if "purpose" in intents:
                self._say("I am Freya Bank's digital assistant.", "A.1", "A.3")
            self._ask_person()
            return self.snapshot()
        if self.phase == "identity":
            if intents & {"decline", "purpose", "no"}:
                return self._identity_decline()
            if "identity_answer" in intents:
                remaining = [name for name in self.session.state.required_identity_fields if name not in self.session.state.verified]
                before = self.session.state.identity_wrong
                before_calls = len(self.session.bank.calls)
                try:
                    status = self.session.verify_answer(remaining[0], turn.answer)
                except FoundationError as exc:
                    if len(self.session.bank.calls) > before_calls:
                        return self._fault(str(exc))
                    self._identity_question()
                    return self.snapshot()
                if status != "succeeded":
                    return self._fault("Identity tool did not return a successful result; the brief supplies no verified fallback.")
                if self.session.state.identity_wrong >= self.session.policy.facts["identity_max_wrong"].value:
                    self._say("Please use the mobile app or visit a branch.", "A.14")
                    return self._close("not_verified", "failed", "A.12", "A.14")
                if self.session.state.identity_verified:
                    return self._load_account()
                # Successful, unchanged, still-unverified DOB is the validated swap-hint branch.
                hint = before == self.session.state.identity_wrong and remaining[0] not in self.session.state.verified
                self._identity_question(month_word=hint and remaining[0] == "date_of_birth")
                return self.snapshot()
            self._identity_question()
            return self.snapshot()
        if intents & {"change_recorded", "cancel"} and (turn.product_id and self.session.product(turn.product_id).recorded or self.phase == "post_record"):
            self._say("A recorded promise cannot be changed or cancelled in this call. You can change it in the mobile app.", "A.36")
            return self.snapshot()
        if intents & {"paid", "dispute"}:
            if turn.product_id:
                self.active = turn.product_id
            if self.session.product(self.active).recorded:
                self._say("You can review the recorded promise in the mobile app.", "A.36")
                return self.snapshot()
            self.session.state.approved_digest = ""
            reason = "dispute" if "dispute" in intents else "paid"
            if reason == "paid":
                self._say("Payments can take one business day to appear.", "A.39")
            else:
                self._say("I understand that you dispute this product.", "A.40")
            return self._resolve_product(reason, "A.39" if reason == "paid" else "A.40", *( ["B.9"] if self.multi else []))
        if self.phase in {"recording", "post_record"}:
            if "proposal" in intents:
                identifier = turn.product_id or self.active
                if identifier and self.session.product(identifier).recorded:
                    self._say("You can change the recorded promise in the mobile app.", "A.36")
                    return self._readback() if self.pending else self.snapshot()
                self._move("negotiate", "A.35")
                if identifier:
                    self.active = identifier
                return self._proposal(turn)
            if intents & {"no", "cancel"}:
                return self._stop_all("refused", "A.35")
            if intents & {"robot", "scam", "question", "discount"}:
                return self.snapshot()
            return self.snapshot()
        if self.phase == "readback":
            if intents == {"yes"}:
                if turn.consent_token != self.confirmation_token:
                    self._say("Please confirm the current read-back.", "A.34", "A.35")
                    return self.snapshot()
                self.session.confirm(self.confirmation_token, True)
                self.write_queue = [promise.product_id for promise in self.session.state.draft]
                self._move("recording", "A.34", *( ["B.7"] if self.multi else []))
                return self.snapshot()
            if "proposal" in intents or "vague_date" in intents or "too_much" in intents:
                self._move("negotiate", "A.35")
                if turn.product_id:
                    self.active = turn.product_id
            elif "no" in intents:
                identifier = turn.product_id or self.active
                self.pending.pop(identifier, None)
                self.active = identifier
                self._move("negotiate", "A.35")
                return self._persuade()
            else:
                self._say("Do you confirm?", "A.34")
                return self.snapshot()
        if "proposal" in intents or "vague_date" in intents:
            return self._proposal(turn)
        if "too_much" in intents:
            return self._offer_floor()
        if self.phase == "exact_date":
            return self._propose_deadline()
        if self.phase in {"offer", "negotiate", "adjustment"}:
            if "yes" in intents:
                if self.phase == "offer" and self.multi:
                    return self._proposal(Turn(("proposal",), date_text=self.variables["now"][:10], all_products=True))
                return self._accept_active()
            if self.session.state.products and self.negotiations[self.active].hardship:
                return self._resolve_product("hardship", "A.32")
            if intents & {"question", "robot", "scam", "discount", "purpose"}:
                return self._unknown_question() if "purpose" in intents else self.snapshot()
            return self._persuade()
        return self.snapshot()

    def _third_party(self, turn):
        if "human" in turn.intents:
            if self.human_offered:
                return self._transfer("general", "human", "A.42")
            self.human_offered = True
            self.resume_phase = "third_party"
            self._move("human_help", "A.42")
            self._say("Can I help you first?", "A.42")
            return self.snapshot()
        if "callback" in turn.intents:
            try:
                day = resolve_date(turn.date_text, self.variables["now"])
                valid = self.session.calendar.callback_allowed(day, turn.time, self.variables["now"], self.session.policy.facts["callback_hours"].value)
            except (FoundationError, ValueError):
                valid = False
            if valid:
                try:
                    status = self.session.schedule_callback(day, turn.time)
                except FoundationError as exc:
                    return self._fault(str(exc))
                if status != "succeeded":
                    return self._fault("Callback was not confirmed by the tool; callback outcome must not be invented.")
                return self._close("not_reached", "callback", "A.6")
            self.callback_invalid += 1
            if self.callback_invalid >= 2:
                return self._close("not_reached", "no_callback", "A.7")
            self._say("Please suggest another time today or the next business day, from 10:00 to 19:00 and later than now.", "A.6", "A.7")
        elif "no" in turn.intents:
            return self._close("not_reached", "no_callback", "A.7")
        else:
            self._say("When can the customer be reached?", "A.5", "A.10")
        return self.snapshot()

    def _identity_decline(self):
        self.identity_declines += 1
        if self.identity_declines >= 2:
            return self._close("not_verified", "declined", "A.15")
        self._say("These questions protect your account.", "A.15")
        self._identity_question()
        return self.snapshot()

    def _load_account(self):
        try:
            status = self.session.fetch_products()
        except FoundationError as exc:
            return self._fault(str(exc))
        if status != "succeeded":
            return self._fault("Account tool did not provide a confirmed snapshot.")
        try:
            for product in self.session.state.products:
                self._label(product.product_id)
        except FoundationError as exc:
            return self._fault(str(exc))
        self._log("account_loaded", ("A.16", "B.1") if self.multi else ("A.16",))
        for product in self.session.state.products:
            self.negotiations[product.product_id] = Negotiation(str(product.past_due), self.variables["now"][:10])
        self.active = self.session.state.products[0].product_id
        self._say("I am calling from the collections team about a late payment.", "A.17")
        for product in self.session.state.products:
            self._say(f"Your {self._label(product.product_id)} has a past-due amount of {product.past_due:.2f} and is {product.details['days_late']} days late.", "A.17", *( ["B.2"] if self.multi else []))
        self._move("offer", "A.24", "A.28", *( ["B.4"] if self.multi else []))
        if self.multi:
            self._say("Can you pay all of these past-due amounts today?", "A.24", "B.4")
        else:
            self._say(f"Can you make this payment of {self.session.product(self.active).past_due:.2f} today?", "A.24", "A.28")
        return self.snapshot()

    def _label(self, identifier):
        record = self.session.product(identifier).details
        if record["type"] == "credit_card":
            suffix = record.get("card_last4", "")
            if not isinstance(suffix, str) or not suffix.isdigit() or len(suffix) != 4:
                raise FoundationError("Credit-card disclosure requires the verified last four digits")
            return f"credit card ending {suffix}"
        return record["type"].replace("_", " ")

    def _proposal(self, turn):
        if turn.product_id:
            product = self.session.product(turn.product_id)
            if product.recorded:
                self._say("You can change the recorded promise in the mobile app.", "A.36")
                return self.snapshot()
            self.active = turn.product_id
        targets = [product.product_id for product in self.session.state.products if not product.recorded and not product.note_reason] if turn.all_products else [self.active]
        if "vague_date" in turn.intents and not turn.date_text:
            negotiation = self.negotiations[self.active]
            if negotiation.date_clarifications == 0:
                negotiation.date_clarifications += 1
                self._move("exact_date", "A.21")
                self._say("What exact day can you make this payment?", "A.21")
                return self.snapshot()
            return self._propose_deadline()
        try:
            proposed_date = resolve_date(turn.date_text, self.variables["now"]) if turn.date_text else ""
            proposed_amount = money(turn.amount) if turn.amount else None
        except (FoundationError, ValueError):
            self._say("Please give a valid calendar day and amount.", "A.19", "A.27")
            return self.snapshot()
        adjusted = []
        for identifier in targets:
            product = self.session.product(identifier)
            negotiation = self.negotiations[identifier]
            candidate = proposed_date or negotiation.date
            amount = proposed_amount if proposed_amount is not None else money(negotiation.amount)
            if amount < product.floor:
                self.active = identifier
                return self._offer_floor(below=True)
            if amount > product.past_due:
                self._say(f"The full past-due amount for {self._label(identifier)} is {product.past_due:.2f}; I cannot record a higher amount.", "A.27", "A.44")
                return self.snapshot()
            if candidate > product.deadline:
                candidate = product.deadline
                adjusted.append((identifier, "deadline"))
            elif not self.session.calendar.is_business_day(iso_date(candidate)):
                candidate = min(self.session.calendar.on_or_after(iso_date(candidate)).isoformat(), product.deadline)
                adjusted.append((identifier, "business_day"))
            today = self.variables["now"][:10]
            if candidate < today:
                candidate = self.session.calendar.on_or_after(iso_date(today)).isoformat()
                adjusted.append((identifier, "past_date"))
            negotiation.amount, negotiation.date = str(amount), candidate
        self._log("proposal_validated", ("A.19", "A.25", "A.27", "A.29"), products=targets)
        if adjusted and not (self.multi and turn.all_products):
            identifier, reason = adjusted[0]
            self.active = identifier
            negotiation = self.negotiations[identifier]
            self._move("adjustment", "A.20" if reason == "business_day" else "A.23")
            wording = "The latest date I can record" if reason == "deadline" else "The available business day I can offer"
            self._say(f"{wording} is {negotiation.date}. Can you pay {money(negotiation.amount):.2f} on that date?", "A.19", "A.20", "A.23")
            return self.snapshot()
        for identifier in targets:
            self.pending[identifier] = self._draft_for(identifier)
        return self._next_product()

    def _draft_for(self, identifier):
        negotiation = self.negotiations[identifier]
        return {"product_id": identifier, "amount": float(money(negotiation.amount)), "date": negotiation.date}

    def _offer_floor(self, below=False):
        negotiation = self.negotiations[self.active]
        product = self.session.product(self.active)
        negotiation.full_too_much = True
        negotiation.amount = str(product.floor)
        if not self.session.calendar.is_business_day(iso_date(negotiation.date)):
            negotiation.date = self.session.calendar.on_or_after(iso_date(negotiation.date)).isoformat()
        if negotiation.floor_offered:
            self._say("That amount cannot be recorded. Do you accept the amount already offered?", "A.26", "A.27")
        else:
            negotiation.floor_offered = True
            self._say(f"{'That amount is below the minimum. ' if below else ''}I can offer {product.floor:.2f} for {self._label(self.active)} on {negotiation.date}. The rest stays overdue. Do you accept?", "A.25", "A.26", "A.27")
        self._move("adjustment", "A.26", "A.27")
        return self.snapshot()

    def _propose_deadline(self):
        negotiation = self.negotiations[self.active]
        negotiation.date = self.session.product(self.active).deadline
        self._move("adjustment", "A.21")
        self._say(f"I can propose {money(negotiation.amount):.2f} on {negotiation.date}. Do you accept?", "A.21")
        return self.snapshot()

    def _accept_active(self):
        negotiation = self.negotiations[self.active]
        # Today may be a weekend/holiday even when customer says yes to the initial question.
        if not self.session.calendar.is_business_day(iso_date(negotiation.date)):
            return self._proposal(Turn(("proposal",), date_text=negotiation.date))
        self.pending[self.active] = self._draft_for(self.active)
        return self._next_product()

    def _last_business_day(self):
        today = iso_date(self.variables["now"][:10])
        monday = today - timedelta(days=today.weekday())
        for week in (0, 1):
            days = [monday + timedelta(days=week * 7 + day) for day in range(7)]
            valid = [day for day in days if self.session.calendar.is_business_day(day)]
            if valid and valid[-1] > today:
                return valid[-1].isoformat()
        raise FoundationError("No eligible business day in the two source-defined calendar weeks")

    def _persuade(self):
        product = self.session.product(self.active)
        negotiation = self.negotiations[self.active]
        if negotiation.hardship:
            return self._resolve_product("hardship", "A.32")
        bands = self.session.policy.facts["bands"].value
        band = next(band for band in bands if band["min_days_late"] <= product.details["days_late"] <= band["max_days_late"])
        limit = band["persuasion_attempts"]
        negotiation.offers.add((f"{money(negotiation.amount):.2f}", negotiation.date))
        if negotiation.attempts >= limit:
            return self._resolve_product("refused", "A.31", *( ["B.5"] if self.multi else []))
        negotiation.attempts += 1
        if negotiation.attempts == 1:
            negotiation.amount = str(product.floor if negotiation.full_too_much else product.past_due)
            negotiation.date = min(self._last_business_day(), product.deadline)
        else:
            negotiation.amount, negotiation.date = str(product.floor), product.deadline
        offer = (f"{money(negotiation.amount):.2f}", negotiation.date)
        if offer in negotiation.offers:
            if negotiation.attempts < limit:
                return self._persuade()
            return self._resolve_product("refused", "A.31")
        negotiation.offers.add(offer)
        negotiation.last_offer = offer
        self._move("negotiate", "A.30")
        self._say(f"Can you pay {money(negotiation.amount):.2f} for {self._label(self.active)} on {negotiation.date}?", "A.30", *( ["B.4", "B.5"] if self.multi else []))
        return self.snapshot()

    def _hardship(self, turn):
        if not self.session.state.identity_verified:
            if not self.preidentity_hardship:
                self.resume_phase = self.phase
                self._say("I am sorry you are going through this.", "A.32")
                self.preidentity_hardship = True
                self._move("support", "A.32")
                self._say("Would you like me to connect you with customer support?", "A.32")
            return self.snapshot()
        if turn.product_id:
            self.active = turn.product_id
        negotiation = self.negotiations[self.active]
        if not negotiation.hardship:
            self._say("I am sorry you are going through this.", "A.32")
        negotiation.hardship = True
        if "proposal" in turn.intents or turn.date_text or turn.amount:
            if self.session.product(self.active).recorded:
                self._say("You can change the recorded promise in the mobile app.", "A.36")
                return self.snapshot()
            self._move("negotiate", "A.32")
            return self._proposal(turn)
        if not negotiation.support_offered:
            negotiation.support_offered = True
            self._move("support", "A.32")
            self._say("Would you like me to connect you with customer support?", "A.32")
        return self.snapshot()

    def _resolve_product(self, reason, *rules):
        self.pending.pop(self.active, None)
        if not self.session.product(self.active).recorded:
            self.session.defer_note(self.active, reason)
        self._log("product_resolved", rules, product_id=self.active, reason=reason)
        return self._next_product()

    def _next_product(self):
        unresolved = [product.product_id for product in self.session.state.products
                      if not product.recorded and not product.note_reason and product.product_id not in self.pending]
        if unresolved:
            self.active = unresolved[0]
            self._move("negotiate", "B.4" if self.multi else "A.30")
            if self.multi:
                return self._persuade()
            return self.snapshot()
        if self.pending:
            return self._readback()
        outcome, reason = self.session.outcome_for_products()
        return self._close(outcome, reason, "A.46", *( ["B.closing"] if self.multi else []))

    def _readback(self):
        ordered = [self.pending[product.product_id] for product in self.session.state.products if product.product_id in self.pending and not product.recorded]
        self.confirmation_token = self.session.set_draft(ordered)
        self._move("readback", "A.34", "A.35", *( ["B.7"] if self.multi else []))
        lines = [f"{self._label(item['product_id'])}: {money(item['amount']):.2f} on {item['date']}" for item in ordered]
        self._say("Please confirm these promises: " + "; ".join(lines) + ". Do you confirm?", "A.34", *( ["B.6", "B.7"] if self.multi else []))
        return self.snapshot()

    def _stop_all(self, reason, *rules):
        self.pending.clear()
        self.session.state.approved_digest = ""
        for product in self.session.state.products:
            if not product.recorded and not product.note_reason:
                self.session.defer_note(product.product_id, reason)
        outcome, final_reason = self.session.outcome_for_products()
        return self._close(outcome, final_reason, *rules)

    def _transfer(self, queue, reason, *rules):
        self.pending.clear()
        self.write_queue.clear()
        try:
            status = self.session.transfer(queue, reason)
        except FoundationError as exc:
            return self._fault(str(exc))
        self._log("transfer", rules, queue=queue, status=status)
        if status != "succeeded":
            return self._fault("Transfer was not confirmed; no successful transfer outcome is claimed.")
        self._say("I am connecting you with the requested team.", *rules)
        return self._close("transferred", reason, *rules, *( ["B.10"] if self.multi else []))

    def _unknown_question(self):
        self._say("I do not have that information.", "A.44")
        return self.snapshot()

    def _close(self, outcome, reason, *rules):
        self._say("Thank you for your time. Goodbye.", "A.47")
        try:
            status = self.session.finalize(outcome, reason, goodbye_spoken=True)
        except FoundationError as exc:
            return self._fault(str(exc))
        self.outcome = {"outcome": outcome, "reason": reason, "tool_confirmed": status == "succeeded"}
        self._log("close", (*rules, "A.46"), status=status, outcome=outcome, reason=reason)
        self._move("ended", "A.46", "A.47")
        return self.snapshot()

    def _fault(self, detail):
        self.fault = detail
        self._move("fault")
        self._log("quarantined_uncertain_result", detail=detail)
        return self.snapshot()

    def advance(self):
        if self.phase == "post_record":
            outcome, reason = self.session.outcome_for_products()
            return self._close(outcome, reason, "A.46", *( ["B.closing"] if self.multi else []))
        if self.phase != "recording" or not self.write_queue:
            raise FoundationError("No internal promise-write step is pending")
        identifier = self.write_queue[0]
        try:
            status = self.session.record_promise(identifier)
        except FoundationError as exc:
            return self._fault(str(exc))
        self._log("promise_write", ("A.37",), product_id=identifier, status=status)
        if status == "unknown":
            return self._fault("Promise write result is ambiguous; no retry or recorded announcement.")
        product = self.session.product(identifier)
        if status == "failed" and product.attempts < self.session.policy.facts["promise_max_attempts"].value:
            return self.snapshot()  # Next advance is the single explicit retry.
        self.write_queue.pop(0)
        self.pending.pop(identifier, None)
        if status == "succeeded":
            self._say(f"The promise for {self._label(identifier)} is recorded.", "A.37")
            self._say("You will receive a text message confirming this promise.", "A.38")
        else:
            self._say(f"I could not record the promise for {self._label(identifier)}.", "A.37")
            self.session.defer_note(identifier, "service_error")
            self._log("product_resolved", ("A.37", "B.11") if self.multi else ("A.37",), product_id=identifier, reason="service_error")
        if not self.write_queue:
            self._move("post_record", "A.46")
        return self.snapshot()

    def snapshot(self):
        return {"phase": self.phase, "active_product_id": self.active, "identity_verified": self.session.state.identity_verified,
                "readback_token": self.confirmation_token if self.phase == "readback" else "",
                "pending_write_count": len(self.write_queue), "outcome": self.outcome,
                "fault": self.fault, "api_key_read": False, "network_calls": 0,
                "last_reply": self.messages[-1]["text"] if self.messages else ""}

    def evidence(self):
        return {"snapshot": self.snapshot(), "messages": self.messages, "trace": self.trace,
                "bank_calls": [asdict(call) for call in self.session.bank.calls], "business_results": self.session.events,
                "recorded_promises": [product.recorded.payload(self.session.state.customer_id) for product in self.session.state.products if product.recorded],
                "negotiations": {key: {"attempts": value.attempts, "floor_offered": value.floor_offered,
                    "hardship": value.hardship, "support_offered": value.support_offered} for key, value in self.negotiations.items()}}


def conversation_plan(semantics):
    transitions = {
        "new": ["person"], "person": ["identity", "third_party", "support", "human_help", "ended", "fault"],
        "third_party": ["third_party", "human_help", "ended", "fault"], "identity": ["identity", "offer", "support", "human_help", "ended", "fault"],
        "offer": ["negotiate", "exact_date", "adjustment", "readback", "support", "human_help", "ended", "fault"],
        "negotiate": ["negotiate", "exact_date", "adjustment", "readback", "support", "human_help", "ended", "fault"],
        "exact_date": ["adjustment", "readback", "negotiate", "support", "human_help", "ended", "fault"],
        "adjustment": ["adjustment", "negotiate", "exact_date", "readback", "support", "human_help", "ended", "fault"],
        "support": ["support", "person", "identity", "negotiate", "adjustment", "exact_date", "readback", "ended", "fault"],
        "human_help": ["person", "third_party", "identity", "offer", "negotiate", "exact_date", "adjustment", "support", "readback", "recording", "post_record", "ended", "fault"],
        "readback": ["readback", "recording", "negotiate", "adjustment", "exact_date", "support", "human_help", "ended", "fault"],
        "recording": ["recording", "post_record", "readback", "negotiate", "support", "human_help", "ended", "fault"],
        "post_record": ["post_record", "support", "human_help", "ended", "fault"], "ended": [], "fault": []}
    return {"kind": "executable_local_conversation_plan", "is_freya_export": False, "publish_allowed": False,
            "source_version_admitted": semantics["source_version_admitted"], "initial": "new",
            "states": [{"id": phase, "next": transitions[phase], "terminal": phase in {"ended", "fault"},
                        "status": "implemented_for_reviewed_practice" if semantics["complete_for_supplied_practice"] else "unsupported_source_version"} for phase in PHASES],
            "entry_point": "builder.conversation.Conversation", "methods": ["start", "say", "receive", "advance", "snapshot", "evidence"],
            "transport": "MockBank only", "internal_writes_per_advance": 1,
            "invariants": ["both identity factors before account/disclosure", "only explicit current readback consent",
                "product-local date/amount/attempt budgets", "recorded promises immutable", "notes deferred until finalization",
                "zero notes on any transfer", "confirmed transfer takes outcome precedence", "one last close after goodbye",
                "ambiguous tool results quarantine without invented outcome"],
            "limits": semantics.get("remaining_limits", [semantics.get("reason", "Unreviewed input")])}
