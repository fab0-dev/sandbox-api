import contextlib
import http.client
import io
import json
import socket
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
                                        "decline_reason", "message", "card", "created_at", "refund"})
        self.assertIsNone(payment["refund"])
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
        self.assertEqual(response.json, {"order_reference": "nothing", "status": "unpaid", "paid": False,
                                         "refunded_at": None, "payments": []})

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
                             ("GET", "/orders//payments"), ("GET", "/orders/a/b/payments"), ("OPTIONS", "/nope"),
                             ("GET", "/payments/pay_1"), ("POST", "/payments/pay_1/refunds"),
                             ("POST", "/payments//refund"), ("POST", "/payments/a/b/refund")):
            with self.subTest(method=method, path=path):
                self.assertError(self.call(method, path), 404, "not_found")

    def test_other_methods_are_405_with_allow(self):
        for method, path, allow in (("PUT", "/payments", "GET, POST, OPTIONS"),
                                    ("DELETE", "/payments", "GET, POST, OPTIONS"),
                                    ("POST", "/orders/a/payments", "GET, OPTIONS"),
                                    ("PATCH", "/orders/a/payments", "GET, OPTIONS"),
                                    ("GET", "/payments/pay_1/refund", "POST, OPTIONS"),
                                    ("PUT", "/payments/pay_1/refund", "POST, OPTIONS"),
                                    ("DELETE", "/payments/pay_1/refund", "POST, OPTIONS")):
            with self.subTest(method=method, path=path):
                response = self.call(method, path, {"x": 1})
                self.assertError(response, 405, "method_not_allowed")
                self.assertEqual(response.headers["Allow"], allow)

    def test_preflight_is_204_on_all_three_paths(self):
        for path in ("/payments", "/orders/a%20b/payments", "/payments/pay_1/refund"):
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


class CrossOriginApiTest(ApiCase):
    """What a browser sends from another origin, and what it needs back (D1, D9)."""

    ORIGIN = "http://checkout.example:3000"
    PAY = "/payments"
    STATUS = "/orders/ord_1/payments"

    def browser(self, method, path, payload=None, headers=None, raw=None, origin=None):
        sent = {"Origin": origin or self.ORIGIN}
        sent.update(headers or {})
        return self.call(method, path, payload, sent, raw=raw)

    def preflight(self, path, method="POST", request_headers="content-type,idempotency-key", origin=None):
        sent = {}
        if method is not None:
            sent["Access-Control-Request-Method"] = method
        if request_headers is not None:
            sent["Access-Control-Request-Headers"] = request_headers
        return self.browser("OPTIONS", path, headers=sent, origin=origin)

    def assertOnce(self, response, name, value):
        self.assertEqual(response.headers.get_all(name), [value], name)

    def assertAnswerForBrowser(self, response, replayed=False):
        self.assertOnce(response, "Access-Control-Allow-Origin", "*")
        self.assertOnce(response, "Access-Control-Expose-Headers", "Idempotency-Replayed")
        self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))
        if replayed:
            self.assertOnce(response, "Idempotency-Replayed", "true")
        else:
            self.assertIsNone(response.headers.get("Idempotency-Replayed"))

    def assertPreflight(self, response):
        self.assertEqual(response.status, 204)
        self.assertEqual(response.raw_body, b"")
        self.assertOnce(response, "Access-Control-Allow-Origin", "*")
        self.assertOnce(response, "Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.assertOnce(response, "Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.assertOnce(response, "Access-Control-Max-Age", "600")
        self.assertOnce(response, "Access-Control-Expose-Headers", "Idempotency-Replayed")
        self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))

    def assertBrowserWouldProceed(self, response, method, request_headers):
        """The check a browser makes on a preflight answer before the real request."""
        self.assertTrue(200 <= response.status < 300)
        self.assertIn(response.headers["Access-Control-Allow-Origin"], ("*", self.ORIGIN))
        allowed_methods = {m.strip() for m in response.headers["Access-Control-Allow-Methods"].split(",")}
        self.assertIn(method, allowed_methods)
        allowed = {h.strip().lower() for h in response.headers["Access-Control-Allow-Headers"].split(",")}
        for name in request_headers.split(","):
            self.assertIn(name.strip().lower(), allowed)

    def broken_status_server(self):
        class BrokenStore:
            def attempts_for_order(self, order_reference):
                raise RuntimeError("store exploded")

        server = make_server(PaymentService(BrokenStore(), process_card), "127.0.0.1", 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
        self.addCleanup(stop)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        return conn

    # -- the preflight -----------------------------------------------------

    def test_preflight_for_pay_order_as_a_browser_sends_it(self):
        response = self.preflight(self.PAY, "POST", "content-type,idempotency-key")
        self.assertPreflight(response)
        self.assertBrowserWouldProceed(response, "POST", "content-type,idempotency-key")

    def test_preflight_for_order_status_as_a_browser_sends_it(self):
        for path in (self.STATUS, "/orders/ord%2F1%20x/payments"):
            with self.subTest(path=path):
                response = self.preflight(path, "GET", "content-type,idempotency-key")
                self.assertPreflight(response)
                self.assertBrowserWouldProceed(response, "GET", "content-type,idempotency-key")

    def test_requested_headers_in_any_case_order_and_spacing(self):
        for requested in ("content-type,idempotency-key", "idempotency-key,content-type",
                          "Content-Type, Idempotency-Key", "IDEMPOTENCY-KEY , CONTENT-TYPE",
                          "Idempotency-Key", "content-type", "Idempotency-key,Content-type"):
            for path, method in ((self.PAY, "POST"), (self.STATUS, "GET")):
                with self.subTest(requested=requested, path=path):
                    response = self.preflight(path, method, requested)
                    self.assertPreflight(response)
                    self.assertBrowserWouldProceed(response, method, requested)

    def test_the_preflight_answer_does_not_depend_on_what_is_requested(self):
        for method, requested in ((None, None), ("PUT", "x-other"), ("post", "content-type"), ("DELETE", "")):
            for path in (self.PAY, self.STATUS):
                with self.subTest(method=method, requested=requested, path=path):
                    self.assertPreflight(self.preflight(path, method, requested))

    def test_the_preflight_answers_for_any_origin(self):
        for origin in ("http://localhost:5173", "https://shop.example", "null", "http://127.0.0.1:3000"):
            with self.subTest(origin=origin):
                self.assertPreflight(self.preflight(self.PAY, origin=origin))

    def test_the_preflight_ignores_a_body_and_a_query(self):
        response = self.browser("OPTIONS", "/payments?x=1", raw="not json", headers={
            "Access-Control-Request-Method": "POST"})
        self.assertPreflight(response)

    def test_the_preflight_is_repeatable_on_one_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        for _ in range(3):
            conn.request("OPTIONS", self.PAY, headers={
                "Origin": self.ORIGIN, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,idempotency-key"})
            self.assertPreflight(Response(conn.getresponse()))

    def test_the_preflight_on_an_unknown_path_is_404_with_the_origin_header(self):
        response = self.preflight("/nope", "POST")
        self.assertError(response, 404, "not_found")
        self.assertAnswerForBrowser(response)

    def test_the_preflight_records_nothing_and_charges_nothing(self):
        self.preflight(self.PAY)
        self.preflight(self.STATUS, "GET")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.list_payments().payments, [])

    # -- every answer to a real request -------------------------------------

    def test_every_answer_to_pay_order_carries_the_headers(self):
        answers = []
        first = self.browser("POST", self.PAY, body(order="ord_ok"), {"Idempotency-Key": "k-1"})
        self.assertEqual(first.status, 201)
        answers.append((first, False))
        replay = self.browser("POST", self.PAY, body(order="ord_ok"), {"Idempotency-Key": "k-1"})
        self.assertEqual(replay.status, 200)
        answers.append((replay, True))
        declined = self.browser("POST", self.PAY, body(order="ord_dec", number="4000000000000002"))
        self.assertEqual((declined.status, declined.json["payment"]["outcome"]), (201, "declined"))
        answers.append((declined, False))
        for label, response, status, code in (
            ("409", self.browser("POST", self.PAY, body(order="ord_ok")), 409, "order_already_paid"),
            ("422 fields", self.browser("POST", self.PAY, body(order="o2", number="4242424242424241")), 422,
             "validation_failed"),
            ("422 amount", self.browser("POST", self.PAY, body(order="o3", amount=0)), 422, "invalid_amount"),
            ("422 key", self.browser("POST", self.PAY, body(order="ord_ok", amount=1), {"Idempotency-Key": "k-1"}),
             422, "idempotency_key_reused"),
            ("400 json", self.browser("POST", self.PAY, raw="{"), 400, "invalid_request"),
            ("400 shape", self.browser("POST", self.PAY, body(currency="usd")), 400, "invalid_request"),
            ("400 big", self.browser("POST", self.PAY, raw="1" * 70000), 400, "invalid_request"),
            ("405", self.browser("PUT", self.PAY, {"x": 1}), 405, "method_not_allowed"),
            ("404", self.browser("POST", "/payment", body()), 404, "not_found"),
        ):
            with self.subTest(label):
                self.assertError(response, status, code)
                answers.append((response, False))
        for response, replayed in answers:
            with self.subTest(status=response.status, replayed=replayed):
                self.assertAnswerForBrowser(response, replayed)

    def test_every_answer_to_pay_order_with_a_failing_service_carries_the_headers(self):
        def broken(number):
            raise RuntimeError("provider exploded")

        server = make_server(PaymentService(self.store, broken), "127.0.0.1", 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(io.StringIO()):
            conn.request("POST", "/payments", json.dumps(body()),
                         {"Content-Type": "application/json", "Origin": self.ORIGIN, "Idempotency-Key": "k"})
            response = Response(conn.getresponse())
        self.assertError(response, 500, "internal_error")
        self.assertAnswerForBrowser(response)

    def test_every_answer_to_order_status_carries_the_headers(self):
        self.browser("POST", self.PAY, body(order="ord_1"))
        for label, response, status in (
            ("paid", self.browser("GET", self.STATUS), 200),
            ("unknown order", self.browser("GET", "/orders/nobody/payments"), 200),
            ("405", self.browser("POST", self.STATUS, {"x": 1}), 405),
            ("405 delete", self.browser("DELETE", self.STATUS), 405),
            ("404", self.browser("GET", "/orders/ord_1/payment"), 404),
        ):
            with self.subTest(label):
                self.assertEqual(response.status, status)
                self.assertAnswerForBrowser(response)
        self.assertTrue(self.browser("GET", self.STATUS).json["paid"])

    def test_a_failing_order_status_is_500_with_the_headers(self):
        conn = self.broken_status_server()
        with contextlib.redirect_stderr(io.StringIO()):
            conn.request("GET", self.STATUS, headers={"Origin": self.ORIGIN})
            response = Response(conn.getresponse())
        self.assertError(response, 500, "internal_error")
        self.assertAnswerForBrowser(response)

    def test_list_payments_and_its_refusals_carry_the_headers_too(self):
        self.browser("POST", self.PAY, body())
        for path, status in (("/payments", 200), ("/payments?outcome=declined", 200), ("/payments?limit=0", 400)):
            with self.subTest(path=path):
                response = self.browser("GET", path)
                self.assertEqual(response.status, status)
                self.assertAnswerForBrowser(response)

    def test_answers_carry_the_headers_without_an_origin_header_too(self):
        # A same-origin or non-browser caller gets the same answer.
        self.assertAnswerForBrowser(self.call("GET", self.STATUS))
        self.assertAnswerForBrowser(self.pay(body()))

    def test_errors_found_by_the_server_itself_carry_the_headers(self):
        for label, raw in (("bad request line", b"GET /payments HTTP/x.y\r\n\r\n"),
                           ("header too large", b"GET /payments HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 70000 + b"\r\n\r\n")):
            with self.subTest(label):
                with socket.create_connection(("127.0.0.1", self.port), timeout=10) as conn:
                    conn.sendall(raw)
                    reply = http.client.HTTPResponse(conn)
                    reply.begin()
                    response = Response(reply)
                self.assertEqual(response.status // 100, 4)
                self.assertAnswerForBrowser(response)

    def test_the_replay_header_is_exposed_so_script_can_read_it(self):
        pay = body(number="4000000000000002")
        first = self.browser("POST", self.PAY, pay, {"Idempotency-Key": "k-replay"})
        again = self.browser("POST", self.PAY, pay, {"Idempotency-Key": "k-replay"})
        self.assertEqual((first.status, again.status), (201, 200))
        exposed = {h.strip().lower() for h in again.headers["Access-Control-Expose-Headers"].split(",")}
        self.assertIn("idempotency-replayed", exposed)
        self.assertEqual(again.headers["Idempotency-Replayed"], "true")
        self.assertEqual(again.json, first.json)
        self.assertEqual(len(self.store.attempts_for_order("ord_1")), 1)

    def test_a_whole_browser_exchange_preflight_then_request_then_replay(self):
        requested = "content-type,idempotency-key"
        pre = self.preflight(self.PAY, "POST", requested)
        self.assertPreflight(pre)
        self.assertBrowserWouldProceed(pre, "POST", requested)
        first = self.browser("POST", self.PAY, body(), {"Idempotency-Key": "k-flow"})
        again = self.browser("POST", self.PAY, body(), {"Idempotency-Key": "k-flow"})
        self.assertAnswerForBrowser(first)
        self.assertAnswerForBrowser(again, replayed=True)
        self.assertEqual(again.json, first.json)
        status = self.browser("GET", self.STATUS)
        self.assertAnswerForBrowser(status)
        self.assertEqual(len(status.json["payments"]), 1)


class RefundApiCase(ApiCase):
    def refund(self, payment_reference, payload=None, origin=None, **kw):
        headers = {"Origin": origin} if origin else None
        return self.call("POST", "/payments/%s/refund" % urllib.parse.quote(payment_reference, safe=""),
                         payload, headers, **kw)

    def make_payment(self, order="ord_1", number=VISA_OK, key=None):
        return self.pay(body(order=order, number=number), key).json["payment"]

    def state(self):
        return (self.call("GET", "/payments").json, self.calls)


class RefundPaymentApiTest(RefundApiCase):
    def test_a_refund_is_200_with_the_payment_and_its_refund(self):
        payment = self.make_payment()
        response = self.refund(payment["payment_reference"], {"reason": "Out of stock"})
        self.assertEqual(response.status, 200)
        self.assertCors(response)
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        self.assertNotIn("Idempotency-Replayed", response.headers)
        self.assertEqual(set(response.json), {"payment"})
        shown = response.json["payment"]
        self.assertEqual(shown["outcome"], "succeeded")
        self.assertIsNone(shown["decline_reason"])
        self.assertEqual(set(shown["refund"]), {"amount", "currency", "reason", "refunded_at"})
        self.assertEqual((shown["refund"]["amount"], shown["refund"]["currency"]), (2599, "USD"))
        self.assertEqual(shown["refund"]["reason"], "Out of stock")
        self.assertRegex(shown["refund"]["refunded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(dict(shown, refund=None), payment)

    def test_the_reason_is_kept_as_given_with_unicode_and_the_reference_is_url_decoded(self):
        payment = self.make_payment()
        reason = " Rupture de stock \u2013 \u00e9t\u00e9 "
        response = self.refund(payment["payment_reference"], {"reason": reason})
        self.assertEqual(response.json["payment"]["refund"]["reason"], reason)

    def test_a_reason_of_exactly_500_characters_is_accepted(self):
        payment = self.make_payment()
        self.assertEqual(self.refund(payment["payment_reference"], {"reason": "x" * 500}).status, 200)

    def test_a_missing_or_blank_reason_is_422_reason_required_and_nothing_changes(self):
        payment = self.make_payment()
        before = self.state()
        for label, payload, raw in (("no body", None, None), ("empty object", {}, None),
                                    ("null", {"reason": None}, None), ("empty", {"reason": ""}, None),
                                    ("blank", {"reason": " \t "}, None), ("number", {"reason": 3}, None),
                                    ("raw empty body with a content type", None, b"")):
            with self.subTest(label):
                response = self.refund(payment["payment_reference"], payload, raw=raw)
                self.assertError(response, 422, "reason_required")
                self.assertEqual(response.json["error"]["message"], "Give the reason for the refund.")
        self.assertEqual(self.state(), before)

    def test_a_reason_over_500_characters_is_422_reason_too_long(self):
        payment = self.make_payment()
        before = self.state()
        response = self.refund(payment["payment_reference"], {"reason": "x" * 501})
        self.assertError(response, 422, "reason_too_long")
        self.assertEqual(response.json["error"]["message"], "The reason can be at most 500 characters.")
        self.assertEqual(self.state(), before)

    def test_a_body_that_is_not_json_or_not_an_object_or_too_large_is_400(self):
        payment = self.make_payment()
        before = self.state()
        for label, kwargs in (("not json", {"raw": "{reason"}), ("array", {"payload": ["x"]}),
                              ("string", {"payload": "Out of stock"}), ("null", {"raw": "null"}),
                              ("bad utf-8", {"raw": b"\xff\xfe"}), ("whitespace only", {"raw": "  "}),
                              ("too large", {"raw": '{"reason": "%s"}' % ("x" * 70000)})):
            with self.subTest(label):
                response = self.refund(payment["payment_reference"], **kwargs)
                self.assertError(response, 400, "invalid_request")
        self.assertEqual(self.state(), before)

    def test_an_unknown_payment_is_404_payment_not_found_and_nothing_is_recorded(self):
        self.make_payment()
        before = self.state()
        response = self.refund("pay_nope", {"reason": "Out of stock"})
        self.assertError(response, 404, "payment_not_found")
        self.assertEqual(response.json["error"]["message"], "There is no payment with this reference.")
        self.assertEqual(response.json["error"]["payment_reference"], "pay_nope")
        self.assertEqual(self.state(), before)

    def test_payment_not_found_is_told_apart_from_a_path_without_a_route(self):
        unknown_payment = self.refund("pay_nope", {"reason": "r"})
        no_route = self.call("GET", "/payments/pay_nope")
        self.assertEqual(unknown_payment.json["error"]["code"], "payment_not_found")
        self.assertEqual(no_route.json["error"]["code"], "not_found")

    def test_a_declined_payment_is_409_payment_declined_and_nothing_changes(self):
        declined = self.make_payment(number="4000000000000002")
        before = self.state()
        response = self.refund(declined["payment_reference"], {"reason": "Out of stock"})
        self.assertError(response, 409, "payment_declined")
        self.assertEqual(response.json["error"]["message"],
                         "This payment was declined, so it took no money and there is nothing to refund.")
        self.assertEqual(response.json["error"]["payment_reference"], declined["payment_reference"])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.call("GET", "/orders/ord_1/payments").json["status"], "unpaid")

    def test_a_second_refund_is_409_payment_already_refunded_with_the_first_time(self):
        payment = self.make_payment()
        first = self.refund(payment["payment_reference"], {"reason": "Out of stock"}).json["payment"]
        before = self.state()
        response = self.refund(payment["payment_reference"], {"reason": "Customer cancelled"})
        self.assertError(response, 409, "payment_already_refunded")
        error = response.json["error"]
        self.assertEqual(error["refunded_at"], first["refund"]["refunded_at"])
        self.assertEqual(error["payment_reference"], payment["payment_reference"])
        self.assertEqual(error["message"], "This payment was already refunded at %s." % error["refunded_at"])
        self.assertEqual(self.state(), before)
        listed = self.call("GET", "/payments").json["payments"]
        self.assertEqual(listed[0]["refund"], first["refund"])

    def test_every_refusal_has_its_own_code_and_a_plain_message(self):
        payment = self.make_payment()
        declined = self.make_payment(order="ord_2", number="4000000000000002")
        self.refund(payment["payment_reference"], {"reason": "r"})
        codes = {}
        for label, response, status in (
            ("invalid_request", self.refund(payment["payment_reference"], ["x"]), 400),
            ("reason_required", self.refund(payment["payment_reference"], {}), 422),
            ("reason_too_long", self.refund(payment["payment_reference"], {"reason": "x" * 501}), 422),
            ("payment_not_found", self.refund("pay_nope", {"reason": "r"}), 404),
            ("payment_declined", self.refund(declined["payment_reference"], {"reason": "r"}), 409),
            ("payment_already_refunded", self.refund(payment["payment_reference"], {"reason": "r"}), 409),
        ):
            self.assertError(response, status, label)
            codes[label] = response.json["error"]["message"]
        self.assertEqual(len(set(codes.values())), len(codes))

    def test_refusals_come_in_the_contracts_order(self):
        declined = self.make_payment(number="4000000000000002")
        self.assertEqual(self.refund("pay_nope", ["x"]).json["error"]["code"], "invalid_request")
        self.assertEqual(self.refund("pay_nope", {"reason": " "}).json["error"]["code"], "reason_required")
        self.assertEqual(self.refund("pay_nope", {"reason": "x" * 501}).json["error"]["code"], "reason_too_long")
        self.assertEqual(self.refund("pay_nope", {"reason": "r"}).json["error"]["code"], "payment_not_found")
        self.assertEqual(self.refund(declined["payment_reference"], {}).json["error"]["code"], "reason_required")
        self.assertEqual(self.refund(declined["payment_reference"], {"reason": "r"}).json["error"]["code"],
                         "payment_declined")

    def test_a_success_is_told_apart_from_a_refusal(self):
        payment = self.make_payment()
        ok = self.refund(payment["payment_reference"], {"reason": "r"})
        refused = self.refund(payment["payment_reference"], {"reason": "r"})
        self.assertEqual((ok.status, "payment" in ok.json, "error" in ok.json), (200, True, False))
        self.assertEqual((refused.status, "payment" in refused.json, "error" in refused.json), (409, False, True))

    def test_two_simultaneous_refunds_give_one_200_and_one_already_refunded(self):
        payment = self.make_payment()
        # At most 5 connections: ThreadingHTTPServer's listen backlog is 5, and a sixth
        # simultaneous connection is sometimes reset on macOS.
        results = [None] * 5
        errors = []
        barrier = threading.Barrier(len(results))

        def work(index):
            try:
                barrier.wait()
                results[index] = self.refund(payment["payment_reference"], {"reason": "reason %d" % index})
            except BaseException as error:
                errors.append((index, repr(error)))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(len(results))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        won = [r for r in results if r.status == 200]
        lost = [r for r in results if r.status != 200]
        self.assertEqual(len(won), 1)
        self.assertEqual(len(lost), len(results) - 1)
        for response in lost:
            self.assertError(response, 409, "payment_already_refunded")
            self.assertEqual(response.json["error"]["refunded_at"], won[0].json["payment"]["refund"]["refunded_at"])
        listed = self.call("GET", "/payments").json["payments"]
        self.assertEqual(listed[0]["refund"], won[0].json["payment"]["refund"])

    def test_the_answers_hold_no_card_data(self):
        number, code = "4000 0000 0000 9995", "654"
        self.pay(body(order="d", number=number, security_code=code))
        ok = self.pay(body(order="p", security_code=code)).json["payment"]
        answers = [self.refund(ok["payment_reference"], {"reason": "r"}),
                   self.refund(ok["payment_reference"], {"reason": "r"}),
                   self.refund(self.call("GET", "/orders/d/payments").json["payments"][0]["payment_reference"],
                               {"reason": "r"}),
                   self.call("GET", "/orders/p/payments"), self.call("GET", "/payments")]
        shown = "".join(a.raw_body.decode() for a in answers)
        for secret in ("4000000000009995", number, VISA_OK, "4242 4242 4242 4242", '"%s"' % code):
            self.assertNotIn(secret, shown)
        self.assertEqual(answers[0].json["payment"]["card"], {"brand": "visa", "last4": "4242"})

    def test_a_refund_over_a_kept_connection_and_the_next_request_work(self):
        payment = self.make_payment()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        for _ in range(2):
            conn.request("POST", "/payments/%s/refund" % payment["payment_reference"],
                         json.dumps({"reason": "r"}), {"Content-Type": "application/json"})
            response = Response(conn.getresponse())
            self.assertIn(response.status, (200, 409))


class RefundedOrderApiTest(RefundApiCase):
    def refunded_order(self):
        self.make_payment(number="4000000000009995", key="a")
        payment = self.make_payment(key="b")
        refunded = self.refund(payment["payment_reference"], {"reason": "Customer cancelled"}).json["payment"]
        return payment, refunded

    def test_order_status_after_a_refund_says_refunded_with_the_time(self):
        payment, refunded = self.refunded_order()
        response = self.call("GET", "/orders/ord_1/payments")
        self.assertEqual(response.status, 200)
        self.assertCors(response)
        self.assertEqual(response.json["status"], "refunded")
        self.assertFalse(response.json["paid"])
        self.assertEqual(response.json["refunded_at"], refunded["refund"]["refunded_at"])
        self.assertEqual([p["outcome"] for p in response.json["payments"]], ["succeeded", "declined"])
        self.assertEqual(response.json["payments"][0]["refund"], refunded["refund"])
        self.assertIsNone(response.json["payments"][1]["refund"])

    def test_order_status_of_unpaid_and_paid_orders_is_as_before_plus_status(self):
        self.make_payment(order="u", number="4000000000000002")
        self.make_payment(order="p")
        unpaid = self.call("GET", "/orders/u/payments").json
        paid = self.call("GET", "/orders/p/payments").json
        self.assertEqual((unpaid["status"], unpaid["paid"], unpaid["refunded_at"]), ("unpaid", False, None))
        self.assertEqual((paid["status"], paid["paid"], paid["refunded_at"]), ("paid", True, None))
        self.assertEqual(set(paid), {"order_reference", "status", "paid", "refunded_at", "payments"})
        self.assertIsNone(paid["payments"][0]["refund"])

    def test_paying_a_refunded_order_is_409_order_refunded_and_no_attempt_is_made(self):
        _, refunded = self.refunded_order()
        calls, listed = self.calls, self.call("GET", "/payments").json
        for key in (None, "fresh"):
            for number in (VISA_OK, "4000000000000002"):
                with self.subTest(key=key, number=number):
                    response = self.pay(body(number=number, amount=100, currency="EUR"), key)
                    self.assertError(response, 409, "order_refunded")
                    error = response.json["error"]
                    self.assertEqual(error["message"], "This order was refunded, so it cannot be paid again.")
                    self.assertEqual(error["order_reference"], "ord_1")
                    self.assertEqual(error["refunded_at"], refunded["refund"]["refunded_at"])
                    self.assertNotIn("Idempotency-Replayed", response.headers)
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.call("GET", "/payments").json, listed)

    def test_order_refunded_is_a_different_refusal_from_order_already_paid(self):
        self.make_payment(order="paid")
        paid = self.pay(body(order="paid"))
        self.assertError(paid, 409, "order_already_paid")
        self.refunded_order()
        self.assertEqual(self.pay(body()).json["error"]["code"], "order_refunded")

    def test_other_pay_refusals_still_come_before_order_refunded(self):
        self.refunded_order()
        self.assertError(self.pay(body(amount=0)), 422, "invalid_amount")
        self.assertError(self.pay(body(number="1")), 422, "validation_failed")
        self.assertError(self.pay(None, raw="nope"), 400, "invalid_request")

    def test_a_replay_of_a_success_stays_a_200_after_the_refund(self):
        first = self.pay(body(), "k")
        self.refund(first.json["payment"]["payment_reference"], {"reason": "r"})
        again = self.pay(body(), "k")
        self.assertEqual(again.status, 200)
        self.assertEqual(again.headers["Idempotency-Replayed"], "true")
        self.assertEqual(again.json, first.json)
        self.assertEqual(self.pay(body(amount=1), "k").json["error"]["code"], "idempotency_key_reused")

    def test_list_payments_shows_the_refund_in_place_and_narrowing_is_unchanged(self):
        self.make_payment(order="a")
        self.make_payment(order="b", number="4000000000000002")
        target = self.make_payment(order="c")
        before = self.call("GET", "/payments").json["payments"]
        succeeded = [p["payment_reference"] for p in self.call("GET", "/payments?outcome=succeeded").json["payments"]]
        declined = [p["payment_reference"] for p in self.call("GET", "/payments?outcome=declined").json["payments"]]
        refunded = self.refund(target["payment_reference"], {"reason": "Out of stock"}).json["payment"]
        after = self.call("GET", "/payments").json["payments"]
        self.assertEqual([p["payment_reference"] for p in after], [p["payment_reference"] for p in before])
        self.assertEqual([p["payment_reference"] for p in self.call("GET", "/payments?outcome=succeeded").json["payments"]],
                         succeeded)
        self.assertEqual([p["payment_reference"] for p in self.call("GET", "/payments?outcome=declined").json["payments"]],
                         declined)
        self.assertEqual(after[0]["refund"], refunded["refund"])
        self.assertEqual([p["refund"] for p in after[1:]], [None, None])
        self.assertEqual(dict(after[0], refund=None), before[0])

    def test_earlier_cases_are_unchanged_for_unrefunded_orders(self):
        # retry after a decline, already paid, double submit, the shop's list and lookup
        self.assertEqual(self.pay(body(number="4000000000009995"), "a").json["payment"]["outcome"], "declined")
        self.assertEqual(self.pay(body(), "b").json["payment"]["outcome"], "succeeded")
        self.assertError(self.pay(body(), "c"), 409, "order_already_paid")
        self.assertEqual(self.pay(body(), "b").status, 200)
        status = self.call("GET", "/orders/ord_1/payments").json
        self.assertEqual((status["status"], status["paid"]), ("paid", True))
        self.assertEqual(len(self.call("GET", "/payments").json["payments"]), 2)

    def test_an_order_refunded_while_its_checkout_is_open_refuses_the_pay_and_shows_the_date(self):
        # the checkout's sequence for D14: open, the shop refunds, the customer pays, the checkout reads the status
        opened = self.call("GET", "/orders/other/payments").json
        self.assertEqual(opened["status"], "unpaid")
        # one tab pays, the shop refunds, a second tab (still showing the card form) pays
        payment = self.make_payment(order="other", key="tab-1")
        self.refund(payment["payment_reference"], {"reason": "Out of stock"})
        attempt = self.pay(body(order="other"), "tab-2")
        self.assertError(attempt, 409, "order_refunded")
        status = self.call("GET", "/orders/other/payments").json
        self.assertEqual(status["status"], "refunded")
        self.assertEqual(status["refunded_at"], attempt.json["error"]["refunded_at"])
        self.assertEqual(len(status["payments"]), 1)


class RefundCrossOriginApiTest(RefundApiCase):
    ORIGIN = "http://shop.example:3000"

    def assertBrowserHeaders(self, response):
        self.assertEqual(response.headers.get_all("Access-Control-Allow-Origin"), ["*"])
        self.assertEqual(response.headers.get_all("Access-Control-Expose-Headers"), ["Idempotency-Replayed"])
        self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))
        self.assertIsNone(response.headers.get("Idempotency-Replayed"))

    def test_the_preflight_of_the_refund_path_is_204_like_the_others(self):
        for request_method, request_headers in (("POST", "content-type"), ("POST", "Content-Type, Idempotency-Key"),
                                                (None, None), ("DELETE", "x-other")):
            with self.subTest(method=request_method, headers=request_headers):
                sent = {"Origin": self.ORIGIN}
                if request_method:
                    sent["Access-Control-Request-Method"] = request_method
                if request_headers:
                    sent["Access-Control-Request-Headers"] = request_headers
                response = self.call("OPTIONS", "/payments/pay_1/refund", headers=sent)
                self.assertEqual(response.status, 204)
                self.assertEqual(response.raw_body, b"")
                self.assertEqual(response.headers.get_all("Access-Control-Allow-Origin"), ["*"])
                self.assertEqual(response.headers.get_all("Access-Control-Allow-Methods"), ["GET, POST, OPTIONS"])
                self.assertEqual(response.headers.get_all("Access-Control-Allow-Headers"),
                                 ["Content-Type, Idempotency-Key"])
                self.assertEqual(response.headers.get_all("Access-Control-Max-Age"), ["600"])
                self.assertEqual(response.headers.get_all("Access-Control-Expose-Headers"), ["Idempotency-Replayed"])
                self.assertIsNone(response.headers.get("Access-Control-Allow-Credentials"))

    def test_the_preflight_of_the_refund_path_records_nothing(self):
        payment = self.make_payment()
        self.call("OPTIONS", "/payments/%s/refund" % payment["payment_reference"], {"reason": "r"})
        self.assertIsNone(self.call("GET", "/payments").json["payments"][0]["refund"])

    def test_every_answer_of_refund_payment_carries_the_headers(self):
        payment = self.make_payment()
        declined = self.make_payment(order="d", number="4000000000000002")
        ref = payment["payment_reference"]
        for label, response, status in (
            ("400", self.refund(ref, ["x"], origin=self.ORIGIN), 400),
            ("422 required", self.refund(ref, {}, origin=self.ORIGIN), 422),
            ("422 too long", self.refund(ref, {"reason": "x" * 501}, origin=self.ORIGIN), 422),
            ("404 payment", self.refund("pay_nope", {"reason": "r"}, origin=self.ORIGIN), 404),
            ("409 declined", self.refund(declined["payment_reference"], {"reason": "r"}, origin=self.ORIGIN), 409),
            ("200", self.refund(ref, {"reason": "r"}, origin=self.ORIGIN), 200),
            ("409 already", self.refund(ref, {"reason": "r"}, origin=self.ORIGIN), 409),
            ("405", self.call("GET", "/payments/%s/refund" % ref, headers={"Origin": self.ORIGIN}), 405),
            ("404 route", self.call("POST", "/payments/%s/refunds" % ref, {"x": 1}, {"Origin": self.ORIGIN}), 404),
        ):
            with self.subTest(label):
                self.assertEqual(response.status, status)
                self.assertBrowserHeaders(response)

    def test_the_new_answers_of_pay_order_and_order_status_carry_the_headers(self):
        payment = self.make_payment()
        self.refund(payment["payment_reference"], {"reason": "r"})
        refused = self.call("POST", "/payments", body(), {"Origin": self.ORIGIN, "Idempotency-Key": "k"})
        self.assertError(refused, 409, "order_refunded")
        self.assertBrowserHeaders(refused)
        status = self.call("GET", "/orders/ord_1/payments", headers={"Origin": self.ORIGIN})
        self.assertEqual(status.json["status"], "refunded")
        self.assertBrowserHeaders(status)
        listed = self.call("GET", "/payments", headers={"Origin": self.ORIGIN})
        self.assertBrowserHeaders(listed)

    def test_a_failing_refund_is_500_with_the_standard_body_and_the_headers(self):
        class BrokenStore:
            def get_payment(self, payment_reference):
                raise RuntimeError("store exploded")

        server = make_server(PaymentService(BrokenStore(), process_card), "127.0.0.1", 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(io.StringIO()):
            conn.request("POST", "/payments/pay_1/refund", json.dumps({"reason": "r"}),
                         {"Content-Type": "application/json", "Origin": self.ORIGIN})
            response = Response(conn.getresponse())
        self.assertError(response, 500, "internal_error")
        self.assertBrowserHeaders(response)


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
