import datetime
import os
import sqlite3
import tempfile
import threading
import unittest

from payments.cards import mask_card
from payments.processor import DECLINE_MESSAGES, DECLINED, SUCCEEDED, SUCCESS_MESSAGE
from payments.store import (
    InvalidCursor,
    OrderAlreadyPaid,
    PaymentAlreadyRefunded,
    PaymentDeclined,
    PaymentNotFound,
    PaymentStore,
    Refund,
)

UTC = datetime.timezone.utc
FULL_NUMBER = "4242424242424242"
SECURITY_CODE = "737"


def at(second, minute=0):
    return datetime.datetime(2026, 10, 18, 14, minute, second, tzinfo=UTC)


def ok_attempt(order="ord_1", amount=2599, when=None, **extra):
    card = mask_card(FULL_NUMBER)
    args = dict(order_reference=order, amount=amount, currency="USD", outcome=SUCCEEDED,
                decline_reason=None, message=SUCCESS_MESSAGE, brand=card.brand,
                last4=card.last4, created_at=when)
    args.update(extra)
    return args


def declined_attempt(order="ord_1", reason="card_declined", when=None, **extra):
    args = ok_attempt(order=order, when=when, outcome=DECLINED, decline_reason=reason,
                      message=DECLINE_MESSAGES[reason])
    args.update(extra)
    return args


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)

    def test_records_a_succeeded_attempt_with_every_field(self):
        p = self.store.record_attempt(**ok_attempt(when=at(22)))
        self.assertTrue(p.payment_reference.startswith("pay_"))
        self.assertEqual(
            p.as_dict(),
            {
                "payment_reference": p.payment_reference,
                "order_reference": "ord_1",
                "amount": 2599,
                "currency": "USD",
                "outcome": "succeeded",
                "decline_reason": None,
                "message": SUCCESS_MESSAGE,
                "card": {"brand": "visa", "last4": "4242"},
                "created_at": "2026-10-18T14:00:22Z",
                "refund": None,
            },
        )
        self.assertEqual(self.store.attempts_for_order("ord_1"), [p])

    def test_records_a_declined_attempt_with_reason_and_message(self):
        p = self.store.record_attempt(**declined_attempt(reason="insufficient_funds", when=at(1)))
        self.assertEqual(p.outcome, "declined")
        self.assertEqual(p.decline_reason, "insufficient_funds")
        self.assertEqual(p.message, DECLINE_MESSAGES["insufficient_funds"])
        self.assertEqual(self.store.attempts_for_order("ord_1"), [p])

    def test_references_are_unique(self):
        refs = {self.store.record_attempt(**declined_attempt()).payment_reference for _ in range(50)}
        self.assertEqual(len(refs), 50)

    def test_time_defaults_to_now_in_utc_with_z(self):
        before = datetime.datetime.now(UTC).replace(microsecond=0)
        p = self.store.record_attempt(**ok_attempt())
        stamp = datetime.datetime.strptime(p.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, datetime.datetime.now(UTC))

    def test_other_time_zones_are_stored_as_utc(self):
        plus2 = datetime.timezone(datetime.timedelta(hours=2))
        p = self.store.record_attempt(**ok_attempt(when=datetime.datetime(2026, 10, 18, 16, 3, 22, 999, tzinfo=plus2)))
        self.assertEqual(p.created_at, "2026-10-18T14:03:22Z")

    def test_refuses_values_that_do_not_fit(self):
        bad = [
            ok_attempt(order=""),
            ok_attempt(order=None),
            ok_attempt(amount=0),
            ok_attempt(amount=-5),
            ok_attempt(amount=25.99),
            ok_attempt(amount=True),
            ok_attempt(currency="usd"),
            ok_attempt(currency="US"),
            ok_attempt(outcome="pending"),
            ok_attempt(decline_reason="card_declined"),
            declined_attempt(decline_reason=None),
            declined_attempt(decline_reason="stolen_card"),
            ok_attempt(brand="diners"),
            ok_attempt(message=None),
        ]
        for args in bad:
            with self.assertRaises(ValueError, msg=args):
                self.store.record_attempt(**args)
        self.assertEqual(self.store.list_payments().payments, [])

    def test_second_success_for_an_order_is_refused_and_not_recorded(self):
        self.store.record_attempt(**ok_attempt())
        with self.assertRaises(OrderAlreadyPaid) as ctx:
            self.store.record_attempt(**ok_attempt())
        self.assertEqual(ctx.exception.order_reference, "ord_1")
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)
        # Another order is not affected.
        self.store.record_attempt(**ok_attempt(order="ord_2"))

    def test_declines_before_and_after_a_success_are_all_kept(self):
        self.store.record_attempt(**declined_attempt(when=at(1)))
        self.store.record_attempt(**ok_attempt(when=at(2)))
        self.store.record_attempt(**declined_attempt(when=at(3)))
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 3)


class NoCardDataTest(unittest.TestCase):
    """D10: a full card number or a security code is never kept."""

    def test_last4_must_be_exactly_four_digits(self):
        store = PaymentStore()
        self.addCleanup(store.close)
        for last4 in (FULL_NUMBER, "424", "42424", "42a2", "", None, "４２４２", " 424"):
            with self.assertRaises(ValueError, msg=repr(last4)):
                store.record_attempt(**ok_attempt(last4=last4))

    def test_nothing_in_the_database_file_holds_the_number_or_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payments.db")
            store = PaymentStore(path)
            store.record_attempt(**ok_attempt())
            store.record_attempt(**declined_attempt(order="ord_2", reason="expired_card"))
            store.close()
            with open(path, "rb") as f:
                raw = f.read()
            self.assertNotIn(FULL_NUMBER.encode(), raw)
            self.assertNotIn(SECURITY_CODE.encode(), raw)
            conn = sqlite3.connect(path)
            self.addCleanup(conn.close)
            columns = [r[1] for r in conn.execute("PRAGMA table_info(payments)")]
            self.assertEqual(
                columns,
                ["seq", "payment_reference", "order_reference", "amount", "currency", "outcome",
                 "decline_reason", "message", "brand", "last4", "created_at"],
            )
            for row in conn.execute("SELECT * FROM payments"):
                for value in row:
                    self.assertNotIn(FULL_NUMBER, str(value))

    def test_returned_payments_carry_only_brand_and_last4(self):
        store = PaymentStore()
        self.addCleanup(store.close)
        p = store.record_attempt(**ok_attempt())
        shown = repr(p) + repr(p.as_dict()) + repr(store.list_payments())
        self.assertNotIn(FULL_NUMBER, shown)
        self.assertNotIn(SECURITY_CODE, shown)
        self.assertEqual(set(p.as_dict()["card"]), {"brand", "last4"})


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)

    def test_unknown_order_is_unpaid_with_no_attempts(self):
        self.assertFalse(self.store.is_paid("nope"))
        self.assertEqual(self.store.attempts_for_order("nope"), [])

    def test_order_with_only_declines_is_unpaid(self):
        self.store.record_attempt(**declined_attempt())
        self.assertFalse(self.store.is_paid("ord_1"))

    def test_order_is_paid_after_a_success_only_for_that_order(self):
        self.store.record_attempt(**declined_attempt(when=at(1)))
        self.store.record_attempt(**ok_attempt(when=at(2)))
        self.store.record_attempt(**declined_attempt(order="ord_2"))
        self.assertTrue(self.store.is_paid("ord_1"))
        self.assertFalse(self.store.is_paid("ord_2"))

    def test_order_references_match_exactly(self):
        self.store.record_attempt(**ok_attempt(order="ord_1"))
        self.assertFalse(self.store.is_paid("ORD_1"))
        self.assertFalse(self.store.is_paid("ord_1 "))
        self.assertFalse(self.store.is_paid("ord_%"))
        self.assertEqual(self.store.attempts_for_order("ord_%"), [])

    def test_attempts_for_an_order_are_newest_first(self):
        a = self.store.record_attempt(**declined_attempt(when=at(10)))
        b = self.store.record_attempt(**declined_attempt(reason="processing_error", when=at(30)))
        c = self.store.record_attempt(**ok_attempt(when=at(50)))
        self.store.record_attempt(**ok_attempt(order="other", when=at(40)))
        self.assertEqual(self.store.attempts_for_order("ord_1"), [c, b, a])

    def test_same_time_is_broken_by_the_later_attempt_first(self):
        a = self.store.record_attempt(**declined_attempt(when=at(10)))
        b = self.store.record_attempt(**declined_attempt(when=at(10)))
        c = self.store.record_attempt(**ok_attempt(when=at(10)))
        self.assertEqual(self.store.attempts_for_order("ord_1"), [c, b, a])

    def test_an_attempt_recorded_later_with_an_earlier_time_sorts_by_time(self):
        late = self.store.record_attempt(**declined_attempt(when=at(0, minute=5)))
        early = self.store.record_attempt(**declined_attempt(when=at(0, minute=1)))
        self.assertEqual(self.store.attempts_for_order("ord_1"), [late, early])


class ListTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)
        self.made = []
        for i in range(7):
            if i % 3 == 0:
                args = ok_attempt(order="ord_%d" % i, when=at(i))
            else:
                args = declined_attempt(order="ord_%d" % i, when=at(i))
            self.made.append(self.store.record_attempt(**args))
        self.newest_first = list(reversed(self.made))

    def test_lists_everything_newest_first(self):
        page = self.store.list_payments()
        self.assertEqual(page.payments, self.newest_first)
        self.assertIsNone(page.next_cursor)

    def test_empty_store_lists_nothing(self):
        with PaymentStore() as empty:
            page = empty.list_payments()
        self.assertEqual((page.payments, page.next_cursor), ([], None))

    def test_narrowed_by_outcome(self):
        succeeded = self.store.list_payments(outcome="succeeded").payments
        declined = self.store.list_payments(outcome="declined").payments
        self.assertEqual(succeeded, [p for p in self.newest_first if p.outcome == "succeeded"])
        self.assertEqual(declined, [p for p in self.newest_first if p.outcome == "declined"])
        self.assertEqual(len(succeeded) + len(declined), 7)

    def test_paging_reaches_every_payment_once_in_order(self):
        for outcome, expected in (
            (None, self.newest_first),
            ("declined", [p for p in self.newest_first if p.outcome == "declined"]),
        ):
            seen, cursor, pages = [], None, 0
            while True:
                page = self.store.list_payments(outcome=outcome, limit=2, cursor=cursor)
                self.assertLessEqual(len(page.payments), 2)
                seen.extend(page.payments)
                pages += 1
                cursor = page.next_cursor
                if cursor is None:
                    break
            self.assertEqual(seen, expected)

    def test_a_page_that_ends_exactly_at_the_end_has_no_cursor(self):
        page = self.store.list_payments(limit=7)
        self.assertEqual(len(page.payments), 7)
        self.assertIsNone(page.next_cursor)
        page = self.store.list_payments(limit=6)
        self.assertIsNotNone(page.next_cursor)

    def test_paging_across_payments_with_the_same_time(self):
        with PaymentStore() as store:
            made = [store.record_attempt(**declined_attempt(order="o%d" % i, when=at(5))) for i in range(5)]
            seen, cursor = [], None
            while True:
                page = store.list_payments(limit=2, cursor=cursor)
                seen.extend(page.payments)
                cursor = page.next_cursor
                if cursor is None:
                    break
        self.assertEqual(seen, list(reversed(made)))

    def test_new_payments_do_not_disturb_a_page_in_progress(self):
        first = self.store.list_payments(limit=3)
        self.store.record_attempt(**declined_attempt(order="new", when=at(0, minute=30)))
        second = self.store.list_payments(limit=3, cursor=first.next_cursor)
        self.assertEqual(first.payments + second.payments, self.newest_first[:6])

    def test_bad_arguments(self):
        for kwargs in ({"outcome": "pending"}, {"limit": 0}, {"limit": 501}, {"limit": "5"}, {"limit": True}):
            with self.assertRaises(ValueError, msg=kwargs):
                self.store.list_payments(**kwargs)
        self.store.list_payments(limit=1)
        self.store.list_payments(limit=500)

    def test_a_cursor_the_store_did_not_issue_is_refused(self):
        for cursor in ("", "abc", "!!!!", "bm90IGpzb24", "W10", "WyIyMDI2IiwxXQ", "é", 5):
            with self.assertRaises(InvalidCursor, msg=repr(cursor)):
                self.store.list_payments(cursor=cursor)
        self.assertTrue(issubclass(InvalidCursor, ValueError))


class PersistenceAndThreadsTest(unittest.TestCase):
    def test_attempts_survive_reopening_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payments.db")
            with PaymentStore(path) as store:
                p = store.record_attempt(**ok_attempt())
            with PaymentStore(path) as store:
                self.assertEqual(store.attempts_for_order("ord_1"), [p])
                self.assertTrue(store.is_paid("ord_1"))
                with self.assertRaises(OrderAlreadyPaid):
                    store.record_attempt(**ok_attempt())

    def test_concurrent_successes_for_one_order_record_one(self):
        with PaymentStore() as store:
            results = []

            def work():
                try:
                    store.record_attempt(**ok_attempt())
                    results.append("ok")
                except OrderAlreadyPaid:
                    results.append("paid")

            threads = [threading.Thread(target=work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), ["ok"] + ["paid"] * 7)
            self.assertEqual(len(store.attempts_for_order("ord_1")), 1)


def refund_time(second=0, minute=5):
    return at(second, minute)


class RefundTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)
        self.paid = self.store.record_attempt(**ok_attempt(when=at(22)))

    def test_refunds_the_whole_payment_and_returns_it_with_its_refund(self):
        p = self.store.refund_payment(self.paid.payment_reference, "Out of stock", refund_time(7))
        self.assertEqual(p.refund, Refund(2599, "USD", "Out of stock", "2026-10-18T14:05:07Z"))
        shown = p.as_dict()
        self.assertEqual(
            shown["refund"],
            {"amount": 2599, "currency": "USD", "reason": "Out of stock",
             "refunded_at": "2026-10-18T14:05:07Z"},
        )
        self.assertEqual(set(shown["refund"]), {"amount", "currency", "reason", "refunded_at"})
        self.assertEqual(shown["outcome"], "succeeded")
        self.assertIsNone(shown["decline_reason"])
        self.assertEqual(shown["created_at"], "2026-10-18T14:00:22Z")

    def test_amount_and_currency_are_the_payments_own(self):
        other = self.store.record_attempt(**ok_attempt(order="ord_2", amount=1, currency="JPY"))
        p = self.store.refund_payment(other.payment_reference, "Customer cancelled")
        self.assertEqual((p.refund.amount, p.refund.currency), (1, "JPY"))

    def test_reason_is_kept_exactly_as_given(self):
        reason = "  Customer cancelled \u2014 caf\u00e9\n"
        p = self.store.refund_payment(self.paid.payment_reference, reason)
        self.assertEqual(p.refund.reason, reason)
        self.assertEqual(self.store.get_payment(self.paid.payment_reference).refund.reason, reason)

    def test_time_defaults_to_now_in_utc_with_z(self):
        before = datetime.datetime.now(UTC).replace(microsecond=0)
        p = self.store.refund_payment(self.paid.payment_reference, "r")
        stamp = datetime.datetime.strptime(p.refund.refunded_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, datetime.datetime.now(UTC))

    def test_other_time_zones_are_stored_as_utc(self):
        plus2 = datetime.timezone(datetime.timedelta(hours=2))
        when = datetime.datetime(2026, 10, 18, 16, 5, 7, tzinfo=plus2)
        p = self.store.refund_payment(self.paid.payment_reference, "r", when)
        self.assertEqual(p.refund.refunded_at, "2026-10-18T14:05:07Z")

    def test_a_second_refund_is_refused_with_the_first_time_and_changes_nothing(self):
        ref = self.paid.payment_reference
        first = self.store.refund_payment(ref, "Out of stock", refund_time(1))
        with self.assertRaises(PaymentAlreadyRefunded) as ctx:
            self.store.refund_payment(ref, "Another reason", refund_time(59))
        self.assertEqual(ctx.exception.refunded_at, "2026-10-18T14:05:01Z")
        self.assertEqual(ctx.exception.payment_reference, ref)
        self.assertEqual(self.store.get_payment(ref), first)
        self.assertEqual(self.store.get_payment(ref).refund.reason, "Out of stock")
        count = self.store._conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        self.assertEqual(count, 1)

    def test_unknown_payment_is_refused_and_nothing_is_recorded(self):
        with self.assertRaises(PaymentNotFound) as ctx:
            self.store.refund_payment("pay_nope", "r")
        self.assertEqual(ctx.exception.payment_reference, "pay_nope")
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0], 0)

    def test_declined_payment_is_refused_and_nothing_is_recorded(self):
        declined = self.store.record_attempt(**declined_attempt(order="ord_2"))
        with self.assertRaises(PaymentDeclined):
            self.store.refund_payment(declined.payment_reference, "r")
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0], 0)
        self.assertIsNone(self.store.get_payment(declined.payment_reference).refund)
        self.assertIsNone(self.store.refunded_at("ord_2"))

    def test_a_reason_that_does_not_fit_is_refused_and_nothing_is_recorded(self):
        ref = self.paid.payment_reference
        for reason in (None, 5, "", "   ", "\n\t ", "x" * 501):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    self.store.refund_payment(ref, reason)
        self.assertIsNone(self.store.get_payment(ref).refund)
        self.assertEqual(self.store.refund_payment(ref, "x" * 500).refund.reason, "x" * 500)

    def test_the_database_allows_one_refund_per_payment(self):
        ref = self.paid.payment_reference
        self.store.refund_payment(ref, "r")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute(
                "INSERT INTO refunds VALUES (?, 2599, 'USD', 'again', '2026-10-18T14:06:00Z')", (ref,))

    def test_the_database_refuses_a_refund_that_is_not_the_payments_own(self):
        declined = self.store.record_attempt(**declined_attempt(order="ord_2"))
        rows = [
            (declined.payment_reference, 2599, "USD"),  # declined
            (self.paid.payment_reference, 100, "USD"),  # part of the amount
            (self.paid.payment_reference, 2599, "EUR"),  # another currency
            ("pay_nope", 2599, "USD"),  # no such payment
        ]
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store._conn.execute(
                        "INSERT INTO refunds VALUES (?, ?, ?, 'r', '2026-10-18T14:06:00Z')", row)

    def test_a_refund_is_not_changed_or_removed(self):
        ref = self.paid.payment_reference
        self.store.refund_payment(ref, "r")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("UPDATE refunds SET reason = 'x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("DELETE FROM refunds")
        self.assertEqual(self.store.get_payment(ref).refund.reason, "r")

    def test_concurrent_refunds_record_one_and_report_the_rest_as_already_refunded(self):
        ref = self.paid.payment_reference
        results = []

        def work(n):
            try:
                p = self.store.refund_payment(ref, "reason %d" % n)
                results.append(("ok", p.refund.reason))
            except PaymentAlreadyRefunded as error:
                results.append(("refunded", error.refunded_at))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(results), 10)
        stored = self.store.get_payment(ref).refund
        self.assertEqual(stored.reason, oks[0][1])
        self.assertTrue(all(r[1] == stored.refunded_at for r in results if r[0] == "refunded"))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0], 1)

    def test_two_stores_on_one_file_record_one_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payments.db")
            with PaymentStore(path) as a, PaymentStore(path) as b:
                p = a.record_attempt(**ok_attempt())
                a.refund_payment(p.payment_reference, "first", refund_time(1))
                with self.assertRaises(PaymentAlreadyRefunded) as ctx:
                    b.refund_payment(p.payment_reference, "second", refund_time(2))
                self.assertEqual(ctx.exception.refunded_at, "2026-10-18T14:05:01Z")

    def test_refund_survives_reopening_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payments.db")
            with PaymentStore(path) as store:
                p = store.record_attempt(**ok_attempt())
                refunded = store.refund_payment(p.payment_reference, "r", refund_time(3))
            with PaymentStore(path) as store:
                self.assertEqual(store.get_payment(p.payment_reference), refunded)
                self.assertEqual(store.refunded_at("ord_1"), "2026-10-18T14:05:03Z")
                with self.assertRaises(PaymentAlreadyRefunded):
                    store.refund_payment(p.payment_reference, "again")


class RefundReadTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)
        self.declined = self.store.record_attempt(**declined_attempt(order="ord_1", when=at(1)))
        self.paid = self.store.record_attempt(**ok_attempt(order="ord_1", when=at(2)))
        self.other = self.store.record_attempt(**ok_attempt(order="ord_2", when=at(3)))
        self.unpaid = self.store.record_attempt(**declined_attempt(order="ord_3", when=at(4)))

    def refund(self, payment, second=0):
        return self.store.refund_payment(payment.payment_reference, "Out of stock", refund_time(second))

    def test_get_payment_by_reference(self):
        self.assertEqual(self.store.get_payment(self.paid.payment_reference), self.paid)
        self.assertEqual(self.store.get_payment(self.declined.payment_reference), self.declined)
        self.assertIsNone(self.store.get_payment("pay_nope"))
        self.assertIsNone(self.store.get_payment(""))

    def test_a_payment_that_was_not_refunded_carries_none_everywhere(self):
        self.assertIsNone(self.store.get_payment(self.paid.payment_reference).refund)
        for p in self.store.attempts_for_order("ord_1"):
            self.assertIsNone(p.refund)
            self.assertIn("refund", p.as_dict())
            self.assertIsNone(p.as_dict()["refund"])
        for p in self.store.list_payments().payments:
            self.assertIsNone(p.as_dict()["refund"])

    def test_order_is_refunded_only_after_its_payment_is(self):
        self.assertFalse(self.store.is_refunded("ord_1"))
        self.assertIsNone(self.store.refunded_at("ord_1"))
        self.refund(self.paid, 9)
        self.assertTrue(self.store.is_refunded("ord_1"))
        self.assertEqual(self.store.refunded_at("ord_1"), "2026-10-18T14:05:09Z")
        self.assertTrue(self.store.is_paid("ord_1"))  # the outcome stays succeeded
        for order in ("ord_2", "ord_3", "ord_unknown"):
            self.assertFalse(self.store.is_refunded(order))
            self.assertIsNone(self.store.refunded_at(order))

    def test_by_order_carries_the_refund_on_the_succeeded_payment_only(self):
        refunded = self.refund(self.paid, 9)
        attempts = self.store.attempts_for_order("ord_1")
        self.assertEqual(attempts, [refunded, self.declined])
        self.assertEqual(attempts[0].refund.refunded_at, self.store.refunded_at("ord_1"))
        self.assertIsNone(attempts[1].refund)

    def test_list_carries_the_refund_and_keeps_order_and_narrowing(self):
        before_all = self.store.list_payments()
        before_ok = self.store.list_payments(outcome="succeeded")
        before_declined = self.store.list_payments(outcome="declined")
        refunded = self.refund(self.paid, 9)
        after_all = self.store.list_payments()
        self.assertEqual([p.payment_reference for p in after_all.payments],
                         [p.payment_reference for p in before_all.payments])
        self.assertEqual(
            [p.payment_reference for p in self.store.list_payments(outcome="succeeded").payments],
            [p.payment_reference for p in before_ok.payments])
        self.assertEqual(self.store.list_payments(outcome="declined"), before_declined)
        self.assertIn(refunded, after_all.payments)
        self.assertEqual(refunded.outcome, "succeeded")
        self.assertIsNone(refunded.decline_reason)
        self.assertEqual([p.refund is not None for p in after_all.payments], [False, False, True, False])

    def test_paging_with_a_refund_reaches_every_payment_once_in_order(self):
        self.refund(self.other, 5)
        seen, cursor = [], None
        while True:
            page = self.store.list_payments(limit=1, cursor=cursor)
            seen += [p.payment_reference for p in page.payments]
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(seen, [self.unpaid.payment_reference, self.other.payment_reference,
                                self.paid.payment_reference, self.declined.payment_reference])

    def test_refund_holds_no_card_data(self):
        p = self.refund(self.paid)
        shown = repr(p) + repr(p.as_dict()) + repr(self.store.list_payments())
        self.assertNotIn(FULL_NUMBER, shown)
        self.assertNotIn(SECURITY_CODE, shown)
        columns = [r[1] for r in self.store._conn.execute("PRAGMA table_info(refunds)")]
        self.assertEqual(columns, ["payment_reference", "amount", "currency", "reason", "refunded_at"])
        self.assertEqual(set(p.as_dict()["card"]), {"brand", "last4"})


class RefundMigrationTest(unittest.TestCase):
    def test_a_file_from_before_refunds_gets_the_refunds_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payments.db")
            with PaymentStore(path) as store:
                p = store.record_attempt(**ok_attempt())
            conn = sqlite3.connect(path)
            conn.executescript("DROP TRIGGER refunds_match_a_succeeded_payment;"
                               "DROP TRIGGER refunds_are_not_updated;"
                               "DROP TRIGGER refunds_are_not_deleted; DROP TABLE refunds;")
            conn.close()
            with PaymentStore(path) as store:
                self.assertIsNone(store.get_payment(p.payment_reference).refund)
                self.assertTrue(store.refund_payment(p.payment_reference, "r").refund)


if __name__ == "__main__":
    unittest.main()
