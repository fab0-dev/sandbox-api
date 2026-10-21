"""The payment store: every payment attempt, kept in sqlite3.

Only the parts of a card that may be kept are accepted and stored: the brand
and the last four digits. There is no column for a full card number or a
security code, and `record_attempt` refuses a `last4` that is not exactly four
digits, so a full number cannot be passed in by mistake.

Lists are newest first by `created_at`, ties broken by the later attempt
first. Times are UTC, to the second, written like 2026-10-18T14:03:22Z.

A payment is refunded at most once, in full: the `refunds` table has the payment
reference as its primary key, so a second refund cannot be written, however
the callers race. A refund copies the payment's own amount and currency inside
the same statement that writes it, and a trigger refuses a refund that does not
match a succeeded payment. Every `Payment` read from the store carries its
refund, or None. A refund holds no card data.

One `PaymentStore` shares a single connection between threads and guards it
with a lock, so it can sit behind a threaded HTTP server.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Optional

from payments.cards import BRANDS
from payments.processor import DECLINE_REASONS, DECLINED, SUCCEEDED

OUTCOMES = (SUCCEEDED, DECLINED)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_REASON_LENGTH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_reference TEXT    NOT NULL UNIQUE,
    order_reference   TEXT    NOT NULL CHECK (order_reference <> ''),
    amount            INTEGER NOT NULL CHECK (amount > 0),
    currency          TEXT    NOT NULL CHECK (length(currency) = 3),
    outcome           TEXT    NOT NULL CHECK (outcome IN ('succeeded', 'declined')),
    decline_reason    TEXT,
    message           TEXT    NOT NULL,
    brand             TEXT    NOT NULL,
    last4             TEXT    NOT NULL CHECK (length(last4) = 4),
    created_at        TEXT    NOT NULL,
    CHECK ((outcome = 'succeeded') = (decline_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS payments_by_order ON payments (order_reference, created_at, seq);
CREATE INDEX IF NOT EXISTS payments_by_time ON payments (created_at, seq);
CREATE INDEX IF NOT EXISTS payments_by_outcome ON payments (outcome, created_at, seq);
-- An order is charged once: at most one succeeded attempt per order, even if
-- a caller forgets to check first.
CREATE UNIQUE INDEX IF NOT EXISTS payments_one_success_per_order
    ON payments (order_reference) WHERE outcome = 'succeeded';

-- A payment is refunded at most once: the payment reference is the key. The
-- refund is the payment's whole amount in its currency, and only a succeeded
-- payment has one. A refund is never changed or removed.
CREATE TABLE IF NOT EXISTS refunds (
    payment_reference TEXT    NOT NULL PRIMARY KEY REFERENCES payments (payment_reference),
    amount            INTEGER NOT NULL CHECK (amount > 0),
    currency          TEXT    NOT NULL CHECK (length(currency) = 3),
    reason            TEXT    NOT NULL CHECK (trim(reason) <> ''),
    refunded_at       TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS refunds_match_a_succeeded_payment
BEFORE INSERT ON refunds
WHEN NOT EXISTS (
    SELECT 1 FROM payments
    WHERE payments.payment_reference = NEW.payment_reference
      AND payments.outcome = 'succeeded'
      AND payments.amount = NEW.amount
      AND payments.currency = NEW.currency
)
BEGIN
    SELECT RAISE(ABORT, 'refund does not match a succeeded payment');
END;
CREATE TRIGGER IF NOT EXISTS refunds_are_not_updated
BEFORE UPDATE ON refunds
BEGIN
    SELECT RAISE(ABORT, 'a refund is not changed');
END;
CREATE TRIGGER IF NOT EXISTS refunds_are_not_deleted
BEFORE DELETE ON refunds
BEGIN
    SELECT RAISE(ABORT, 'a refund is not removed');
END;
"""

# Columns of a payment (the first eleven, `seq` first) and of its refund, from
# `payments p LEFT JOIN refunds r`. The refund columns are all NULL for a
# payment that was not refunded.
_FROM = "payments p LEFT JOIN refunds r ON r.payment_reference = p.payment_reference"
_COLUMNS = (
    "p.seq, p.payment_reference, p.order_reference, p.amount, p.currency, p.outcome, "
    "p.decline_reason, p.message, p.brand, p.last4, p.created_at, "
    "r.amount, r.currency, r.reason, r.refunded_at"
)


class OrderAlreadyPaid(Exception):
    """A succeeded attempt was recorded for an order that is already paid."""

    def __init__(self, order_reference: str):
        super().__init__("order already paid")
        self.order_reference = order_reference


class PaymentNotFound(Exception):
    """There is no payment with this reference."""

    def __init__(self, payment_reference: str):
        super().__init__("payment not found")
        self.payment_reference = payment_reference


class PaymentDeclined(Exception):
    """The payment was declined: it took no money, so there is nothing to refund."""

    def __init__(self, payment_reference: str):
        super().__init__("payment was declined")
        self.payment_reference = payment_reference


class PaymentAlreadyRefunded(Exception):
    """The payment already has a refund; `refunded_at` is the time of the first one."""

    def __init__(self, payment_reference: str, refunded_at: str):
        super().__init__("payment already refunded")
        self.payment_reference = payment_reference
        self.refunded_at = refunded_at


class InvalidCursor(ValueError):
    """The cursor is not one this store issued."""


@dataclass(frozen=True)
class Refund:
    """The refund of a payment: always its whole amount, in its own currency."""

    amount: int  # minor units, the payment's own
    currency: str  # the payment's own
    reason: str  # the shop's reason, as given
    refunded_at: str  # ISO 8601 UTC with a trailing Z

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "reason": self.reason,
            "refunded_at": self.refunded_at,
        }


@dataclass(frozen=True)
class Payment:
    """One recorded attempt. Holds no full card number and no security code."""

    payment_reference: str
    order_reference: str
    amount: int  # minor units
    currency: str
    outcome: str  # "succeeded" or "declined"
    decline_reason: Optional[str]
    message: str
    brand: str
    last4: str
    created_at: str  # ISO 8601 UTC with a trailing Z
    refund: Optional[Refund] = None  # None when the payment was not refunded

    def as_dict(self) -> dict[str, Any]:
        """The payment in the shape of the contract."""
        return {
            "payment_reference": self.payment_reference,
            "order_reference": self.order_reference,
            "amount": self.amount,
            "currency": self.currency,
            "outcome": self.outcome,
            "decline_reason": self.decline_reason,
            "message": self.message,
            "card": {"brand": self.brand, "last4": self.last4},
            "created_at": self.created_at,
            "refund": None if self.refund is None else self.refund.as_dict(),
        }


@dataclass(frozen=True)
class Page:
    payments: list[Payment]
    next_cursor: Optional[str]  # None when there are no older payments


def format_time(moment: datetime.datetime) -> str:
    """UTC, to the second, trailing Z. A naive datetime is taken as UTC."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc)
    return moment.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_ascii_digits(value: str) -> bool:
    return value != "" and all("0" <= ch <= "9" for ch in value)


def _row_to_payment(row: sqlite3.Row | tuple) -> Payment:
    refund = None if row[11] is None else Refund(*row[11:15])
    return Payment(*row[1:11], refund)


def _encode_cursor(created_at: str, seq: int) -> str:
    raw = json.dumps([created_at, seq], separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Any) -> tuple[str, int]:
    try:
        if not isinstance(cursor, str) or not cursor or not cursor.isascii():
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, seq = json.loads(raw.decode("ascii"))
        if not isinstance(created_at, str) or not _is_int(seq) or seq < 1:
            raise ValueError
        datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise InvalidCursor("cursor is not valid") from None
    return created_at, seq


class PaymentStore:
    """Payment attempts in a sqlite3 database.

    `path` is a file name, or ":memory:" (the default) for a store that lives
    as long as the object.
    """

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "PaymentStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writing ---------------------------------------------------------

    def record_attempt(
        self,
        *,
        order_reference: str,
        amount: int,
        currency: str,
        outcome: str,
        decline_reason: Optional[str],
        message: str,
        brand: str,
        last4: str,
        created_at: Optional[datetime.datetime] = None,
    ) -> Payment:
        """Record one attempt, succeeded or declined, and return it.

        `amount` is in minor units and greater than 0. A declined attempt has
        a decline reason; a succeeded one has none. `brand` and `last4` are
        all that is kept of the card. `created_at` defaults to now (UTC).

        Raises ValueError for a value that does not fit, and OrderAlreadyPaid
        when a succeeded attempt is recorded for an order that already has one.
        """
        self._check_attempt(order_reference, amount, currency, outcome, decline_reason, message, brand, last4)
        stamp = format_time(created_at or datetime.datetime.now(datetime.timezone.utc))
        with self._lock:
            for _ in range(5):
                reference = "pay_" + secrets.token_hex(6)
                try:
                    with self._conn:
                        self._conn.execute(
                            "INSERT INTO payments (payment_reference, order_reference, amount, currency,"
                            " outcome, decline_reason, message, brand, last4, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (reference, order_reference, amount, currency, outcome,
                             decline_reason, message, brand, last4, stamp),
                        )
                except sqlite3.IntegrityError as error:
                    if "payments.payment_reference" in str(error):
                        continue  # a reference collision: draw another
                    if outcome == SUCCEEDED and "payments.order_reference" in str(error):
                        raise OrderAlreadyPaid(order_reference) from None
                    raise
                return Payment(reference, order_reference, amount, currency, outcome,
                               decline_reason, message, brand, last4, stamp)
        raise RuntimeError("could not make a unique payment reference")

    def refund_payment(
        self,
        payment_reference: str,
        reason: str,
        refunded_at: Optional[datetime.datetime] = None,
    ) -> Payment:
        """Refund a succeeded payment in full, once, and return it with its refund.

        The refund is its whole amount in its currency, with the shop's `reason`
        kept as given (not blank, at most 500 characters) and the time of the
        refund (default now, UTC). The check and the write are one statement,
        so of two or more simultaneous refunds exactly one is recorded.

        Raises ValueError for a reason that does not fit, PaymentNotFound,
        PaymentDeclined, and PaymentAlreadyRefunded (carrying the first
        refund's time, which stays as it was). A refused refund changes nothing.
        """
        if not isinstance(reason, str) or reason.strip() == "":
            raise ValueError("reason must be text that is not blank")
        if len(reason) > MAX_REASON_LENGTH:
            raise ValueError("reason must be at most %d characters" % MAX_REASON_LENGTH)
        stamp = format_time(refunded_at or datetime.datetime.now(datetime.timezone.utc))
        with self._lock:
            try:
                with self._conn:
                    written = self._conn.execute(
                        "INSERT INTO refunds (payment_reference, amount, currency, reason, refunded_at)"
                        " SELECT payment_reference, amount, currency, ?, ? FROM payments"
                        " WHERE payment_reference = ? AND outcome = 'succeeded'",
                        (reason, stamp, payment_reference),
                    ).rowcount
            except sqlite3.IntegrityError as error:
                if "refunds.payment_reference" not in str(error):
                    raise
                first = self._conn.execute(
                    "SELECT refunded_at FROM refunds WHERE payment_reference = ?",
                    (payment_reference,),
                ).fetchone()
                if first is None:  # another writer, on another connection, is not done yet
                    raise
                raise PaymentAlreadyRefunded(payment_reference, first[0]) from None
            if written == 0:
                payment = self.get_payment(payment_reference)
                if payment is None:
                    raise PaymentNotFound(payment_reference)
                raise PaymentDeclined(payment_reference)
            payment = self.get_payment(payment_reference)
        assert payment is not None and payment.refund is not None
        return payment

    @staticmethod
    def _check_attempt(order_reference, amount, currency, outcome, decline_reason, message, brand, last4) -> None:
        if not isinstance(order_reference, str) or order_reference == "":
            raise ValueError("order_reference must be a non-empty string")
        if not _is_int(amount) or amount <= 0:
            raise ValueError("amount must be an integer greater than 0, in minor units")
        if not (isinstance(currency, str) and len(currency) == 3 and currency.isascii()
                and currency.isalpha() and currency.isupper()):
            raise ValueError("currency must be three upper-case letters")
        if outcome not in OUTCOMES:
            raise ValueError("outcome must be 'succeeded' or 'declined'")
        if outcome == DECLINED and decline_reason not in DECLINE_REASONS:
            raise ValueError("a declined attempt needs a known decline reason")
        if outcome == SUCCEEDED and decline_reason is not None:
            raise ValueError("a succeeded attempt has no decline reason")
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if brand not in BRANDS:
            raise ValueError("brand is not a known card brand")
        if not isinstance(last4, str) or len(last4) != 4 or not _is_ascii_digits(last4):
            raise ValueError("last4 must be exactly four digits")

    # -- reading ---------------------------------------------------------

    def attempts_for_order(self, order_reference: str) -> list[Payment]:
        """All attempts for an order, newest first; empty for an unknown order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT " + _COLUMNS + " FROM " + _FROM + " WHERE p.order_reference = ?"
                " ORDER BY p.created_at DESC, p.seq DESC",
                (order_reference,),
            ).fetchall()
        return [_row_to_payment(r) for r in rows]

    def is_paid(self, order_reference: str) -> bool:
        """True when at least one attempt for the order succeeded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM payments WHERE order_reference = ? AND outcome = 'succeeded' LIMIT 1",
                (order_reference,),
            ).fetchone()
        return row is not None

    def get_payment(self, payment_reference: str) -> Optional[Payment]:
        """The payment with this reference, with its refund; None when there is none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT " + _COLUMNS + " FROM " + _FROM + " WHERE p.payment_reference = ?",
                (payment_reference,),
            ).fetchone()
        return None if row is None else _row_to_payment(row)

    def refunded_at(self, order_reference: str) -> Optional[str]:
        """When the order was refunded (UTC, trailing Z); None when it was not."""
        with self._lock:
            # An order has at most one succeeded payment, so at most one refund.
            row = self._conn.execute(
                "SELECT r.refunded_at FROM payments p JOIN refunds r"
                " ON r.payment_reference = p.payment_reference WHERE p.order_reference = ?",
                (order_reference,),
            ).fetchone()
        return None if row is None else row[0]

    def is_refunded(self, order_reference: str) -> bool:
        """True when the order's succeeded payment was refunded."""
        return self.refunded_at(order_reference) is not None

    def list_payments(
        self,
        outcome: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        cursor: Optional[str] = None,
    ) -> Page:
        """Payments across all orders, newest first, one page at a time.

        `outcome` narrows to "succeeded" or "declined". `limit` is 1 to 500.
        `cursor` is the `next_cursor` of the previous page. Raises ValueError
        for a bad outcome or limit and InvalidCursor for a cursor this store
        did not issue.
        """
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError("outcome must be 'succeeded' or 'declined'")
        if not _is_int(limit) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError("limit must be an integer from 1 to %d" % MAX_LIMIT)
        where, args = [], []
        if outcome is not None:
            where.append("p.outcome = ?")
            args.append(outcome)
        if cursor is not None:
            stamp, seq = _decode_cursor(cursor)
            where.append("(p.created_at < ? OR (p.created_at = ? AND p.seq < ?))")
            args.extend([stamp, stamp, seq])
        sql = "SELECT " + _COLUMNS + " FROM " + _FROM
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.created_at DESC, p.seq DESC LIMIT ?"
        args.append(limit + 1)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(last[10], last[0])
        return Page([_row_to_payment(r) for r in rows], next_cursor)
