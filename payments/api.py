"""The JSON HTTP service of the contract, on the standard library.

    POST /payments                          Pay order
    GET  /orders/{order_reference}/payments Order payment status
    GET  /payments                          List payments

Every response, errors and CORS preflights included, is JSON (or empty) and
carries the CORS headers of the contract. A refusal is a 4xx with
{"error": {"code", "message", ...}}; a succeeded or declined payment is 201
(200 when replayed by Idempotency-Key) with a "payment".

Nothing here logs a request body, so card data never reaches the log.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from payments.service import PaymentService, Refusal

MAX_BODY_BYTES = 64 * 1024

STATUS_BY_CODE = {
    "invalid_request": 400,
    "invalid_amount": 422,
    "validation_failed": 422,
    "idempotency_key_reused": 422,
    "order_already_paid": 409,
    "not_found": 404,
    "method_not_allowed": 405,
    "internal_error": 500,
}

_ORDER_PATH = re.compile(r"/orders/([^/]+)/payments")
_DIGITS = re.compile(r"[0-9]+")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "Idempotency-Replayed",
}
PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Idempotency-Key",
    "Access-Control-Max-Age": "600",
}


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _single(query: dict[str, list[str]], name: str) -> Optional[str]:
    values = query.get(name)
    if values is None:
        return None
    if len(values) > 1:
        raise Refusal("invalid_request", "%s is given more than once." % name)
    return values[0]


def make_handler(service: PaymentService, quiet: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "sandbox-payments"

        # -- plumbing ----------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:
            if not quiet:
                sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def _send(self, status: int, body: Optional[dict[str, Any]], headers: Optional[dict[str, str]] = None) -> None:
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            for name, value in CORS_HEADERS.items():
                self.send_header(name, value)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if body is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if data and self.command != "HEAD":
                self.wfile.write(data)

        def _refuse(self, refusal: Refusal, headers: Optional[dict[str, str]] = None) -> None:
            status = STATUS_BY_CODE.get(refusal.code, 400)
            self._send(status, error_body(refusal.code, refusal.message, **refusal.extra), headers)

        def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
            # Errors found by http.server itself (bad request line, unsupported
            # method, header too large) get the standard body too.
            self.close_connection = True
            if code == 501:  # a method http.server has no handler for
                code, message = 405, "This method is not accepted."
            known = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}
            name = known.get(code, "invalid_request" if 400 <= code < 500 else "internal_error")
            self._send(code, error_body(name, message or "The request could not be handled."))

        def _read_body(self) -> Optional[bytes]:
            """The request body, or None when it is too large or its length is
            not usable (the connection is then closed after the answer)."""
            if self.headers.get("Transfer-Encoding") is not None:
                self.close_connection = True
                return None
            length = self.headers.get("Content-Length")
            if length is None:
                return b""
            if not _DIGITS.fullmatch(length.strip()) or int(length) > MAX_BODY_BYTES:
                self.close_connection = True
                return None
            return self.rfile.read(int(length))

        # -- routing -----------------------------------------------------

        def _handle(self) -> None:
            try:
                self._route()
            except Refusal as refusal:
                self._refuse(refusal)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.close_connection = True
                self._send(500, error_body("internal_error", "Something went wrong on our side. Please try again."))

        def _route(self) -> None:
            body = self._read_body()
            parts = urlsplit(self.path)
            order_match = _ORDER_PATH.fullmatch(parts.path)
            if parts.path == "/payments":
                allowed = ("GET", "POST")
            elif order_match:
                allowed = ("GET",)
            else:
                raise Refusal("not_found", "There is nothing at this address.")
            method = self.command
            if method == "OPTIONS":
                self._send(204, None, PREFLIGHT_HEADERS)
                return
            if method not in allowed:
                self._send(
                    405,
                    error_body("method_not_allowed", "This address does not accept %s." % method),
                    {"Allow": ", ".join(allowed + ("OPTIONS",))},
                )
                return
            if method == "POST":
                self._pay_order(body)
            elif order_match:
                self._order_status(unquote(order_match.group(1)))
            else:
                self._list_payments(parts.query)

        # -- operations --------------------------------------------------

        def _pay_order(self, raw: Optional[bytes]) -> None:
            if raw is None:
                raise Refusal("invalid_request", "The request body is missing or too large.")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                raise Refusal("invalid_request", "The request body is not valid JSON.") from None
            result = service.pay_order(body, self.headers.get("Idempotency-Key"))
            headers = {"Idempotency-Replayed": "true"} if result.replayed else None
            self._send(200 if result.replayed else 201, result.body(), headers)

        def _order_status(self, order_reference: str) -> None:
            self._send(200, service.order_status(order_reference).body())

        def _list_payments(self, query_string: str) -> None:
            try:
                query = parse_qs(query_string, keep_blank_values=True, strict_parsing=bool(query_string))
            except ValueError:
                raise Refusal("invalid_request", "The query string is not valid.") from None
            outcome = _single(query, "outcome")
            raw_limit = _single(query, "limit")
            cursor = _single(query, "cursor")
            limit = 100
            if raw_limit is not None:
                if not _DIGITS.fullmatch(raw_limit) or not raw_limit.isascii():
                    raise Refusal("invalid_request", "limit must be a whole number from 1 to 500.")
                limit = int(raw_limit) if len(raw_limit) < 10 else 10**9
            page = service.list_payments(outcome=outcome, limit=limit, cursor=cursor)
            self._send(
                200,
                {"payments": [p.as_dict() for p in page.payments], "next_cursor": page.next_cursor},
            )

        do_GET = do_POST = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

    return Handler


def make_server(service: PaymentService, host: str = "127.0.0.1", port: int = 8000, quiet: bool = False) -> ThreadingHTTPServer:
    """A threaded HTTP server for the service; call serve_forever() on it.
    Port 0 picks a free port (see server.server_address)."""
    server = ThreadingHTTPServer((host, port), make_handler(service, quiet))
    server.daemon_threads = True
    return server
