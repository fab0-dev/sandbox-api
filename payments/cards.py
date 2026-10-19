"""Card checks: field validation, brand detection and last-four masking.

Nothing in this module keeps, logs or echoes a full card number or a security
code. Error messages are fixed texts. The functions return only the brand and
the last four digits of a card.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Mapping, Optional

BRANDS = ("visa", "mastercard", "amex", "discover", "unknown")

NUMBER_MIN_DIGITS = 12
NUMBER_MAX_DIGITS = 19
NAME_MAX_LENGTH = 100

# Order in which invalid fields are listed (as in the contract).
FIELDS = ("cardholder_name", "number", "expiry", "security_code")


# Every card-check message the service returns, in one table: the reference
# wording for the checkout (docs/field-messages.md lists the same texts and the
# rules behind them; a test keeps the two equal). Keys are (field, code,
# variant). The variant is None except for the security code length message,
# which depends on the card brand:
#   "amex"          the number starts like an American Express card
#   "other_brands"  any other brand found from the number, including a number
#                   that passes the check but starts like no known brand
#   "brand_unknown" the number is missing or malformed, so no brand is known
VARIANT_AMEX = "amex"
VARIANT_OTHER_BRANDS = "other_brands"
VARIANT_BRAND_UNKNOWN = "brand_unknown"

FIELD_MESSAGES: dict[tuple[str, str, Optional[str]], str] = {
    ("cardholder_name", "required", None): "Enter the name on the card.",
    ("cardholder_name", "invalid_format", None): "Enter the name on the card, up to %d characters." % NAME_MAX_LENGTH,
    ("number", "required", None): "Enter your card number.",
    ("number", "invalid_format", None): "Enter the card number using digits only, 12 to 19 of them. Spaces and dashes are fine.",
    ("number", "invalid_number", None): "This card number is not valid. Check it and try again.",
    ("expiry", "required", None): "Enter the expiry date, month and year.",
    ("expiry", "invalid_format", None): "Enter a valid expiry date, month 1 to 12 and a year such as 2030.",
    ("expiry", "expired", None): "This card has expired. Check the expiry date or use another card.",
    ("security_code", "required", None): "Enter the security code from your card.",
    ("security_code", "invalid_format", None): "The security code has digits only.",
    ("security_code", "invalid_length", VARIANT_AMEX): "Enter the 4-digit security code from the front of your American Express card.",
    ("security_code", "invalid_length", VARIANT_OTHER_BRANDS): "Enter the 3-digit security code from the back of your card.",
    ("security_code", "invalid_length", VARIANT_BRAND_UNKNOWN): "The security code has 3 or 4 digits.",
}


def field_error(field: str, code: str, variant: Optional[str] = None) -> "FieldError":
    """A FieldError whose message is taken from FIELD_MESSAGES."""
    return FieldError(field, code, FIELD_MESSAGES[(field, code, variant)])


@dataclass(frozen=True)
class FieldError:
    """One invalid field. `field` is one of FIELDS; `code` is one of required,
    invalid_format, invalid_number, expired, invalid_length."""

    field: str
    code: str
    message: str


@dataclass(frozen=True)
class CardSummary:
    """The only parts of a card number that may be shown or recorded."""

    brand: str
    last4: str


def normalize_number(number: Any) -> Optional[str]:
    """Strip spaces and dashes. Returns None when the value is not a string."""
    if not isinstance(number, str):
        return None
    return number.replace(" ", "").replace("-", "")


def _is_digits(value: str) -> bool:
    # str.isdigit() accepts characters such as superscripts; accept ASCII only.
    return value != "" and all("0" <= ch <= "9" for ch in value)


def luhn_valid(digits: str) -> bool:
    """The standard check digit (Luhn, mod 10) over a string of ASCII digits."""
    if not _is_digits(digits):
        return False
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = ord(ch) - ord("0")
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def detect_brand(number: Any) -> str:
    """The card brand from the leading digits: visa, mastercard, amex,
    discover or unknown. Accepts a number with spaces or dashes."""
    digits = normalize_number(number)
    if not digits or not _is_digits(digits):
        return "unknown"
    if digits.startswith("4"):
        return "visa"
    if len(digits) >= 2 and digits[:2] in ("34", "37"):
        return "amex"
    if len(digits) >= 2 and "51" <= digits[:2] <= "55":
        return "mastercard"
    if len(digits) >= 4 and "2221" <= digits[:4] <= "2720":
        return "mastercard"
    if digits.startswith("6011") or digits[:2] == "65" or digits[:3] in ("644", "645", "646", "647", "648", "649"):
        return "discover"
    if len(digits) >= 6 and "622126" <= digits[:6] <= "622925":
        return "discover"
    return "unknown"


def mask_card(number: Any) -> CardSummary:
    """Brand and last four digits of a card number, nothing else.

    Raises ValueError when the number is not 12 to 19 digits (after removing
    spaces and dashes); call validate_card first."""
    digits = normalize_number(number)
    if digits is None or not _is_digits(digits) or not NUMBER_MIN_DIGITS <= len(digits) <= NUMBER_MAX_DIGITS:
        raise ValueError("not a card number")
    return CardSummary(brand=detect_brand(digits), last4=digits[-4:])


def expected_security_code_length(brand: str) -> int:
    return 4 if brand == "amex" else 3


def _check_name(card: Mapping[str, Any]) -> Optional[FieldError]:
    name = card.get("cardholder_name")
    if name is None or (isinstance(name, str) and name.strip() == ""):
        return field_error("cardholder_name", "required")
    if (
        not isinstance(name, str)
        or len(name.strip()) > NAME_MAX_LENGTH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        return field_error("cardholder_name", "invalid_format")
    return None


def _check_number(card: Mapping[str, Any]) -> Optional[FieldError]:
    raw = card.get("number")
    if raw is None or (isinstance(raw, str) and normalize_number(raw).strip() == ""):
        return field_error("number", "required")
    digits = normalize_number(raw)
    if digits is None or not _is_digits(digits) or not NUMBER_MIN_DIGITS <= len(digits) <= NUMBER_MAX_DIGITS:
        return field_error("number", "invalid_format")
    if not luhn_valid(digits):
        return field_error("number", "invalid_number")
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_expiry(card: Mapping[str, Any], today: datetime.date) -> Optional[FieldError]:
    month = card.get("expiry_month")
    year = card.get("expiry_year")
    if month is None or year is None:
        return field_error("expiry", "required")
    if not _is_int(month) or not _is_int(year) or not 1 <= month <= 12:
        return field_error("expiry", "invalid_format")
    if 0 <= year <= 99:
        year += 2000
    elif not 1000 <= year <= 9999:
        return field_error("expiry", "invalid_format")
    # A card is good through the last day of its expiry month.
    if (year, month) < (today.year, today.month):
        return field_error("expiry", "expired")
    return None


def _check_security_code(card: Mapping[str, Any], brand: Optional[str]) -> Optional[FieldError]:
    code = card.get("security_code")
    if code is None or code == "":
        return field_error("security_code", "required")
    if not isinstance(code, str) or not _is_digits(code):
        return field_error("security_code", "invalid_format")
    if brand is None:
        # The number is missing or malformed, so the brand is not known:
        # accept either length rather than blame this field for the number.
        ok = len(code) in (3, 4)
        variant = VARIANT_BRAND_UNKNOWN
    else:
        ok = len(code) == expected_security_code_length(brand)
        variant = VARIANT_AMEX if brand == "amex" else VARIANT_OTHER_BRANDS
    if not ok:
        return field_error("security_code", "invalid_length", variant)
    return None


def validate_card(card: Any, today: Optional[datetime.date] = None) -> list[FieldError]:
    """Check card details before any payment is tried.

    `card` is a mapping with cardholder_name, number, expiry_month,
    expiry_year and security_code. Returns every invalid field, in the order
    cardholder_name, number, expiry, security_code; an empty list when the
    card is acceptable. `today` defaults to the current UTC date.

    The security code length depends on the brand found from the number
    (4 digits for amex, 3 otherwise). When the number is missing or has the
    wrong format the brand is unknown and 3 or 4 digits are accepted.
    """
    if not isinstance(card, Mapping):
        card = {}
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()

    number_error = _check_number(card)
    brand: Optional[str] = None
    if number_error is None or number_error.code == "invalid_number":
        brand = detect_brand(card.get("number"))

    errors = [
        _check_name(card),
        number_error,
        _check_expiry(card, today),
        _check_security_code(card, brand),
    ]
    return [e for e in errors if e is not None]
