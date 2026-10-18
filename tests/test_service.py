import threading
import time
import unittest

from payments.processor import DECLINE_MESSAGES, SUCCESS_MESSAGE, process_card
from payments.service import PaymentService, Refusal
from payments.store import PaymentStore

VISA_OK = "4242424242424242"
CODE = "737"


def card(number=VISA_OK, **extra):
    value = {
        "cardholder_name": "Ada Lovelace",
        "number": number,
        "expiry_month": 12,
        "expiry_year": 2099,
        "security_code": CODE,
    }
    value.update(extra)
    return value


def request(order="ord_1", amount=2599, currency="USD", number=VISA_OK, **card_extra):
    return {"order_reference": order, "amount": amount, "currency": currency, "card": card(number, **card_extra)}


class CountingProcessor:
    """The test-mode processor, counting the attempts it is asked to make."""

    def __init__(self, delay=0.0):
        self.calls = 0
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, number):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return process_card(number)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)
        self.processor = CountingProcessor()
        self.service = PaymentService(self.store, self.processor)

    def refusal(self, body, key=None):
        with self.assertRaises(Refusal) as caught:
            self.service.pay_order(body, key)
        return caught.exception

    def assertNoAttempt(self):
        self.assertEqual(self.processor.calls, 0)
        self.assertEqual(self.store.list_payments().payments, [])


class PayOrderTest(ServiceCase):
    def test_success(self):
        result = self.service.pay_order(request())
        self.assertFalse(result.replayed)
        self.assertTrue(result.order_paid)
        payment = result.payment.as_dict()
        self.assertEqual(payment["outcome"], "succeeded")
        self.assertIsNone(payment["decline_reason"])
        self.assertEqual(payment["message"], SUCCESS_MESSAGE)
        self.assertEqual((payment["order_reference"], payment["amount"], payment["currency"]), ("ord_1", 2599, "USD"))
        self.assertEqual(payment["card"], {"brand": "visa", "last4": "4242"})
        self.assertTrue(payment["payment_reference"].startswith("pay_"))
        self.assertTrue(payment["created_at"].endswith("Z"))
        self.assertEqual(result.body(), {"payment": payment, "order_paid": True})
        self.assertTrue(self.store.is_paid("ord_1"))

    def test_each_decline_reason_is_a_normal_answer_and_leaves_the_order_unpaid(self):
        numbers = {
            "card_declined": "4000000000000002",
            "insufficient_funds": "4000000000009995",
            "expired_card": "4000000000000069",
            "incorrect_card_details": "4000000000000127",
            "processing_error": "4000000000000119",
        }
        for index, (reason, number) in enumerate(numbers.items()):
            with self.subTest(reason):
                order = "ord_%d" % index
                result = self.service.pay_order(request(order=order, number=number))
                self.assertFalse(result.order_paid)
                self.assertEqual(result.payment.outcome, "declined")
                self.assertEqual(result.payment.decline_reason, reason)
                self.assertEqual(result.payment.message, DECLINE_MESSAGES[reason])
                self.assertFalse(self.service.order_status(order).paid)

    def test_another_attempt_after_a_decline_and_a_later_success_marks_the_order_paid(self):
        first = self.service.pay_order(request(number="4000000000009995"), "k1")
        second = self.service.pay_order(request(number="4000000000009995"), "k2")
        third = self.service.pay_order(request(number=VISA_OK), "k3")
        self.assertEqual([first.payment.outcome, second.payment.outcome, third.payment.outcome],
                         ["declined", "declined", "succeeded"])
        self.assertEqual(self.processor.calls, 3)
        status = self.service.order_status("ord_1")
        self.assertTrue(status.paid)
        self.assertEqual(len(status.payments), 3)

    def test_an_already_paid_order_is_refused_without_a_new_attempt(self):
        self.service.pay_order(request())
        calls = self.processor.calls
        refusal = self.refusal(request(number="5555555555554444", amount=100, currency="EUR"))
        self.assertEqual(refusal.code, "order_already_paid")
        self.assertEqual(refusal.extra, {"order_reference": "ord_1"})
        self.assertEqual(refusal.message, "This order has already been paid.")
        self.assertEqual(self.processor.calls, calls)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)

    def test_other_orders_are_not_affected_by_a_paid_order(self):
        self.service.pay_order(request(order="a"))
        self.assertTrue(self.service.pay_order(request(order="b")).order_paid)


class RefusalTest(ServiceCase):
    def test_invalid_amounts(self):
        for amount in (0, -1, 25.99, 2599.0, "2599", True, None, 2**63):
            with self.subTest(amount=amount):
                body = request()
                body["amount"] = amount
                self.assertEqual(self.refusal(body).code, "invalid_amount")
        body = request()
        del body["amount"]
        self.assertEqual(self.refusal(body).code, "invalid_amount")
        self.assertNoAttempt()

    def test_invalid_requests(self):
        for body in (None, [], "text", 5):
            with self.subTest(body=body):
                self.assertEqual(self.refusal(body).code, "invalid_request")
        for change in (
            {"order_reference": None}, {"order_reference": ""}, {"order_reference": 12},
            {"order_reference": "x" * 256}, {"currency": None}, {"currency": "usd"},
            {"currency": "US"}, {"currency": "USDX"}, {"currency": 840},
        ):
            with self.subTest(change=change):
                body = request()
                body.update(change)
                self.assertEqual(self.refusal(body).code, "invalid_request")
        for key in ("", "k" * 256):
            with self.subTest(key=len(key)):
                self.assertEqual(self.refusal(request(), key).code, "invalid_request")
        self.assertNoAttempt()

    def test_invalid_card_names_each_field_in_order(self):
        body = request(cardholder_name=" ", number="4242424242424241", expiry_year=2001, security_code="12")
        refusal = self.refusal(body)
        self.assertEqual(refusal.code, "validation_failed")
        self.assertEqual(
            [(f["field"], f["code"]) for f in refusal.extra["fields"]],
            [("cardholder_name", "required"), ("number", "invalid_number"),
             ("expiry", "expired"), ("security_code", "invalid_length")],
        )
        for item in refusal.extra["fields"]:
            self.assertEqual(set(item), {"field", "code", "message"})
        self.assertNoAttempt()

    def test_a_missing_card_names_all_four_fields(self):
        for card_value in (None, {}, "4242424242424242"):
            with self.subTest(card=card_value):
                body = request()
                body["card"] = card_value
                refusal = self.refusal(body)
                self.assertEqual(refusal.code, "validation_failed")
                self.assertEqual([f["field"] for f in refusal.extra["fields"]],
                                 ["cardholder_name", "number", "expiry", "security_code"])
        self.assertNoAttempt()

    def test_amex_needs_a_four_digit_code(self):
        refusal = self.refusal(request(number="378282246310005"))
        self.assertEqual([f["field"] for f in refusal.extra["fields"]], ["security_code"])
        self.assertTrue(self.service.pay_order(request(number="378282246310005", security_code="1234")).order_paid)

    def test_refusal_order_is_fixed(self):
        # invalid_request before invalid_amount before validation_failed before already paid
        self.service.pay_order(request())
        body = request(number="1", expiry_year=2001)
        body["amount"] = 0
        body["currency"] = "usd"
        self.assertEqual(self.refusal(body).code, "invalid_request")
        body["currency"] = "USD"
        self.assertEqual(self.refusal(body).code, "invalid_amount")
        body["amount"] = 5
        self.assertEqual(self.refusal(body).code, "validation_failed")
        body["card"] = card()
        self.assertEqual(self.refusal(body).code, "order_already_paid")

    def test_messages_never_contain_card_data(self):
        number, code = "4242 4242 4242 4241", "98765"
        refusal = self.refusal(request(number=number, security_code=code))
        text = repr((refusal.message, refusal.extra))
        for secret in (number, "4242424242424241", code):
            self.assertNotIn(secret, text)


class IdempotencyTest(ServiceCase):
    def test_a_repeated_key_replays_a_success_without_a_new_attempt(self):
        first = self.service.pay_order(request(), "key-1")
        again = self.service.pay_order(request(), "key-1")
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(again.payment, first.payment)
        self.assertTrue(again.order_paid)
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)

    def test_a_repeated_key_replays_a_decline(self):
        first = self.service.pay_order(request(number="4000000000000002"), "key-1")
        again = self.service.pay_order(request(number="4000000000000002"), "key-1")
        self.assertTrue(again.replayed)
        self.assertEqual(again.payment, first.payment)
        self.assertFalse(again.order_paid)
        self.assertEqual(self.processor.calls, 1)

    def test_a_replay_of_a_success_comes_before_the_already_paid_check(self):
        self.service.pay_order(request(), "key-1")
        self.assertTrue(self.service.pay_order(request(), "key-1").replayed)

    def test_a_new_key_after_a_decline_is_a_new_attempt(self):
        self.service.pay_order(request(number="4000000000000002"), "key-1")
        self.assertFalse(self.service.pay_order(request(), "key-2").replayed)
        self.assertEqual(self.processor.calls, 2)

    def test_the_same_key_with_a_different_request_is_refused(self):
        self.service.pay_order(request(number="4000000000000002"), "key-1")
        for body in (request(amount=1), request(order="other"), request(currency="EUR"),
                     request(cardholder_name="Someone Else"), request(expiry_month=11),
                     request(number="4000000000009995"), request(number="5555555555554444")):
            with self.subTest(body=body):
                self.assertEqual(self.refusal(body, "key-1").code, "idempotency_key_reused")
        self.assertEqual(self.processor.calls, 1)

    def test_the_security_code_and_middle_digits_are_not_part_of_the_comparison(self):
        # The digest is kept without the full card number and the security code.
        first = self.service.pay_order(request(number="4000000000000002"), "key-1")
        again = self.service.pay_order(request(number="4000000000000002", security_code="999"), "key-1")
        self.assertTrue(again.replayed)
        self.assertEqual(again.payment, first.payment)

    def test_a_refusal_is_not_remembered_under_a_key(self):
        self.assertEqual(self.refusal(request(number="1"), "key-1").code, "validation_failed")
        self.assertTrue(self.service.pay_order(request(), "key-1").order_paid)
        self.service.pay_order(request(order="paid"))
        self.assertEqual(self.refusal(request(order="paid"), "key-2").code, "order_already_paid")
        self.assertEqual(self.refusal(request(order="paid"), "key-2").code, "order_already_paid")

    def test_the_key_after_an_already_paid_refusal_is_judged_afresh(self):
        self.service.pay_order(request(order="o"))
        self.assertEqual(self.refusal(request(order="o"), "k").code, "order_already_paid")
        # Same key, now a different request: not "reused", since the refusal was not remembered.
        self.assertTrue(self.service.pay_order(request(order="o2"), "k").order_paid)

    def test_a_key_is_remembered_only_up_to_the_limit(self):
        service = PaymentService(self.store, self.processor, key_limit=2)
        for index in range(4):
            service.pay_order(request(order="o%d" % index), "k%d" % index)
        self.assertEqual(len(service._keys), 2)
        self.assertTrue(service.pay_order(request(order="o3"), "k3").replayed)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.store = PaymentStore()
        self.addCleanup(self.store.close)
        self.processor = CountingProcessor(delay=0.05)
        self.service = PaymentService(self.store, self.processor)

    def run_threads(self, bodies_and_keys):
        outcomes = [None] * len(bodies_and_keys)
        barrier = threading.Barrier(len(bodies_and_keys))

        def work(index, body, key):
            barrier.wait()
            try:
                outcomes[index] = self.service.pay_order(body, key)
            except Refusal as refusal:
                outcomes[index] = refusal

        threads = [threading.Thread(target=work, args=(i, b, k)) for i, (b, k) in enumerate(bodies_and_keys)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        return outcomes

    def test_simultaneous_payments_for_one_order_charge_it_once(self):
        outcomes = self.run_threads([(request(), "key-%d" % i) for i in range(8)])
        paid = [o for o in outcomes if not isinstance(o, Refusal)]
        refused = [o for o in outcomes if isinstance(o, Refusal)]
        self.assertEqual(len(paid), 1)
        self.assertEqual(len(refused), 7)
        self.assertTrue(all(r.code == "order_already_paid" for r in refused))
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)

    def test_simultaneous_payments_without_keys_charge_the_order_once(self):
        outcomes = self.run_threads([(request(), None)] * 6)
        self.assertEqual(sum(not isinstance(o, Refusal) for o in outcomes), 1)
        self.assertEqual(self.processor.calls, 1)

    def test_simultaneous_submits_with_one_key_make_one_attempt(self):
        outcomes = self.run_threads([(request(number="4000000000000002"), "same")] * 6)
        self.assertTrue(all(not isinstance(o, Refusal) for o in outcomes))
        self.assertEqual(sorted(o.replayed for o in outcomes), [False] + [True] * 5)
        self.assertEqual(len({o.payment.payment_reference for o in outcomes}), 1)
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)

    def test_attempts_for_one_order_never_overlap(self):
        active, worst = [0], [0]
        guard = threading.Lock()

        def processor(number):
            with guard:
                active[0] += 1
                worst[0] = max(worst[0], active[0])
            time.sleep(0.02)
            with guard:
                active[0] -= 1
            return process_card(number)

        service = PaymentService(self.store, processor)
        threads = [
            threading.Thread(target=lambda i=i: service.pay_order(request(number="4000000000000002"), "k%d" % i))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(worst[0], 1)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 5)

    def test_different_orders_run_side_by_side(self):
        started = threading.Barrier(2, timeout=5)

        def processor(number):
            started.wait()  # both attempts must be inside the processor at once
            return process_card(number)

        service = PaymentService(self.store, processor)
        results = []
        threads = [
            threading.Thread(target=lambda o=o: results.append(service.pay_order(request(order=o))))
            for o in ("a", "b")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 2)

    def test_a_failure_does_not_leave_a_key_or_a_lock_behind(self):
        calls = []

        def processor(number):
            calls.append(number)
            if len(calls) == 1:
                raise RuntimeError("provider down")
            return process_card(number)

        service = PaymentService(self.store, processor)
        with self.assertRaises(RuntimeError):
            service.pay_order(request(), "k")
        self.assertEqual(self.store.attempts_for_order("ord_1"), [])
        self.assertTrue(service.pay_order(request(), "k").order_paid)
        self.assertEqual(service._order_locks._locks, {})


class StatusAndListTest(ServiceCase):
    def test_status_of_an_unknown_order_is_unpaid_and_empty(self):
        status = self.service.order_status("nope")
        self.assertEqual(status.body(), {"order_reference": "nope", "paid": False, "payments": []})

    def test_status_lists_attempts_newest_first(self):
        self.service.pay_order(request(number="4000000000000002"), "k1")
        self.service.pay_order(request(number="4000000000009995"), "k2")
        self.service.pay_order(request(), "k3")
        body = self.service.order_status("ord_1").body()
        self.assertTrue(body["paid"])
        self.assertEqual([p["outcome"] for p in body["payments"]], ["succeeded", "declined", "declined"])
        self.assertEqual([p["decline_reason"] for p in body["payments"]], [None, "insufficient_funds", "card_declined"])

    def test_list_is_newest_first_narrowed_and_paged(self):
        for index in range(5):
            number = VISA_OK if index % 2 == 0 else "4000000000000002"
            self.service.pay_order(request(order="o%d" % index, number=number))
        everything = self.service.list_payments()
        self.assertEqual([p.order_reference for p in everything.payments], ["o4", "o3", "o2", "o1", "o0"])
        self.assertIsNone(everything.next_cursor)
        self.assertEqual(len(self.service.list_payments(outcome="declined").payments), 2)
        self.assertEqual(len(self.service.list_payments(outcome="succeeded").payments), 3)
        seen, cursor = [], None
        while True:
            page = self.service.list_payments(limit=2, cursor=cursor)
            seen += [p.order_reference for p in page.payments]
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(seen, ["o4", "o3", "o2", "o1", "o0"])

    def test_list_refuses_bad_filters(self):
        for kwargs in ({"outcome": "paid"}, {"outcome": ""}, {"limit": 0}, {"limit": 501},
                       {"limit": "5"}, {"cursor": "not-a-cursor"}, {"cursor": ""}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(Refusal) as caught:
                    self.service.list_payments(**kwargs)
                self.assertEqual(caught.exception.code, "invalid_request")


class NoCardDataTest(ServiceCase):
    def test_nothing_returned_or_stored_holds_the_card_number_or_the_security_code(self):
        number, code = "4000 0000 0000 9995", "654"
        digits = number.replace(" ", "")
        results = [
            self.service.pay_order(request(number=number, security_code=code), "k1"),
            self.service.pay_order(request(order="o2", number=VISA_OK, security_code=code)),
        ]
        shown = repr([r.body() for r in results])
        shown += repr(self.service.order_status("ord_1").body())
        shown += repr([p.as_dict() for p in self.service.list_payments().payments])
        dump = "\n".join(self.store._conn.iterdump())
        shown += dump
        shown += repr(self.service._keys)
        for secret in (digits, number, VISA_OK, "4242 4242 4242 4242"):
            self.assertNotIn(secret, shown)
        self.assertNotIn("'%s'" % code, shown)
        self.assertNotIn('"%s"' % code, shown)
        columns = [row[1] for row in self.store._conn.execute("PRAGMA table_info(payments)")]
        self.assertFalse({"number", "card_number", "security_code", "cvv"} & set(columns))


if __name__ == "__main__":
    unittest.main()
