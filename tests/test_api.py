import contextlib
import http.client
import io
import json
import threading
import time
import unittest
import urllib.parse

from payments.api import make_server
from payments.processor import DECLINE_MESSAGES, process_card
from payments.service import PaymentService
from payments.store import PaymentStore

VISA_OK = "4242424242424242"
CODE = "737"


def body(order="ord_1", amount=2599, currency="USD", number=VISA_OK, **card_extra):
    card = {"cardholder_name": "Ada Lovelace", "number": number, "expiry_month": 12,
            "expiry_year": 2099, "security_code": CODE}
    card.update(card_extra)
    return {"order_reference": order, "amount": amount, "currency": currency, "card": card}


class Response:
    def __init__(self, raw):
        self.status = raw.status
        self.headers = raw.headers
        self.raw_body = raw.read()
        self.json = json.loads(self.raw_body) if self.raw_body else None


class ApiCase(unittest.TestCase):
    delay = 0.0

    def setUp(self):
        self.store = PaymentStore()
        self.calls = 0
        self.calls_lock = threading.Lock()

        def processor(number):
            with self.calls_lock:
                self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            return process_card(number)

        self.server = make_server(PaymentService(self.store, processor), "127.0.0.1", 0, quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.store.close()

    def call(self, method, path, payload=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = raw if raw is not None else (None if payload is None else json.dumps(payload))
            all_headers = {"Content-Type": "application/json"} if data is not None else {}
            all_headers.update(headers or {})
            conn.request(method, path, body=data, headers=all_headers)
            return Response(conn.getresponse())
        finally:
            conn.close()

    def pay(self, payload, key=None, **kw):
        return self.call("POST", "/payments", payload, {"Idempotency-Key": key} if key else None, **kw)

    def assertCors(self, response):
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response.headers["Access-Control-Expose-Headers"], "Idempotency-Replayed")

    def assertError(self, response, status, code):
        self.assertEqual(response.status, status)
        self.assertEqual(response.json["error"]["code"], code)
        self.assertIsInstance(response.json["error"]["message"], str)
        self.assertNotIn("payment", response.json)
        self.assertCors(response)


class PayOrderApiTest(ApiCase):
    def test_success_is_201_with_the_payment(self):
        response = self.pay(body())
        self.assertEqual(response.status, 201)
        self.assertCors(response)
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        self.assertNotIn("Idempotency-Replayed", response.headers)
        self.assertTrue(response.json["order_paid"])
        payment = response.json["payment"]
        self.assertEqual(set(payment), {"payment_reference", "order_reference", "amount", "currency", "outcome",
                                        "decline_reason", "message", "card", "created_at"})
        self.assertEqual(payment["outcome"], "succeeded")
        self.assertEqual(payment["amount"], 2599)
        self.assertEqual(payment["card"], {"brand": "visa", "last4": "4242"})

    def test_declines_are_201_normal_answers_with_a_reason_and_message(self):
        cards = {"card_declined": "4000000000000002", "insufficient_funds": "4000000000009995",
                 "expired_card": "4000000000000069", "incorrect_card_details": "4000000000000127",
                 "processing_error": "4000000000000119"}
        for index, (reason, number) in enumerate(cards.items()):
            with self.subTest(reason):
                response = self.pay(body(order="o%d" % index, number=number))
                self.assertEqual(response.status, 201)
                self.assertFalse(response.json["order_paid"])
                self.assertEqual(response.json["payment"]["outcome"], "declined")
                self.assertEqual(response.json["payment"]["decline_reason"], reason)
                self.assertEqual(response.json["payment"]["message"], DECLINE_MESSAGES[reason])

    def test_retry_after_a_decline_then_success_marks_the_order_paid(self):
        self.assertEqual(self.pay(body(number="4000000000009995"), "a").json["payment"]["outcome"], "declined")
        self.assertEqual(self.pay(body(), "b").json["payment"]["outcome"], "succeeded")
        status = self.call("GET", "/orders/ord_1/payments").json
        self.assertTrue(status["paid"])
        self.assertEqual([p["outcome"] for p in status["payments"]], ["succeeded", "declined"])

    def test_a_paid_order_is_refused_with_409_and_no_second_charge(self):
        self.pay(body())
        response = self.pay(body(), "other-key")
        self.assertError(response, 409, "order_already_paid")
        self.assertEqual(response.json["error"]["order_reference"], "ord_1")
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.call("GET", "/orders/ord_1/payments").json["payments"]), 1)

    def test_invalid_card_is_422_naming_each_field_and_no_attempt(self):
        response = self.pay(body(cardholder_name="", number="4242424242424241", expiry_year=2001, security_code="1"))
        self.assertError(response, 422, "validation_failed")
        self.assertEqual([(f["field"], f["code"]) for f in response.json["error"]["fields"]],
                         [("cardholder_name", "required"), ("number", "invalid_number"),
                          ("expiry", "expired"), ("security_code", "invalid_length")])
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.call("GET", "/payments").json["payments"], [])

    def test_invalid_amount_is_422(self):
        for amount in (0, -5, 1.5, "10", None):
            with self.subTest(amount=amount):
                self.assertError(self.pay(body(amount=amount)), 422, "invalid_amount")
        self.assertEqual(self.calls, 0)

    def test_invalid_request_is_400(self):
        self.assertError(self.pay(None, raw="{not json"), 400, "invalid_request")
        self.assertError(self.pay(None, raw="[1, 2]"), 400, "invalid_request")
        self.assertError(self.pay(None, raw=b"\xff\xfe"), 400, "invalid_request")
        self.assertError(self.pay(None, raw=""), 400, "invalid_request")
        self.assertError(self.pay(body(currency="usd")), 400, "invalid_request")
        self.assertError(self.pay(body(order="")), 400, "invalid_request")
        self.assertError(self.pay(body(), key="x" * 256), 400, "invalid_request")
        self.assertError(self.pay(None, raw="1" * 70000), 400, "invalid_request")
        self.assertEqual(self.calls, 0)

    def test_repeated_key_replays_with_200_and_the_header(self):
        first = self.pay(body(number="4000000000000002"), "key-1")
        again = self.pay(body(number="4000000000000002"), "key-1")
        self.assertEqual((first.status, again.status), (201, 200))
        self.assertEqual(again.headers["Idempotency-Replayed"], "true")
        self.assertCors(again)
        self.assertEqual(again.json, first.json)
        self.assertEqual(self.calls, 1)

    def test_replay_of_a_success_is_200_not_409(self):
        self.pay(body(), "key-1")
        again = self.pay(body(), "key-1")
        self.assertEqual(again.status, 200)
        self.assertTrue(again.json["order_paid"])

    def test_key_reused_for_a_different_request_is_422(self):
        self.pay(body(), "key-1")
        self.assertError(self.pay(body(amount=1), "key-1"), 422, "idempotency_key_reused")
        self.assertEqual(self.calls, 1)

    def test_refusal_is_not_replayed(self):
        self.assertError(self.pay(body(number="1"), "key-1"), 422, "validation_failed")
        self.assertEqual(self.pay(body(), "key-1").status, 201)

    def test_responses_do_not_contain_card_data(self):
        number, code = "4000 0000 0000 9995", "654"
        responses = [
            self.pay(body(number=number, security_code=code), "k"),
            self.pay(body(number=number, security_code=code), "k"),
            self.pay(body(number="4242 4242 4242 4241", security_code=code)),
            self.call("GET", "/orders/ord_1/payments"),
            self.call("GET", "/payments"),
        ]
        text = b"".join(r.raw_body for r in responses).decode()
        for secret in ("4000000000009995", number, "4242424242424241", "4242 4242 4242 4241", '"%s"' % code):
            self.assertNotIn(secret, text)

    def test_two_requests_over_one_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        for order in ("a", "b"):
            conn.request("POST", "/payments", json.dumps(body(order=order)), {"Content-Type": "application/json"})
            self.assertEqual(Response(conn.getresponse()).status, 201)
        conn.request("POST", "/nope", json.dumps(body()), {"Content-Type": "application/json"})
        self.assertEqual(Response(conn.getresponse()).status, 404)
        conn.request("GET", "/payments")
        self.assertEqual(len(Response(conn.getresponse()).json["payments"]), 2)


class DoubleSubmitApiTest(ApiCase):
    delay = 0.05

    def submit_together(self, count, key_for):
        responses = [None] * count
        barrier = threading.Barrier(count)

        def work(index):
            barrier.wait()
            responses[index] = self.pay(body(), key_for(index))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        return responses

    def test_a_double_click_with_one_key_is_one_attempt(self):
        responses = self.submit_together(5, lambda i: "same")
        self.assertEqual(sorted(r.status for r in responses), [200, 200, 200, 200, 201])
        self.assertEqual(len({r.json["payment"]["payment_reference"] for r in responses}), 1)
        self.assertEqual(self.calls, 1)

    def test_a_resubmit_with_new_keys_charges_once(self):
        responses = self.submit_together(5, lambda i: "key-%d" % i)
        self.assertEqual(sorted(r.status for r in responses), [201, 409, 409, 409, 409])
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.call("GET", "/orders/ord_1/payments").json["payments"]), 1)


class StatusApiTest(ApiCase):
    def test_unknown_order_is_unpaid_with_an_empty_list(self):
        response = self.call("GET", "/orders/nothing/payments")
        self.assertEqual(response.status, 200)
        self.assertCors(response)
        self.assertEqual(response.json, {"order_reference": "nothing", "paid": False, "payments": []})

    def test_order_reference_is_url_decoded(self):
        reference = "ord/1 é#"
        self.pay(body(order=reference))
        response = self.call("GET", "/orders/%s/payments" % urllib.parse.quote(reference, safe=""))
        self.assertEqual(response.json["order_reference"], reference)
        self.assertTrue(response.json["paid"])

    def test_lists_only_that_order_newest_first(self):
        self.pay(body(order="a", number="4000000000000002"), "1")
        self.pay(body(order="b"))
        self.pay(body(order="a"), "2")
        payments = self.call("GET", "/orders/a/payments").json["payments"]
        self.assertEqual([p["outcome"] for p in payments], ["succeeded", "declined"])
        self.assertEqual({p["order_reference"] for p in payments}, {"a"})


class ListApiTest(ApiCase):
    def fill(self, count=5):
        for index in range(count):
            number = VISA_OK if index % 2 == 0 else "4000000000000002"
            self.pay(body(order="o%d" % index, number=number))

    def test_newest_first_and_narrowed_by_outcome(self):
        self.fill()
        response = self.call("GET", "/payments")
        self.assertEqual(response.status, 200)
        self.assertCors(response)
        self.assertIsNone(response.json["next_cursor"])
        self.assertEqual([p["order_reference"] for p in response.json["payments"]], ["o4", "o3", "o2", "o1", "o0"])
        declined = self.call("GET", "/payments?outcome=declined").json["payments"]
        self.assertEqual([p["order_reference"] for p in declined], ["o3", "o1"])
        succeeded = self.call("GET", "/payments?outcome=succeeded").json["payments"]
        self.assertEqual(len(succeeded), 3)

    def test_empty_list(self):
        self.assertEqual(self.call("GET", "/payments").json, {"payments": [], "next_cursor": None})

    def test_paging_reaches_the_oldest(self):
        self.fill()
        seen, query = [], "/payments?limit=2"
        for _ in range(10):
            page = self.call("GET", query).json
            seen += [p["order_reference"] for p in page["payments"]]
            if page["next_cursor"] is None:
                break
            query = "/payments?limit=2&cursor=" + urllib.parse.quote(page["next_cursor"])
        self.assertEqual(seen, ["o4", "o3", "o2", "o1", "o0"])

    def test_bad_query_is_400(self):
        for query in ("outcome=paid", "outcome=", "limit=0", "limit=501", "limit=abc", "limit=-1", "limit=1.5",
                      "limit=99999999999999999999", "cursor=garbage", "cursor=", "outcome=declined&outcome=succeeded",
                      "limit"):
            with self.subTest(query=query):
                self.assertError(self.call("GET", "/payments?" + query), 400, "invalid_request")


class RoutingApiTest(ApiCase):
    def test_unknown_paths_are_404(self):
        for method, path in (("GET", "/"), ("GET", "/nope"), ("POST", "/payments/1"), ("GET", "/payments/"),
                             ("GET", "/orders//payments"), ("GET", "/orders/a/b/payments"), ("OPTIONS", "/nope")):
            with self.subTest(method=method, path=path):
                self.assertError(self.call(method, path), 404, "not_found")

    def test_other_methods_are_405_with_allow(self):
        for method, path, allow in (("PUT", "/payments", "GET, POST, OPTIONS"),
                                    ("DELETE", "/payments", "GET, POST, OPTIONS"),
                                    ("POST", "/orders/a/payments", "GET, OPTIONS"),
                                    ("PATCH", "/orders/a/payments", "GET, OPTIONS")):
            with self.subTest(method=method, path=path):
                response = self.call(method, path, {"x": 1})
                self.assertError(response, 405, "method_not_allowed")
                self.assertEqual(response.headers["Allow"], allow)

    def test_preflight_is_204_on_both_paths(self):
        for path in ("/payments", "/orders/a%20b/payments"):
            with self.subTest(path=path):
                response = self.call("OPTIONS", path, headers={
                    "Origin": "http://shop.example", "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type, idempotency-key"})
                self.assertEqual(response.status, 204)
                self.assertEqual(response.raw_body, b"")
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")
                self.assertEqual(response.headers["Access-Control-Allow-Headers"], "Content-Type, Idempotency-Key")
                self.assertEqual(response.headers["Access-Control-Max-Age"], "600")

    def test_unsupported_methods_get_the_standard_body(self):
        self.assertError(self.call("TRACE", "/payments"), 405, "method_not_allowed")


class InternalErrorApiTest(ApiCase):
    def test_an_unexpected_failure_is_500_with_the_standard_body_and_no_card_data(self):
        def broken(number):
            raise RuntimeError("provider exploded for " + number)

        service = PaymentService(self.store, broken)
        server = make_server(service, "127.0.0.1", 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
        self.addCleanup(stop)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(io.StringIO()):
            conn.request("POST", "/payments", json.dumps(body()), {"Content-Type": "application/json"})
            response = Response(conn.getresponse())
        self.assertError(response, 500, "internal_error")
        self.assertNotIn(VISA_OK, response.raw_body.decode())
        self.assertEqual(self.store.list_payments().payments, [])


if __name__ == "__main__":
    unittest.main()
