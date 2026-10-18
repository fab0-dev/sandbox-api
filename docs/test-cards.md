# Test cards

The service runs in test mode only. No real provider is called and no money moves. Known card numbers give known outcomes, so you can repeat every outcome on purpose.

## How to use them

Enter one of the numbers below at checkout (or send it in `card.number` to `POST /payments`) together with:

- any cardholder name (not blank, at most 100 characters);
- any expiry date that is not in the past (the current month is fine);
- any security code of the right length: 3 digits, or 4 digits for American Express cards. None of the cards below is American Express.

The outcome depends on the card number alone. Spaces and dashes in the number are ignored, so `4242 4242 4242 4242` works.

## Cards that give each outcome

| Card number         | Brand      | Outcome   | Decline reason           | Message shown to the customer                                  |
|---------------------|------------|-----------|--------------------------|----------------------------------------------------------------|
| 4242 4242 4242 4242 | Visa       | succeeded | -                        | Payment received. Your order is paid.                          |
| 5555 5555 5555 4444 | Mastercard | succeeded | -                        | Payment received. Your order is paid.                          |
| 4000 0000 0000 0002 | Visa       | declined  | `card_declined`          | Your card was declined. Try another card.                      |
| 4000 0000 0000 9995 | Visa       | declined  | `insufficient_funds`     | Your card has insufficient funds. Try another card.            |
| 4000 0000 0000 0069 | Visa       | declined  | `expired_card`           | Your card has expired. Check the expiry date or use another card. |
| 4000 0000 0000 0127 | Visa       | declined  | `incorrect_card_details` | Some of your card details are incorrect. Check them and try again. |
| 4000 0000 0000 0119 | Visa       | declined  | `processing_error`       | We could not process the payment right now. Please try again.  |

A `processing_error` decline is temporary: the order stays unpaid and you can try again.

Any other card number that passes the check digit (Luhn) is approved and gives `succeeded`.

Note that the `expired_card` decline comes from the card number, whatever expiry date you enter. An expiry date in the past is a different thing: it is refused before any payment is tried, as a `validation_failed` error on the `expiry` field.

## Cards that are refused before any payment is tried

These are not test cards, they show the card checks:

- A number that fails the check digit, for example `4242 4242 4242 4241`: refused with `invalid_number` on `number`.
- A security code of the wrong length, for example 4 digits on a Visa card: refused with `invalid_length` on `security_code`.
- A missing field, or an expiry date in the past: refused with `required` or `expired`.

## Card data is never kept

The full card number and the security code are used only while the payment is being decided. They are not stored, returned or listed. A payment shows at most the card brand and the last four digits.
