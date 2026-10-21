"""docs/refunds.md and the README keep up with what the service returns (D12)."""

import http.client
import json
import os
import re
import threading
import unittest

from payments import api as api_module
from payments import service as service_module
from payments.api import STATUS_BY_CODE, make_server
from payments.service import PaymentService
from payments.store import PaymentStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFUNDS_DOC = os.path.join(ROOT, "docs", "refunds.md")
README = os.path.join(ROOT, "README.md")

OK_CARD = "4242424242424242"
DECLINED_CARD = "4000000000000002"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def pay_body(order, number):
    return {"order_reference": order, "amount": 2599, "currency": "USD",
            "card": {"cardholder_name": "Ada Lovelace", "number": number, "expiry_month": 12,
                     "expiry_year": 2099, "security_code": "123"}}


def is_refund_related(code):
    """The codes a refund, or paying a refunded order, can give."""
    return "refund" in code or code.startswith("reason_") or code in ("payment_not_found", "payment_declined")


def codes_in_source():
    """Every error code the service's code names: literals given to Refusal
    and the codes of the status table."""
    found = set(STATUS_BY_CODE)
    for module in (service_module, api_module):
        found.update(re.findall(r'Refusal\(\s*"([a-z_]+)"', read(module.__file__)))
    return found


class RefundDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = read(REFUNDS_DOC)
        cls.store = PaymentStore()
        cls.server = make_server(PaymentService(cls.store), "127.0.0.1", 0, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.store.close()

    def call(self, method, path, payload=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = raw if raw is not None else (None if payload is None else json.dumps(payload))
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            response = conn.getresponse()
            text = response.read()
            return response.status, (json.loads(text) if text else None)
        finally:
            conn.close()

    def pay(self, order, number):
        status, body = self.call("POST", "/payments", pay_body(order, number))
        self.assertEqual(status, 201)
        return body["payment"]["payment_reference"]

    def test_every_refund_related_code_is_in_the_document(self):
        codes = {code for code in codes_in_source() if is_refund_related(code)}
        # The codes the contract names must all be there, so a rename shows up here.
        self.assertGreaterEqual(codes, {
            "reason_required", "reason_too_long", "payment_not_found", "payment_declined",
            "payment_already_refunded", "order_refunded"})
        for code in sorted(codes):
            self.assertIn("`%s`" % code, self.doc, "docs/refunds.md does not mention %s" % code)
        self.assertIn("`invalid_request`", self.doc)

    def test_every_refusal_the_service_gives_is_in_the_document_with_status_and_message(self):
        ok = self.pay("ord_doc_ok", OK_CARD)
        declined = self.pay("ord_doc_declined", DECLINED_CARD)
        reason = {"reason": "Out of stock"}
        self.assertEqual(self.call("POST", "/payments/%s/refund" % ok, reason)[0], 200)

        cases = [
            self.call("POST", "/payments/%s/refund" % ok, raw="{"),
            self.call("POST", "/payments/%s/refund" % ok, {"reason": "  "}),
            self.call("POST", "/payments/%s/refund" % ok, raw=""),
            self.call("POST", "/payments/%s/refund" % ok, {"reason": "x" * 501}),
            self.call("POST", "/payments/pay_missing/refund", reason),
            self.call("POST", "/payments/%s/refund" % declined, reason),
            self.call("POST", "/payments/%s/refund" % ok, reason),
            self.call("POST", "/payments", pay_body("ord_doc_ok", "5555555555554444")),
        ]
        seen = set()
        for status, body in cases:
            error = body["error"]
            seen.add(error["code"])
            self.assertEqual(status, STATUS_BY_CODE[error["code"]], error["code"])
            self.assertIn("`%s`" % error["code"], self.doc)
            row = next((line for line in self.doc.splitlines()
                        if line.startswith("|") and "`%s`" % error["code"] in line), None)
            self.assertIsNotNone(row, "no table row for %s" % error["code"])
            self.assertIn(str(status), row)
            if error["code"] == "invalid_request":
                continue  # its message depends on the fault
            if error["code"] == "payment_already_refunded":
                prefix = error["message"].split(error["refunded_at"])[0]
                self.assertIn(prefix, row)
            else:
                self.assertIn(error["message"], row)
        self.assertEqual(seen, {
            "invalid_request", "reason_required", "reason_too_long", "payment_not_found",
            "payment_declined", "payment_already_refunded", "order_refunded"})

    def test_the_document_has_the_steps_for_d1_to_d5_and_d10_with_the_test_cards(self):
        for heading in ("### D1.", "### D2.", "### D3.", "### D4.", "### D5.", "### D10."):
            self.assertIn(heading, self.doc)
        self.assertIn("4242 4242 4242 4242", self.doc)
        self.assertIn("4000 0000 0000 0002", self.doc)
        self.assertIn("POST /payments/{payment_reference}/refund", self.doc)
        self.assertIn("500 characters", self.doc)

    def test_the_document_matches_the_success_answer(self):
        ok = self.pay("ord_doc_shape", OK_CARD)
        status, body = self.call("POST", "/payments/%s/refund" % ok, {"reason": "Customer cancelled"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body["payment"]["refund"]), {"amount", "currency", "reason", "refunded_at"})
        for member in body["payment"]["refund"]:
            self.assertIn(member, self.doc)
        _, order = self.call("GET", "/orders/ord_doc_shape/payments")
        self.assertEqual(order["status"], "refunded")
        self.assertEqual(order["refunded_at"], body["payment"]["refund"]["refunded_at"])


class ReadmeTest(unittest.TestCase):
    def test_the_readme_links_the_refund_document_and_lists_the_route(self):
        text = read(README)
        self.assertIn("(docs/refunds.md)", text)
        self.assertIn("POST /payments/{payment_reference}/refund", text)
        self.assertTrue(os.path.exists(REFUNDS_DOC))

    def test_the_readme_lists_the_refund_codes(self):
        text = read(README)
        for code in sorted(c for c in codes_in_source() if is_refund_related(c)):
            self.assertIn("`%s`" % code, text)


if __name__ == "__main__":
    unittest.main()
