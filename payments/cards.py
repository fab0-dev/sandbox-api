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
        return FieldError("cardholder_name", "required", "Enter the name on the card.")
    if (
        not isinstance(name, str)
        or len(name.strip()) > NAME_MAX_LENGTH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        return FieldError(
            "cardholder_name",
            "invalid_format",
            "Enter the name on the card, up to %d characters." % NAME_MAX_LENGTH,
        )
    return None


def _check_number(card: Mapping[str, Any]) -> Optional[FieldError]:
    raw = card.get("number")
    if raw is None or (isinstance(raw, str) and normalize_number(raw).strip() == ""):
        return FieldError("number", "required", "Enter your card number.")
    digits = normalize_number(raw)
    if digits is None or not _is_digits(digits) or not NUMBER_MIN_DIGITS <= len(digits) <= NUMBER_MAX_DIGITS:
        return FieldError(
            "number",
            "invalid_format",
            "Enter the card number using digits only, 12 to 19 of them. Spaces and dashes are fine.",
        )
    if not luhn_valid(digits):
        return FieldError(
            "number",
            "invalid_number",
            "This card number is not valid. Check it and try again.",
        )
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_expiry(card: Mapping[str, Any], today: datetime.date) -> Optional[FieldError]:
    month = card.get("expiry_month")
    year = card.get("expiry_year")
    if month is None or year is None:
        return FieldError("expiry", "required", "Enter the expiry date, month and year.")
    if not _is_int(month) or not _is_int(year) or not 1 <= month <= 12:
        return FieldError("expiry", "invalid_format", "Enter a valid expiry date, month 1 to 12 and a year such as 2030.")
    if 0 <= year <= 99:
        year += 2000
    elif not 1000 <= year <= 9999:
        return FieldError("expiry", "invalid_format", "Enter a valid expiry date, month 1 to 12 and a year such as 2030.")
    # A card is good through the last day of its expiry month.
    if (year, month) < (today.year, today.month):
        return FieldError("expiry", "expired", "This card has expired. Check the expiry date or use another card.")
    return None


def _check_security_code(card: Mapping[str, Any], brand: Optional[str]) -> Optional[FieldError]:
    code = card.get("security_code")
    if code is None or code == "":
        return FieldError("security_code", "required", "Enter the security code from your card.")
    if not isinstance(code, str) or not _is_digits(code):
        return FieldError("security_code", "invalid_format", "The security code has digits only.")
    if brand is None:
        # The number is missing or malformed, so the brand is not known:
        # accept either length rather than blame this field for the number.
        ok = len(code) in (3, 4)
        message = "The security code has 3 or 4 digits."
    else:
        expected = expected_security_code_length(brand)
        ok = len(code) == expected
        if brand == "amex":
            message = "Enter the 4-digit security code from the front of your American Express card."
        else:
            message = "Enter the 3-digit security code from the back of your card."
    if not ok:
        return FieldError("security_code", "invalid_length", message)
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
