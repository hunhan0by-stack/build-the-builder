# Collection specification
Policy ID: demo-controls-439d694
1. Business days are Monday to Thursday.
2. Bank holidays are 2026-10-19, 2026-12-30.
3. Identity questions, in order: phone_last4, date_of_birth.
4. Maximum wrong identity answers: 4.
5. Maximum products: 1.
6. Notes are disabled.
7. Promise write attempts: 2.
8. Floor fields: {"car_loan": "instalment_amount", "credit_card": "minimum_payment", "personal_loan": "instalment_amount"}.
9. Delinquency bands: [{"deadline_business_days": 9, "max_days_late": 70, "min_days_late": 1, "name": "source-window", "persuasion_attempts": 1}].
10. Read accounts only after all required identity questions pass.
11. Record promises only after an explicit current readback confirmation.
12. Record promises only within the account floor and past-due amount.
13. Record promises only on business days.
14. Maximum payment amount: 315.75.
15. Latest payment date: 2026-10-28.
16. Say goodbye before closing the call.
17. Close each call once, after all other actions.
18. Closing outcomes: [{"outcome": "promise", "reasons": [], "when": "confirmed promise"}, {"outcome": "paid", "reasons": [], "when": "all products paid"}, {"outcome": "transferred", "reasons": ["human"], "when": "human request"}, {"outcome": "no_promise", "reasons": ["refused", "service_error"], "when": "no recorded promise"}, {"outcome": "not_verified", "reasons": ["failed", "declined"], "when": "identity incomplete"}, {"outcome": "not_reached", "reasons": ["wrong_number"], "when": "wrong recipient"}].
Wording opening: This is a digital assistant for {bank_name}; this call is recorded.
Wording identity_question: Please provide your {identity_field}.
Wording account_summary: Your {product_label} has {past_due} overdue.
Wording proposal_request: Can you pay {amount} on {date}?
Wording readback: Please confirm {amount} on {date} for your {product_label}.
Wording promise_success: The promise was recorded successfully.
Wording goodbye: Thank you for your time. Goodbye.
19. Earliest payment date: 2026-10-14.


# Memo

Policy ID: memo-439d694-28137
1. This memo overrides rule 14 of policy demo-controls-439d694 with: Maximum payment amount: 281.37.
2. This memo overrides rule 19 of policy demo-controls-439d694 with: Earliest payment date: 2026-10-20.
3. This memo overrides rule 15 of policy demo-controls-439d694 with: Latest payment date: 2026-10-26.
