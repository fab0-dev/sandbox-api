import datetime
import itertools
import os
import re
import unittest

from payments.cards import (
    FIELDS,
    FIELD_MESSAGES,
    CardSummary,
    detect_brand,
    luhn_valid,
    mask_card,
    validate_card,
)

TODAY = datetime.date(2026, 10, 18)


def good_card(**changes):
    card = {
        "cardholder_name": "Ada Lovelace",
        "number": "4242424242424242",
        "expiry_month": 12,
        "expiry_year": 2030,
        "security_code": "123",
    }
    card.update(changes)
    return card


def errors_of(card):
    return {e.field: e.code for e in validate_card(card, today=TODAY)}


class LuhnTest(unittest.TestCase):
    def test_valid_numbers(self):
        for number in ("4242424242424242", "5555555555554444", "378282246310005", "6011111111111117", "79927398713"):
            self.assertTrue(luhn_valid(number), number)

    def test_invalid_numbers(self):
        for number in ("4242424242424241", "1234567890123456", "", "abcd", "4242 4242"):
            self.assertFalse(luhn_valid(number), number)


class BrandTest(unittest.TestCase):
    def test_brands(self):
        cases = {
            "4242424242424242": "visa",
            "4000000000000002": "visa",
            "5555555555554444": "mastercard",
            "5105105105105100": "mastercard",
            "2223003122003222": "mastercard",
            "378282246310005": "amex",
            "341111111111111": "amex",
            "6011111111111117": "discover",
            "6500000000000002": "discover",
            "3530111333300000": "unknown",
            "9999999999999995": "unknown",
        }
        for number, brand in cases.items():
            self.assertEqual(detect_brand(number), brand, number)

    def test_spaces_and_dashes_are_ignored(self):
        self.assertEqual(detect_brand("4242 4242-4242 4242"), "visa")

    def test_not_a_number(self):
        for value in (None, "", "abc", 4242, "4x42"):
            self.assertEqual(detect_brand(value), "unknown")


class MaskTest(unittest.TestCase):
    def test_brand_and_last_four_only(self):
        summary = mask_card("4242 4242 4242 4242")
        self.assertEqual(summary, CardSummary(brand="visa", last4="4242"))
        self.assertEqual(mask_card("378282246310005"), CardSummary("amex", "0005"))
        self.assertEqual(set(vars(summary)), {"brand", "last4"})

    def test_rejects_what_is_not_a_card_number(self):
        for value in (None, "", "123", "abcdabcdabcdabcd", 4242424242424242, "4" * 20):
            with self.assertRaises(ValueError):
                mask_card(value)


class ValidateCardTest(unittest.TestCase):
    def test_good_card(self):
        self.assertEqual(validate_card(good_card(), today=TODAY), [])

    def test_number_with_spaces_and_dashes(self):
        self.assertEqual(errors_of(good_card(number="4242 4242-4242 4242")), {})

    def test_default_today_is_used(self):
        self.assertEqual(validate_card(good_card(expiry_year=datetime.date.today().year + 2)), [])
        self.assertEqual(
            [e.code for e in validate_card(good_card(expiry_year=2001))], ["expired"]
        )

    def test_missing_fields(self):
        errors = validate_card({}, today=TODAY)
        self.assertEqual(
            [(e.field, e.code) for e in errors],
            [
                ("cardholder_name", "required"),
                ("number", "required"),
                ("expiry", "required"),
                ("security_code", "required"),
            ],
        )

    def test_not_a_mapping_counts_as_empty(self):
        for value in (None, [], "card", 5):
            self.assertEqual(len(validate_card(value, today=TODAY)), 4)

    def test_blank_values_are_required(self):
        card = good_card(cardholder_name="   ", number=" - ", security_code="")
        self.assertEqual(
            errors_of(card),
            {"cardholder_name": "required", "number": "required", "security_code": "required"},
        )

    def test_all_invalid_fields_listed_in_order(self):
        card = good_card(cardholder_name="", number="4242424242424241", expiry_year=2020, security_code="12")
        self.assertEqual(
            [(e.field, e.code) for e in validate_card(card, today=TODAY)],
            [
                ("cardholder_name", "required"),
                ("number", "invalid_number"),
                ("expiry", "expired"),
                ("security_code", "invalid_length"),
            ],
        )

    def test_name(self):
        self.assertEqual(errors_of(good_card(cardholder_name="A" * 100)), {})
        self.assertEqual(errors_of(good_card(cardholder_name="A" * 101)), {"cardholder_name": "invalid_format"})
        self.assertEqual(errors_of(good_card(cardholder_name=42)), {"cardholder_name": "invalid_format"})
        self.assertEqual(errors_of(good_card(cardholder_name="Ada\nLovelace")), {"cardholder_name": "invalid_format"})

    def test_number_format(self):
        for number in ("4242abcd42424242", "42424242424", "4" * 20, 4242424242424242, "4242.4242.4242.4242"):
            self.assertEqual(errors_of(good_card(number=number)), {"number": "invalid_format"}, number)

    def test_number_check_digit(self):
        self.assertEqual(errors_of(good_card(number="4242424242424241")), {"number": "invalid_number"})

    def test_number_length_bounds(self):
        self.assertEqual(errors_of(good_card(number="79927398713")), {"number": "invalid_format"})  # 11 digits
        self.assertEqual(errors_of(good_card(number="6011000000000004")), {})  # 16 digits
        self.assertEqual(errors_of(good_card(number="4000000000000000006")), {})  # 19 digits, Luhn valid

    def test_expiry_not_in_the_past(self):
        self.assertEqual(errors_of(good_card(expiry_month=10, expiry_year=2026)), {})  # current month
        self.assertEqual(errors_of(good_card(expiry_month=9, expiry_year=2026)), {"expiry": "expired"})
        self.assertEqual(errors_of(good_card(expiry_month=1, expiry_year=2027)), {})
        self.assertEqual(errors_of(good_card(expiry_month=12, expiry_year=2025)), {"expiry": "expired"})

    def test_two_digit_year(self):
        self.assertEqual(errors_of(good_card(expiry_month=11, expiry_year=26)), {})
        self.assertEqual(errors_of(good_card(expiry_month=1, expiry_year=26)), {"expiry": "expired"})
        self.assertEqual(errors_of(good_card(expiry_month=1, expiry_year=31)), {})

    def test_expiry_format(self):
        for month, year in ((0, 2030), (13, 2030), ("12", 2030), (12, "2030"), (12, 2030.0), (True, 2030), (12, 202), (12, 20300), (12, -1)):
            self.assertEqual(errors_of(good_card(expiry_month=month, expiry_year=year)), {"expiry": "invalid_format"}, (month, year))

    def test_expiry_one_part_missing(self):
        self.assertEqual(errors_of(good_card(expiry_month=None)), {"expiry": "required"})
        card = good_card()
        del card["expiry_year"]
        self.assertEqual(errors_of(card), {"expiry": "required"})

    def test_security_code_length_by_brand(self):
        self.assertEqual(errors_of(good_card(security_code="123")), {})
        self.assertEqual(errors_of(good_card(security_code="1234")), {"security_code": "invalid_length"})
        self.assertEqual(errors_of(good_card(security_code="12")), {"security_code": "invalid_length"})
        amex = good_card(number="378282246310005")
        self.assertEqual(errors_of(dict(amex, security_code="1234")), {})
        self.assertEqual(errors_of(dict(amex, security_code="123")), {"security_code": "invalid_length"})

    def test_security_code_format(self):
        for code in ("12a", "１２３", 123, " 12"):
            self.assertEqual(errors_of(good_card(security_code=code)), {"security_code": "invalid_format"}, code)

    def test_brand_still_found_when_check_digit_fails(self):
        amex_bad = good_card(number="378282246310006", security_code="123")
        self.assertEqual(errors_of(amex_bad), {"number": "invalid_number", "security_code": "invalid_length"})

    def test_security_code_not_blamed_for_a_missing_number(self):
        self.assertEqual(errors_of(good_card(number="", security_code="1234")), {"number": "required"})
        self.assertEqual(errors_of(good_card(number="abc", security_code="123")), {"number": "invalid_format"})
        self.assertEqual(errors_of(good_card(number="", security_code="12")), {"number": "required", "security_code": "invalid_length"})

    def test_messages_are_plain_and_leak_no_card_data(self):
        card = good_card(
            cardholder_name="",
            number="4242424242424241",
            expiry_year=2020,
            security_code="98",
        )
        for e in validate_card(card, today=TODAY):
            self.assertTrue(e.message)
            self.assertNotIn("4242424242424241", e.message)
            self.assertNotIn("4241", e.message)
            self.assertNotIn("98", e.message)
        weird = good_card(number="4242424242424242x", security_code="9876543")
        for e in validate_card(weird, today=TODAY):
            self.assertNotIn("9876543", e.message)
            self.assertNotIn("4242", e.message)


DOC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "field-messages.md")
README_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")

CODES = ("required", "invalid_format", "invalid_number", "expired", "invalid_length")
VARIANTS = ("amex", "other_brands", "brand_unknown")

# Inputs that between them reach every message the checks can return.
NAMES = (None, "", "   ", "Ada Lovelace", "A" * 100, "A" * 101, 42, "Ada\nLovelace", "Ada\x7f")
NUMBERS = (
    None, "", " - ", "abc", "42424242424", "4" * 20, 4242424242424242,
    "4242424242424242", "4242424242424241",  # visa: good, bad check digit
    "378282246310005", "378282246310006",  # amex
    "5555555555554444", "6011111111111117",  # mastercard, discover
    "1000000000000008",  # passes the check, starts like no known brand
)
EXPIRIES = (
    (None, None), (None, 2030), (12, None), (0, 2030), (13, 2030), ("12", 2030), (12, 202),
    (9, 2026), (12, 2025), (10, 2026), (12, 2030),
)
SECURITY_CODES = (None, "", "12a", 123, "1", "12", "123", "1234", "12345")


def every_message_returned():
    seen = {}
    for name, number, (month, year), code in itertools.product(NAMES, NUMBERS, EXPIRIES, SECURITY_CODES):
        card = {
            "cardholder_name": name,
            "number": number,
            "expiry_month": month,
            "expiry_year": year,
            "security_code": code,
        }
        for e in validate_card(card, today=TODAY):
            seen.setdefault((e.field, e.code, e.message), card)
    return seen


def parse_doc_table():
    """The rows of the Messages table in docs/field-messages.md, as
    (field, code, brand, message, rule) with the backticks removed."""
    with open(DOC_PATH, encoding="utf-8") as f:
        text = f.read()
    section = text.split("\n## Messages\n", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    header, divider, *rows = lines
    assert [c.strip() for c in header.strip("|").split("|")] == ["Field", "Code", "Brand", "Message", "Rule"]
    parsed = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split(" | ")]
        assert len(cells) == 5, row
        parsed.append(tuple(c[1:-1] if c.startswith("`") and c.endswith("`") else c for c in cells))
    return parsed


class MessageTableTest(unittest.TestCase):
    def test_table_covers_every_field_and_code(self):
        for field, code, variant in FIELD_MESSAGES:
            self.assertIn(field, FIELDS)
            self.assertIn(code, CODES)
            self.assertEqual(variant is not None, code == "invalid_length", (field, code, variant))
            if variant is not None:
                self.assertIn(variant, VARIANTS)
        self.assertEqual(
            {(f, c) for f, c, _ in FIELD_MESSAGES},
            {
                ("cardholder_name", "required"), ("cardholder_name", "invalid_format"),
                ("number", "required"), ("number", "invalid_format"), ("number", "invalid_number"),
                ("expiry", "required"), ("expiry", "invalid_format"), ("expiry", "expired"),
                ("security_code", "required"), ("security_code", "invalid_format"), ("security_code", "invalid_length"),
            },
        )
        self.assertEqual(
            {v for f, c, v in FIELD_MESSAGES if c == "invalid_length"}, set(VARIANTS)
        )

    def test_table_is_in_field_order_and_messages_are_distinct(self):
        order = [FIELDS.index(f) for f, _, _ in FIELD_MESSAGES]
        self.assertEqual(order, sorted(order))
        texts = list(FIELD_MESSAGES.values())
        self.assertEqual(len(texts), len(set(texts)))
        for text in texts:
            self.assertTrue(text.strip())
            self.assertNotIn("`", text)
            self.assertNotIn("|", text)

    def test_every_message_the_service_returns_is_in_the_table(self):
        seen = every_message_returned()
        in_table = set(FIELD_MESSAGES.values())
        for (field, code, message), card in seen.items():
            self.assertIn(message, in_table, (field, code, card))
            self.assertIn(message, [m for (f, c, _), m in FIELD_MESSAGES.items() if (f, c) == (field, code)])

    def test_every_message_in_the_table_can_be_returned(self):
        returned = {message for _, _, message in every_message_returned()}
        self.assertEqual(returned, set(FIELD_MESSAGES.values()))

    def test_security_code_variants_follow_the_brand(self):
        def message(number, code):
            (e,) = [e for e in validate_card(good_card(number=number, security_code=code), today=TODAY) if e.field == "security_code"]
            return e.message

        amex = FIELD_MESSAGES[("security_code", "invalid_length", "amex")]
        other = FIELD_MESSAGES[("security_code", "invalid_length", "other_brands")]
        unknown = FIELD_MESSAGES[("security_code", "invalid_length", "brand_unknown")]
        self.assertEqual(message("378282246310005", "123"), amex)
        for number in ("4242424242424242", "5555555555554444", "6011111111111117", "1000000000000008"):
            self.assertEqual(message(number, "1234"), other, number)
        for number in ("", "abc", None):
            self.assertEqual(message(number, "12"), unknown, number)

    def test_messages_equal_the_contract_texts(self):
        self.assertEqual(
            FIELD_MESSAGES[("cardholder_name", "invalid_format", None)],
            "Enter the name on the card, up to 100 characters.",
        )


class FieldMessagesDocumentTest(unittest.TestCase):
    def test_document_matches_the_table_exactly(self):
        rows = parse_doc_table()
        from_doc = [(f, c, None if b == "-" else b, m) for f, c, b, m, _ in rows]
        self.assertEqual(from_doc, [(f, c, v, m) for (f, c, v), m in FIELD_MESSAGES.items()])

    def test_every_row_has_a_rule(self):
        for row in parse_doc_table():
            self.assertTrue(row[4].strip(), row)

    def test_readme_links_the_document(self):
        with open(README_PATH, encoding="utf-8") as f:
            self.assertIn("(docs/field-messages.md)", f.read())

    def test_document_names_no_message_that_is_not_in_the_table(self):
        with open(DOC_PATH, encoding="utf-8") as f:
            text = f.read()
        quoted = set(re.findall(r"`([^`]*[a-z]{2,}[^`]* [^`]*\.)`", text))
        self.assertTrue(quoted)
        self.assertLessEqual(quoted, set(FIELD_MESSAGES.values()))


if __name__ == "__main__":
    unittest.main()
