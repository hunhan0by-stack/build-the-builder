"""A bounded offline English adapter. Unknown speech stays unknown; it never grants consent."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from .calendar import iso_date, weekday_in_calendar_week
from .models import FoundationError

INTENTS = frozenset({"yes", "no", "unknown", "third_party", "wrong_number", "deceased", "callback",
                    "identity_answer", "decline", "purpose", "robot", "scam", "question", "dont_talk",
                    "proposal", "vague_date", "too_much", "hardship", "support_accept", "support_decline",
                    "paid", "dispute", "restructuring", "human", "discount", "change_recorded", "cancel"})
MONTHS = {name: index for index, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
WEEKDAYS = {name: index for index, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
YES = {"yes", "that's me", "that is me", "speaking", "i confirm", "confirmed", "yes i confirm", "i agree", "agreed"}
NO = {"no", "no thanks", "no thank you", "i refuse", "cannot pay", "can't pay", "i cannot pay", "i can't pay"}


@dataclass(frozen=True)
class Turn:
    intents: tuple[str, ...] = ("unknown",)
    date_text: str = ""
    amount: str = ""
    answer: str = ""
    time: str = ""
    product_id: str = ""
    all_products: bool = False
    consent_token: str = ""

    def __post_init__(self):
        if not isinstance(self.intents, (list, tuple)) or any(not isinstance(item, str) for item in self.intents):
            raise FoundationError("Conversation intents must be a list or tuple of strings")
        if not self.intents or not set(self.intents) <= INTENTS or len(set(self.intents)) != len(self.intents):
            raise FoundationError("Unknown/duplicate conversation intent")
        object.__setattr__(self, "intents", tuple(self.intents))
        for value in (self.date_text, self.amount, self.answer, self.time, self.product_id, self.consent_token):
            if not isinstance(value, str):
                raise FoundationError("Turn values must be explicit strings")
        if type(self.all_products) is not bool:
            raise FoundationError("all_products must be a boolean")
        if {"yes", "no"} <= set(self.intents):
            raise FoundationError("A contradictory yes/no is not a clear affirmative")


def resolve_date(text: str, now: str, *, dob=False):
    value = text.strip().lower()
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if iso:
        return iso_date(iso[0]).isoformat()
    numeric = re.search(r"\b(\d{1,2})[/.\-\s]+(\d{1,2})[/.\-\s]+(\d{4})\b", value)
    if numeric:
        return date(int(numeric[3]), int(numeric[2]), int(numeric[1])).isoformat()
    current = datetime.fromisoformat(now).date()
    for month_name, month in MONTHS.items():
        match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{month_name}(?:\s+(\d{{4}}))?\b", value)
        reverse = re.search(rf"\b{month_name}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", value)
        match = match or reverse
        if match:
            if dob and not match[2]:
                raise FoundationError("Birth year must be explicit")
            return date(int(match[2]) if match[2] else current.year, month, int(match[1])).isoformat()
    if dob:
        raise FoundationError("Birth date needs an explicit full calendar date")
    if re.search(r"\btomorrow\b", value):
        return (current + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", value):
        return current.isoformat()
    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\bnext {name}\b", value):
            return weekday_in_calendar_week(now, weekday, True).isoformat()
        if re.search(rf"\bthis {name}\b", value):
            return weekday_in_calendar_week(now, weekday, False).isoformat()
    raise FoundationError("Date expression needs clarification; no guessed date")


def parse_turn(text, *, phase, now, first_name, last_name, identity_field="", token=""):
    if not isinstance(text, str) or len(text) > 4000:
        raise FoundationError("Customer text must be a bounded string")
    original = text.strip()
    value = re.sub(r"[.!?]+$", "", original.lower()).strip().replace("’", "'")
    intents = []
    # Specific third-party and historical-payoff meanings precede generic payment words.
    if re.search(r"wrong number|number (?:does not|doesn't) belong|not (?:his|her|their) number", value):
        intents.append("wrong_number")
    if re.search(r"(?:customer|he|she) (?:has died|is dead|passed away)", value):
        intents.append("deceased")
    if re.search(r"(?:my|his|her) (?:husband|wife|son|daughter|partner)|i'm (?:his|her) |i am (?:his|her) ", value):
        intents.append("third_party")
    if re.search(r"lost my job|job loss|unemployed|illness|i am ill|i'm ill|hospital|death in (?:my|the) family|my .+ (?:died|passed away)", value):
        intents.append("hardship")
    if re.search(r"lower (?:my |the )?instalments|new payment plan|restructur|payment plan", value):
        intents.append("restructuring")
    if re.search(r"human|real person|person instead|speak to (?:an? )?agent", value):
        intents.append("human")
    if re.search(r"don't want to talk|do not want to talk|stop calling|leave me alone", value):
        intents.append("dont_talk")
    if re.search(r"not (?:my|mine)|never had|closed (?:the |this )?(?:product|loan|card)|paid off|paid years ago", value):
        intents.append("dispute")
    elif re.search(r"already paid|paid (?:it|this|the overdue amount)|paid in the app", value):
        intents.append("paid")
    if re.search(r"robot|\bai\b|artificial intelligence", value):
        intents.append("robot")
    if re.search(r"scam|fraud", value):
        intents.append("scam")
    if re.search(r"what (?:is this|the call|are you calling)|what's (?:this|the call)|why (?:are you|is the bank) calling|how much (?:do i|i) owe", value):
        intents.append("purpose")
    if re.search(r"discount|waiver|waive", value):
        intents.append("discount")
    if re.search(r"(?:change|cancel) (?:my |the |this )?(?:recorded )?promise", value):
        intents.append("change_recorded" if "change" in value else "cancel")
    if re.search(r"too much|full amount.*(?:can't|cannot)|cannot pay the full|can't pay the full", value):
        intents.append("too_much")
    if re.search(r"(?:refuse|decline|won't|will not|don't want to|do not want to) (?:to )?(?:answer|give|tell|security)", value):
        intents.append("decline")
    date_text, amount, answer, callback_time = "", "", "", ""
    all_products = bool(re.search(r"\ball\b|\bboth\b|everything", value))
    if phase == "identity" and not intents:
        if identity_field == "date_of_birth":
            try:
                answer = resolve_date(value, now, dob=True)
            except (FoundationError, ValueError):
                pass
        elif re.fullmatch(r"[0-9]{4}", value):
            answer = value
        if answer:
            return Turn(("identity_answer",), answer=answer)
    if phase == "person" and value in {first_name.lower(), last_name.lower(), f"{first_name} {last_name}".lower(), f"i am {first_name} {last_name}".lower()}:
        intents.append("yes")
    # During person check only the four confirmation classes in A.3 are accepted.
    yes_set = {"yes", "that's me", "that is me", "speaking"} if phase == "person" else YES
    if value in yes_set:
        intents.append("yes")
    elif value in NO or re.fullmatch(r"(?:i )?(?:cannot|can't) pay today", value):
        intents.append("no")
    if phase != "identity":
        try:
            date_text = resolve_date(value, now)
        except (FoundationError, ValueError):
            if re.search(r"next week|week after next|end of the month|salary|sometime", value):
                intents.append("vague_date")
        # An amount needs payment/currency context; dates/phone digits aren't payment amounts.
        match = re.search(r"(?:pay|amount(?: is)?|\$|€)\s*(\d+(?:\.\d{1,2})?)\b", value)
        if match:
            amount = match[1]
        if phase == "third_party":
            time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", value)
            if time_match:
                callback_time = f"{int(time_match[1]):02d}:{time_match[2]}"
            else:
                words = {"ten": 10, "eleven": 11, "twelve": 12}
                time_match = re.search(r"(?:at|around)\s+(ten|eleven|twelve|\d{1,2})\b", value)
                if time_match:
                    hour = words[time_match[1]] if time_match[1] in words else int(time_match[1])
                    if re.search(r"\b(?:pm|afternoon|evening)\b", value) and hour < 12:
                        hour += 12
                    callback_time = f"{hour:02d}:00"
            if date_text and callback_time:
                intents.append("callback")
        elif date_text or amount:
            intents.append("proposal")
    if not intents:
        intents.append("question" if "?" in original else "unknown")
    return Turn(tuple(dict.fromkeys(intents)), date_text=date_text, amount=amount, answer=answer,
                time=callback_time, all_products=all_products, consent_token=token if "yes" in intents else "")


def turn_contract():
    return {"kind": "offline_conversation_turn_contract", "intents": sorted(INTENTS),
            "fields": list(asdict(Turn()).keys()), "unknown_means_consent": False,
            "free_text_language": "bounded English", "readback_confirmation": "clear affirmative plus current digest",
            "network_transport": "none"}
