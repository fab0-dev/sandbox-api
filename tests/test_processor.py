import dataclasses
import datetime
import pathlib
import unittest

from payments.cards import mask_card, validate_card
from payments.processor import (
    DECLINE_MESSAGES,
    DECLINE_REASONS,
    DECLINED,
    SUCCEEDED,
    SUCCESS_MESSAGE,
    TEST_CARDS,
    process_card,
)

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "test-cards.md"


class ProcessCardTest(unittest.TestCase):
    def test_each_test_card_gives_its_outcome(self):
        for card in TEST_CARDS:
            result = process_card(card.number)
            self.assertEqual(result.outcome, card.outcome, card.number)
            self.assertEqual(result.decline_reason, card.decline_reason, card.number)

    def test_every_decline_reason_has_a_test_card_and_a_message(self):
        reasons = {c.decline_reason for c in TEST_CARDS if c.outcome == DECLINED}
        self.assertEqual(reasons, set(DECLINE_REASONS))
        self.assertEqual(set(DECLINE_MESSAGES), set(DECLINE_REASONS))
        self.assertEqual(len(set(DECLINE_MESSAGES.values())), len(DECLINE_REASONS))

    def test_both_outcomes_are_covered(self):
        self.assertEqual({c.outcome for c in TEST_CARDS}, {SUCCEEDED, DECLINED})

    def test_messages(self):
        self.assertEqual(process_card("4242424242424242").message, SUCCESS_MESSAGE)
        self.assertEqual(process_card("4000000000000002").message, "Your card was declined. Try another card.")
        self.assertEqual(process_card("4000000000009995").message, "Your card has insufficient funds. Try another card.")
        self.assertEqual(process_card("4000000000000069").message, "Your card has expired. Check the expiry date or use another card.")
        self.assertEqual(process_card("4000000000000127").message, "Some of your card details are incorrect. Check them and try again.")
        self.assertEqual(process_card("4000000000000119").message, "We could not process the payment right now. Please try again.")

    def test_any_other_valid_number_is_approved(self):
        for number in ("378282246310005", "6011111111111117", "4000000000000000006"):
            result = process_card(number)
            self.assertEqual((result.outcome, result.decline_reason), (SUCCEEDED, None), number)

    def test_spaces_and_dashes_are_ignored(self):
        self.assertEqual(process_card("4000 0000-0000 9995").decline_reason, "insufficient_funds")

    def test_not_a_card_number(self):
        for value in (None, "", "abcd", 4242424242424242):
            with self.assertRaises(ValueError):
                process_card(value)

    def test_same_card_same_answer(self):
        self.assertEqual(process_card("4000000000000119"), process_card("4000000000000119"))

    def test_result_carries_no_card_data(self):
        for card in TEST_CARDS:
            result = process_card(card.number)
            self.assertEqual({f.name for f in dataclasses.fields(result)}, {"outcome", "decline_reason", "message"})
            self.assertNotIn(card.number, repr(result))
            self.assertNotIn(card.number[-4:], result.message)


class TestCardsAreUsableTest(unittest.TestCase):
    def test_test_cards_pass_validation_with_any_good_details(self):
        today = datetime.date(2026, 10, 18)
        for card in TEST_CARDS:
            details = {
                "cardholder_name": "Test Customer",
                "number": card.number,
                "expiry_month": 10,
                "expiry_year": 2026,
                "security_code": "123",
            }
            self.assertEqual(validate_card(details, today=today), [], card.number)
            self.assertEqual(mask_card(card.number).brand, card.brand, card.number)


class DocumentationTest(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")
        self.squashed = self.text.replace(" ", "")

    def test_every_test_card_is_documented(self):
        for card in TEST_CARDS:
            self.assertIn(card.number, self.squashed)

    def test_every_decline_reason_and_message_is_documented(self):
        for reason in DECLINE_REASONS:
            self.assertIn("`%s`" % reason, self.text)
        for message in list(DECLINE_MESSAGES.values()) + [SUCCESS_MESSAGE]:
            self.assertIn(message, self.text)

    def test_each_card_row_matches_its_outcome_and_reason(self):
        rows = {}
        for line in self.text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("|") and len(cells) == 5:
                rows[cells[0].replace(" ", "")] = cells
        for card in TEST_CARDS:
            cells = rows[card.number]
            self.assertEqual(cells[2], card.outcome)
            expected_reason = "`%s`" % card.decline_reason if card.decline_reason else "-"
            self.assertEqual(cells[3], expected_reason)


if __name__ == "__main__":
    unittest.main()
