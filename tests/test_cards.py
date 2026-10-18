import datetime
import unittest

from payments.cards import (
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


if __name__ == "__main__":
    unittest.main()
