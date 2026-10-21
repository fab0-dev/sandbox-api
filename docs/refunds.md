# Refunds

The shop refunds a paid order in full, with a reason, through the service. The service runs in test mode only: no money moves, and a refund that is allowed always succeeds. This page says how to refund, lists every refusal, and gives step-by-step commands (with the [test cards](test-cards.md)) so you can repeat each case on purpose against the running service.

A payment is refunded at most once, in full: its whole amount, in its currency, in one go. There are no partial refunds. The shop names the **payment**, not the order. An order has at most one succeeded payment, so this is the same as refunding the order. Find the `payment_reference` in the answer of `POST /payments`, in `GET /orders/{order_reference}/payments` or in `GET /payments`.

There is no authentication in this Story: anyone who can reach the service can call Refund payment. Access control is left to its own Story.

## How to refund

```
POST /payments/{payment_reference}/refund
Content-Type: application/json

{"reason": "Out of stock"}
```

The reason:

- is required and is plain text from the shop;
- must not be empty or only whitespace;
- is at most 500 characters;
- is kept and shown exactly as given (not trimmed or rewritten).

A request with no body at all is a request without a reason (`reason_required`). A body that is present but is not valid JSON, or not a JSON object, is `invalid_request`.

### The answer

A success is `200 OK` with the payment and its refund. The body has `payment` and no `error`:

```
{"payment": {
   "payment_reference": "pay_8f3a1c", "order_reference": "ord_1",
   "amount": 2599, "currency": "USD",
   "outcome": "succeeded", "decline_reason": null,
   "message": "Payment received. Your order is paid.",
   "card": {"brand": "visa", "last4": "4242"},
   "created_at": "2026-10-18T14:03:22Z",
   "refund": {"amount": 2599, "currency": "USD", "reason": "Out of stock",
              "refunded_at": "2026-10-18T15:30:00Z"}}}
```

- `refund.amount` and `refund.currency` are always the payment's own.
- `refund.refunded_at` is the time of the refund, UTC, like `created_at`.
- `outcome` stays `succeeded`: a refund does not change the outcome, the decline reason, the card or the payment's place in a list.
- A payment that was not refunded has `"refund": null`. The member is present on every payment the service returns.
- No card data beyond `brand` and `last4` is returned or kept.

A refusal has `error` and no `payment`, and is a 4xx. Nothing is changed or recorded.

## Refusals

Every refusal has the body `{"error": {"code", "message", ...}}`. The `code` is what a caller keys on; the messages are fixed texts. When more than one refusal applies, the first of this list is returned.

| Order | HTTP | `error.code` | `error.message` | When |
|---|---|---|---|---|
| 1 | 400 | `invalid_request` | The request body is not valid JSON. | The body is present but is not valid JSON, is not a JSON object, or is too large. |
| 2 | 422 | `reason_required` | Give the reason for the refund. | The reason is missing, null, not a text, empty or only whitespace; also when there is no body at all. |
| 3 | 422 | `reason_too_long` | The reason can be at most 500 characters. | The reason is longer than 500 characters. |
| 4 | 404 | `payment_not_found` | There is no payment with this reference. | No payment has this reference. `error.payment_reference` repeats it. (An unknown path is a different thing: `not_found`.) |
| 5 | 409 | `payment_declined` | This payment was declined, so it took no money and there is nothing to refund. | The payment was declined. `error.payment_reference` names it. |
| 6 | 409 | `payment_already_refunded` | This payment was already refunded at 2026-10-18T15:30:00Z. | The payment was refunded before. The message says when; `error.refunded_at` is the time of the first refund, which stays as it was, and `error.payment_reference` names the payment. |

The message of `invalid_request` depends on the fault (for example "The request body must be a JSON object."); the other messages are exactly as above, except for the time in `payment_already_refunded`.

If two refund requests for the same payment arrive at the same time, exactly one succeeds (200) and the other is refused with `payment_already_refunded`, giving the time of the one that succeeded. A repeat of a request that succeeded is also `payment_already_refunded`, which tells the caller it went through. There is no `Idempotency-Key` for refunds.

### Paying a refunded order: `POST /payments`

A refunded order is final. It is not paid again, so a second checkout tab or a stale page cannot charge it twice. `POST /payments` for such an order is refused before any card is tried, whatever the card, the amount or the `Idempotency-Key`; no payment is tried and no attempt is recorded:

| HTTP | `error.code` | `error.message` | Other members |
|---|---|---|---|
| 409 | `order_refunded` | This order was refunded, so it cannot be paid again. | `error.order_reference`, `error.refunded_at` (UTC time of the refund) |

The message is for the customer and may be shown as is. This refusal is checked before `order_already_paid` (a refunded order also has a succeeded payment), and after `invalid_request`, `invalid_amount`, `validation_failed` and `idempotency_key_reused`.

## How a refund shows

- **Order payment status**, `GET /orders/{order_reference}/payments`: `status` is `unpaid`, `paid` or `refunded`. For a refunded order `status` is `refunded` and `refunded_at` is the time of the refund (the same as `refund.refunded_at` on the succeeded payment, which is among `payments`). `paid` is kept for older readers and is true only when `status` is `paid`, so it is `false` for a refunded order. Read `status`. For an unpaid or a paid order `refunded_at` is `null`.
- **List payments**, `GET /payments`: each payment carries its `refund` (or `null`). A refund changes neither a payment's place in the list nor which payments `outcome=succeeded` and `outcome=declined` return: a refunded payment is still `succeeded`.

## Step by step

Start the service (see the [README](../README.md)) and use another terminal. The commands use `curl` and `python3` (to pick the payment reference out of an answer); the service is at `http://127.0.0.1:8000`.

```
BASE=http://127.0.0.1:8000
```

Test cards (from [test-cards.md](test-cards.md)): `4242 4242 4242 4242` succeeds, `4000 0000 0000 0002` is declined (`card_declined`). Use any name, an expiry that is not in the past and security code `123`.

Pick a fresh order reference each time you repeat a step (here `ord_refund_1` and `ord_refund_2`), because an order that was refunded or paid cannot be paid again.

### D1. Refund a succeeded payment

1. Pay an order with the succeeding card and keep the payment reference:

   ```
   PAY=$(curl -s -X POST $BASE/payments -H 'Content-Type: application/json' \
     -d '{"order_reference": "ord_refund_1", "amount": 2599, "currency": "USD",
          "card": {"cardholder_name": "Ada Lovelace", "number": "4242 4242 4242 4242",
                   "expiry_month": 12, "expiry_year": 2030, "security_code": "123"}}' \
     | python3 -c 'import sys, json; print(json.load(sys.stdin)["payment"]["payment_reference"])')
   echo $PAY
   ```

2. Refund it, with a reason:

   ```
   curl -i -X POST $BASE/payments/$PAY/refund -H 'Content-Type: application/json' \
     -d '{"reason": "Out of stock"}'
   ```

   Expect `200 OK`. The payment is shown with `"outcome": "succeeded"` and a `refund` whose `amount` (2599) and `currency` (USD) equal the payment's own, `reason` is `Out of stock` and `refunded_at` is the time of the refund.

3. See it in the order's status and in the list (D8, D9):

   ```
   curl -s $BASE/orders/ord_refund_1/payments
   curl -s "$BASE/payments?outcome=succeeded"
   ```

   The first answer has `"status": "refunded"`, `"paid": false` and the same `refunded_at`. The payment is in the list as before, with its `refund`.

### D2. A missing or blank reason

Uses any payment reference, for example the one of D1: the reason is checked first, so these are refused the same way for a payment that is refunded or not. Each is refused with `422` and `reason_required`, and nothing changes:

```
curl -i -X POST $BASE/payments/$PAY/refund -H 'Content-Type: application/json' -d '{}'
curl -i -X POST $BASE/payments/$PAY/refund -H 'Content-Type: application/json' -d '{"reason": "   "}'
curl -i -X POST $BASE/payments/$PAY/refund
```

To see that nothing changed, read `GET $BASE/orders/<order>/payments`: the payment is as before (a `refund` of `null` if it was not refunded, the first refund if it was).

### D3. A payment that does not exist

```
curl -i -X POST $BASE/payments/pay_does_not_exist/refund -H 'Content-Type: application/json' \
  -d '{"reason": "Out of stock"}'
```

Expect `404` and `payment_not_found`. Nothing is recorded.

### D4. A declined payment

1. Pay an order with the declined card and keep the reference:

   ```
   DECLINED=$(curl -s -X POST $BASE/payments -H 'Content-Type: application/json' \
     -d '{"order_reference": "ord_refund_2", "amount": 2599, "currency": "USD",
          "card": {"cardholder_name": "Ada Lovelace", "number": "4000 0000 0000 0002",
                   "expiry_month": 12, "expiry_year": 2030, "security_code": "123"}}' \
     | python3 -c 'import sys, json; print(json.load(sys.stdin)["payment"]["payment_reference"])')
   ```

2. Try to refund it:

   ```
   curl -i -X POST $BASE/payments/$DECLINED/refund -H 'Content-Type: application/json' \
     -d '{"reason": "Out of stock"}'
   ```

   Expect `409` and `payment_declined`: the payment took no money, so there is nothing to refund. The payment is unchanged (`refund` is `null`).

### D5. A second refund

Repeat the refund of D1 for the same payment:

```
curl -i -X POST $BASE/payments/$PAY/refund -H 'Content-Type: application/json' \
  -d '{"reason": "Customer cancelled"}'
```

Expect `409` and `payment_already_refunded`. The message and `error.refunded_at` give the time of the first refund. The first refund stays as it was (its reason is still `Out of stock`); check it with `curl -s $BASE/orders/ord_refund_1/payments`.

### D10. Paying a refunded order

With `ord_refund_1` refunded (D1), try to pay it again, with any card:

```
curl -i -X POST $BASE/payments -H 'Content-Type: application/json' \
  -d '{"order_reference": "ord_refund_1", "amount": 2599, "currency": "USD",
       "card": {"cardholder_name": "Ada Lovelace", "number": "5555 5555 5555 4444",
                "expiry_month": 12, "expiry_year": 2030, "security_code": "123"}}'
```

Expect `409` and `order_refunded`, with the message "This order was refunded, so it cannot be paid again." and `error.refunded_at`. No payment is tried: `GET $BASE/orders/ord_refund_1/payments` still lists the one payment it had.

## Every answer allows any origin

As for every call of the service, answers carry `Access-Control-Allow-Origin: *`, and `OPTIONS /payments/{payment_reference}/refund` answers a CORS preflight with 204. A method other than `POST` and `OPTIONS` on the refund path is `405 method_not_allowed` with `Allow: POST, OPTIONS`.
