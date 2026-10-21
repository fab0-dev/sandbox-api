"""Pay order, Refund payment, Order payment status and List payments, without any HTTP.

`PaymentService` holds the rules of the contract:

- a request is refused, and no payment is tried or recorded, when it is
  malformed (invalid_request), the amount is not a positive integer
  (invalid_amount), the card details are invalid (validation_failed), the
  Idempotency-Key was used for another request (idempotency_key_reused), the
  order was refunded (order_refunded) or the order is already paid
  (order_already_paid), judged in that order;
- attempts are serialised per order, so two simultaneous requests for one order
  run one after the other and an order is never charged twice;
- a refund runs under the same per-order lock, so a refund and a payment for
  one order never interleave, and a payment is refunded at most once. It is
  refused, changing nothing, for a missing or blank reason, a reason over 500
  characters, an unknown payment, a declined payment or an already refunded
  one, judged in that order;
- a repeated Idempotency-Key with the same request returns the first answer
  without a new attempt. Only succeeded and declined answers are remembered;
- a decline is a normal answer, not an error.

Card data: the full card number and the security code live only in local
variables of `pay_order`. What is remembered for an Idempotency-Key is a keyed
digest computed without them, and the stored payment holds only the brand and
the last four digits. Refusal messages are fixed texts.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from payments.cards import FieldError, mask_card, validate_card
from payments.processor import ProcessorResult, process_card
from payments.store import (
    MAX_REASON_LENGTH,
    OUTCOMES,
    InvalidCursor,
    OrderAlreadyPaid,
    Page,
    Payment,
    PaymentAlreadyRefunded,
    PaymentDeclined,
    PaymentNotFound,
    PaymentStore,
)

ORDER_REFERENCE_MAX = 255
KEY_MAX = 255
# sqlite3 stores integers in 64 bits.
AMOUNT_MAX = 2**63 - 1
# How many Idempotency-Keys are remembered; the oldest finished ones go first.
DEFAULT_KEY_LIMIT = 10000

_CURRENCY = re.compile(r"[A-Z]{3}")


class Refusal(Exception):
    """The request is refused: no payment was tried and nothing was recorded.

    `code` is the contract's error code. `extra` holds the optional members of
    the error body: `fields` (validation_failed), `order_reference`
    (order_already_paid, order_refunded), `refunded_at` (order_refunded,
    payment_already_refunded) or `payment_reference` (payment_not_found,
    payment_declined, payment_already_refunded).
    """

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class PayResult:
    """A succeeded or declined answer."""

    payment: Payment
    order_paid: bool  # true when this attempt succeeded
    replayed: bool = False  # true when answered from an earlier Idempotency-Key

    def body(self) -> dict[str, Any]:
        return {"payment": self.payment.as_dict(), "order_paid": self.order_paid}


@dataclass(frozen=True)
class OrderStatus:
    order_reference: str
    status: str  # "unpaid", "paid" or "refunded"
    refunded_at: Optional[str]  # the refund's time when status is "refunded", else None
    payments: list[Payment]

    @property
    def paid(self) -> bool:
        """Kept for existing readers: true only when status is "paid"."""
        return self.status == "paid"

    def body(self) -> dict[str, Any]:
        return {
            "order_reference": self.order_reference,
            "status": self.status,
            "paid": self.paid,
            "refunded_at": self.refunded_at,
            "payments": [p.as_dict() for p in self.payments],
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _OrderLocks:
    """One lock per order reference, dropped when nobody holds or awaits it."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._locks: dict[str, list] = {}  # order -> [lock, users]

    def hold(self, order_reference: str) -> "_Held":
        return _Held(self, order_reference)

    def _acquire(self, order_reference: str) -> None:
        with self._mutex:
            entry = self._locks.setdefault(order_reference, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()

    def _release(self, order_reference: str) -> None:
        with self._mutex:
            entry = self._locks[order_reference]
            entry[0].release()
            entry[1] -= 1
            if entry[1] == 0:
                del self._locks[order_reference]


class _Held:
    def __init__(self, locks: _OrderLocks, order_reference: str):
        self._locks = locks
        self._order = order_reference

    def __enter__(self) -> None:
        self._locks._acquire(self._order)

    def __exit__(self, *exc: Any) -> None:
        self._locks._release(self._order)


@dataclass
class _KeyEntry:
    digest: bytes
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[PayResult] = None  # None until answered, and for good if the owner refused or failed


class PaymentService:
    def __init__(
        self,
        store: PaymentStore,
        processor: Callable[[str], ProcessorResult] = process_card,
        key_limit: int = DEFAULT_KEY_LIMIT,
    ):
        self._store = store
        self._processor = processor
        self._key_limit = key_limit
        self._order_locks = _OrderLocks()
        self._secret = secrets.token_bytes(32)
        self._keys_mutex = threading.Lock()
        self._keys: "collections.OrderedDict[str, _KeyEntry]" = collections.OrderedDict()

    # -- Pay order -------------------------------------------------------

    def pay_order(self, body: Any, idempotency_key: Optional[str] = None) -> PayResult:
        """Pay one order by card, in one attempt.

        `body` is the parsed JSON request. Raises Refusal when the request is
        refused. A decline is returned, not raised.
        """
        order_reference, amount, currency, card = self._check_request(body, idempotency_key)
        summary = mask_card(card["number"])
        digest = self._digest(order_reference, amount, currency, card, summary.brand, summary.last4)

        if idempotency_key is None:
            return self._attempt(order_reference, amount, currency, card, summary.brand, summary.last4)

        while True:
            entry, owner = self._claim_key(idempotency_key, digest)
            if owner:
                break
            entry.done.wait()
            if entry.result is None:
                continue  # the first request was refused or failed: judge this one afresh
            if not hmac.compare_digest(entry.digest, digest):
                raise Refusal("idempotency_key_reused", "This Idempotency-Key was used for a different request.")
            first = entry.result
            return PayResult(first.payment, first.order_paid, replayed=True)

        try:
            result = self._attempt(order_reference, amount, currency, card, summary.brand, summary.last4)
        except BaseException:
            self._forget_key(idempotency_key, entry)
            raise
        entry.result = result
        entry.done.set()
        return result

    def _check_request(self, body: Any, idempotency_key: Optional[str]):
        """The refusals before any attempt, in the contract's order."""
        if not isinstance(body, dict):
            raise Refusal("invalid_request", "The request body must be a JSON object.")
        if idempotency_key is not None and not (isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= KEY_MAX):
            raise Refusal("invalid_request", "The Idempotency-Key must be 1 to %d characters." % KEY_MAX)
        order_reference = body.get("order_reference")
        if not isinstance(order_reference, str) or not 1 <= len(order_reference) <= ORDER_REFERENCE_MAX:
            raise Refusal(
                "invalid_request",
                "order_reference is required and must be a text of 1 to %d characters." % ORDER_REFERENCE_MAX,
            )
        currency = body.get("currency")
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise Refusal("invalid_request", "currency is required: three upper-case letters, such as USD.")
        amount = body.get("amount")
        if not _is_int(amount) or amount <= 0:
            raise Refusal("invalid_amount", "The amount must be a whole number of cents greater than 0.")
        if amount > AMOUNT_MAX:
            raise Refusal("invalid_amount", "The amount is too large.")
        card = body.get("card")
        errors = validate_card(card)
        if errors:
            raise Refusal(
                "validation_failed",
                "Some card details need fixing.",
                fields=[_field_body(e) for e in errors],
            )
        return order_reference, amount, currency, card

    def _digest(self, order_reference, amount, currency, card, brand, last4) -> bytes:
        """A keyed digest of the request without the card number and the
        security code: only the brand and the last four digits stand for the card."""
        material = json.dumps(
            [order_reference, amount, currency, card["cardholder_name"], brand, last4,
             card["expiry_month"], card["expiry_year"]],
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret, material, hashlib.sha256).digest()

    def _claim_key(self, key: str, digest: bytes) -> tuple[_KeyEntry, bool]:
        """The entry for a key, and whether the caller is its owner (the first
        request, which makes the attempt)."""
        with self._keys_mutex:
            entry = self._keys.get(key)
            if entry is not None:
                return entry, False
            entry = _KeyEntry(digest)
            self._keys[key] = entry
            if len(self._keys) > self._key_limit:
                for old_key, old in self._keys.items():
                    if old.done.is_set() and old.result is not None:
                        del self._keys[old_key]
                        break
            return entry, True

    def _forget_key(self, key: str, entry: _KeyEntry) -> None:
        with self._keys_mutex:
            if self._keys.get(key) is entry:
                del self._keys[key]
        entry.done.set()

    def _attempt(self, order_reference, amount, currency, card, brand, last4) -> PayResult:
        """The refunded and paid checks, the payment and the record, one order at a time."""
        with self._order_locks.hold(order_reference):
            refunded_at = self._store.refunded_at(order_reference)
            if refunded_at is not None:
                raise Refusal(
                    "order_refunded",
                    "This order was refunded, so it cannot be paid again.",
                    order_reference=order_reference,
                    refunded_at=refunded_at,
                )
            if self._store.is_paid(order_reference):
                raise _already_paid(order_reference)
            outcome = self._processor(card["number"])
            try:
                payment = self._store.record_attempt(
                    order_reference=order_reference,
                    amount=amount,
                    currency=currency,
                    outcome=outcome.outcome,
                    decline_reason=outcome.decline_reason,
                    message=outcome.message,
                    brand=brand,
                    last4=last4,
                )
            except OrderAlreadyPaid:
                raise _already_paid(order_reference) from None
        return PayResult(payment, order_paid=payment.outcome == "succeeded")

    # -- Order payment status -------------------------------------------

    def order_status(self, order_reference: str) -> OrderStatus:
        """Whether the order is unpaid, paid or refunded (and when), and its
        attempts, newest first."""
        payments = self._store.attempts_for_order(order_reference)
        succeeded = next((p for p in payments if p.outcome == "succeeded"), None)
        if succeeded is None:
            return OrderStatus(order_reference, "unpaid", None, payments)
        if succeeded.refund is None:
            return OrderStatus(order_reference, "paid", None, payments)
        return OrderStatus(order_reference, "refunded", succeeded.refund.refunded_at, payments)

    # -- Refund payment --------------------------------------------------

    def refund_payment(self, payment_reference: str, body: Any) -> Payment:
        """Refund one succeeded payment in full, once, with the shop's reason.

        `body` is the parsed JSON request (an empty object when the request had
        no body at all). Returns the payment with its refund. Raises Refusal,
        changing nothing, in the contract's order: invalid_request,
        reason_required, reason_too_long, payment_not_found, payment_declined,
        payment_already_refunded.
        """
        if not isinstance(body, dict):
            raise Refusal("invalid_request", "The request body must be a JSON object.")
        reason = body.get("reason")
        if not isinstance(reason, str) or reason.strip() == "":
            raise Refusal("reason_required", "Give the reason for the refund.")
        if len(reason) > MAX_REASON_LENGTH:
            raise Refusal("reason_too_long", "The reason can be at most %d characters." % MAX_REASON_LENGTH)
        payment = self._store.get_payment(payment_reference)
        if payment is None:
            raise _payment_not_found(payment_reference)
        # The order's lock, as in a payment, so the two never interleave. The
        # store records at most one refund per payment, so of two simultaneous
        # refunds one succeeds and the other finds the first.
        with self._order_locks.hold(payment.order_reference):
            try:
                return self._store.refund_payment(payment_reference, reason)
            except PaymentNotFound:
                raise _payment_not_found(payment_reference) from None
            except PaymentDeclined:
                raise Refusal(
                    "payment_declined",
                    "This payment was declined, so it took no money and there is nothing to refund.",
                    payment_reference=payment_reference,
                ) from None
            except PaymentAlreadyRefunded as already:
                raise Refusal(
                    "payment_already_refunded",
                    "This payment was already refunded at %s." % already.refunded_at,
                    payment_reference=payment_reference,
                    refunded_at=already.refunded_at,
                ) from None

    # -- List payments ---------------------------------------------------

    def list_payments(self, outcome: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Page:
        """Recorded payments, newest first, one page at a time. Raises
        Refusal (invalid_request) for a bad outcome, limit or cursor."""
        if outcome is not None and outcome not in OUTCOMES:
            raise Refusal("invalid_request", "outcome must be 'succeeded' or 'declined'.")
        if not _is_int(limit) or not 1 <= limit <= 500:
            raise Refusal("invalid_request", "limit must be a whole number from 1 to 500.")
        try:
            return self._store.list_payments(outcome=outcome, limit=limit, cursor=cursor)
        except InvalidCursor:
            raise Refusal("invalid_request", "cursor is not one this service issued.") from None


def _field_body(error: FieldError) -> dict[str, str]:
    return {"field": error.field, "code": error.code, "message": error.message}


def _payment_not_found(payment_reference: str) -> Refusal:
    return Refusal(
        "payment_not_found",
        "There is no payment with this reference.",
        payment_reference=payment_reference,
    )


def _already_paid(order_reference: str) -> Refusal:
    return Refusal(
        "order_already_paid",
        "This order has already been paid.",
        order_reference=order_reference,
    )
