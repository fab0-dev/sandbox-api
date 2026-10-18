# sandbox-api

The payment service of fab0's sandbox for connected factories. It takes a card payment for an order, says whether it succeeded or was declined (and why), and keeps every attempt so the shop can look at them. Python 3.12, standard library only. Test mode only: no real provider is called and no money moves.

Its factory is a copy of the `delivery` example, called by `sandbox-home`. The contract it follows is `contract.yaml`.

## Run it

```
python3 -m payments                       # http://127.0.0.1:8000, payments in ./payments.db
python3 -m payments --host 0.0.0.0 --port 9000 --db /var/lib/payments.db
python3 -m payments --db :memory:         # payments are lost when it stops
```

- `--host` (default `127.0.0.1`) and `--port` (default `8000`) set the address.
- `--db` (or the `PAYMENTS_DB` environment variable) is the sqlite3 file the payments are kept in. Default `payments.db` in the current directory.
- There is no authentication in this Story. Every response allows any origin (CORS), so the web checkout can call it from a browser.

## Tests

```
python3 -m unittest discover -s tests -t .
```

## Calls

All bodies are JSON (UTF-8). Amounts are integers in minor units (2599 is 25.99).

### Pay order: `POST /payments`

Optional header `Idempotency-Key` (1 to 255 characters): send a fresh one for each submit of the form and reuse it when you resend the same submit.

```
curl -i -X POST http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: submit-1' \
  -d '{"order_reference": "ord_1", "amount": 2599, "currency": "USD",
       "card": {"cardholder_name": "Ada Lovelace", "number": "4242 4242 4242 4242",
                "expiry_month": 12, "expiry_year": 2030, "security_code": "123"}}'
```

Three answers can be told apart:

| Answer | HTTP | Body |
|---|---|---|
| payment succeeded | 201 (200 when replayed) | `{"payment": {... "outcome": "succeeded"}, "order_paid": true}` |
| payment declined | 201 (200 when replayed) | `{"payment": {... "outcome": "declined", "decline_reason", "message"}, "order_paid": false}` |
| request refused | 4xx | `{"error": {"code", "message", ...}}`, no `payment` |

A decline is a normal answer, not an error: the order stays unpaid and the checkout may submit again with a new `Idempotency-Key`. Show `message` to the customer as is.

A payment is `payment_reference`, `order_reference`, `amount`, `currency`, `outcome`, `decline_reason` (null when succeeded), `message`, `card` (`brand` and `last4` only) and `created_at` (UTC, like `2026-10-18T14:03:22Z`).

Refusals try no payment and record nothing. When several apply, the first of this list is returned:

| `error.code` | HTTP | When |
|---|---|---|
| `invalid_request` | 400 | body is not a JSON object, `order_reference` or `currency` missing or malformed, `Idempotency-Key` empty or over 255 characters |
| `invalid_amount` | 422 | `amount` missing, not an integer or not greater than 0 |
| `validation_failed` | 422 | invalid card; `error.fields` lists every invalid field (`cardholder_name`, `number`, `expiry`, `security_code`) with a `code` and a `message` |
| `idempotency_key_reused` | 422 | the key was used for a different request |
| `order_already_paid` | 409 | the order has a succeeded payment; `error.order_reference` names it |

Idempotency: a repeated key with the same request returns the first answer (200, header `Idempotency-Replayed: true`) without a new attempt. "Same request" is judged on a keyed digest of the order, amount, currency, cardholder name, card brand, last four digits and expiry; the full card number and the security code are not part of it. Only succeeded and declined answers are remembered (the last 10 000 keys, kept in memory, so a restart forgets them); a refusal is judged afresh next time.

No order is charged twice: attempts for one order run one after the other, so of two simultaneous requests for an order at most one can succeed, and the other gets `order_already_paid` (or, with the same key, the first answer).

### Order payment status: `GET /orders/{order_reference}/payments`

`{"order_reference", "paid", "payments": [...]}`, attempts newest first. An order with no attempts is `paid: false` with an empty list. Not paged.

### List payments: `GET /payments`

Query: `outcome` (`succeeded` or `declined`), `limit` (1 to 500, default 100), `cursor` (the `next_cursor` of the previous page). Answer: `{"payments": [...], "next_cursor": "..." or null}`, newest first. Follow `next_cursor` until it is `null` to reach the oldest payment. A bad query is `invalid_request` (400).

### Other

- Unknown paths: 404 `not_found`. A method a path does not accept: 405 `method_not_allowed` with an `Allow` header. An unexpected failure: 500 `internal_error`; read the order payment status before submitting again.
- `OPTIONS` on `/payments` and `/orders/{order_reference}/payments` is a CORS preflight and answers 204.

## Test cards

The card numbers that give each outcome and each decline reason are in [docs/test-cards.md](docs/test-cards.md). For example `4242 4242 4242 4242` succeeds and `4000 0000 0000 9995` is declined for insufficient funds.

## Card data

The full card number and the security code are used only while a payment request is handled. They are not stored, logged, returned, listed or echoed in error messages. A payment shows the card brand and the last four digits, nothing more.
