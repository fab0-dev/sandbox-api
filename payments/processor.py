"""The test-mode processor. No real provider is called and no money moves.

The outcome comes from the card number: a few known test numbers give each
decline reason, and every other number that passes the check digit is
approved. See docs/test-cards.md.

The processor sees the card number only for the duration of the call. Its
result carries no card data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from payments.cards import _is_digits, normalize_number

SUCCEEDED = "succeeded"
DECLINED = "declined"

DECLINE_REASONS = (
    "card_declined",
    "insufficient_funds",
    "expired_card",
    "incorrect_card_details",
    "processing_error",
)

SUCCESS_MESSAGE = "Payment received. Your order is paid."

# Shown to the customer as is.
DECLINE_MESSAGES = {
    "card_declined": "Your card was declined. Try another card.",
    "insufficient_funds": "Your card has insufficient funds. Try another card.",
    "expired_card": "Your card has expired. Check the expiry date or use another card.",
    "incorrect_card_details": "Some of your card details are incorrect. Check them and try again.",
    "processing_error": "We could not process the payment right now. Please try again.",
}


@dataclass(frozen=True)
class KnownCard:
    """A documented test card and what it gives."""

    number: str
    brand: str
    outcome: str
    decline_reason: Optional[str] = None


# The cards documented in docs/test-cards.md.
TEST_CARDS = (
    KnownCard("4242424242424242", "visa", SUCCEEDED),
    KnownCard("5555555555554444", "mastercard", SUCCEEDED),
    KnownCard("4000000000000002", "visa", DECLINED, "card_declined"),
    KnownCard("4000000000009995", "visa", DECLINED, "insufficient_funds"),
    KnownCard("4000000000000069", "visa", DECLINED, "expired_card"),
    KnownCard("4000000000000127", "visa", DECLINED, "incorrect_card_details"),
    KnownCard("4000000000000119", "visa", DECLINED, "processing_error"),
)

_DECLINE_BY_NUMBER = {c.number: c.decline_reason for c in TEST_CARDS if c.outcome == DECLINED}


@dataclass(frozen=True)
class ProcessorResult:
    outcome: str  # "succeeded" or "declined"
    decline_reason: Optional[str]  # None when succeeded
    message: str


def process_card(number: str) -> ProcessorResult:
    """Decide the outcome of one payment attempt from the card number.

    The card must already have passed validate_card; a value that is not a
    string of digits (spaces and dashes allowed) raises ValueError.
    """
    digits = normalize_number(number)
    if digits is None or not _is_digits(digits):
        raise ValueError("not a card number")
    reason = _DECLINE_BY_NUMBER.get(digits)
    if reason is None:
        return ProcessorResult(SUCCEEDED, None, SUCCESS_MESSAGE)
    return ProcessorResult(DECLINED, reason, DECLINE_MESSAGES[reason])
