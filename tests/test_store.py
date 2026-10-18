import datetime
import os
import sqlite3
import tempfile
import threading
import unittest

from payments.cards import mask_card
from payments.processor import DECLINE_MESSAGES, DECLINED, SUCCEEDED, SUCCESS_MESSAGE
from payments.store import InvalidCursor, OrderAlreadyPaid, PaymentStore

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


if __name__ == "__main__":
    unittest.main()
