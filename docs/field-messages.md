# Field messages

The reference wording for card-check mistakes. When `POST /payments` refuses a card (`validation_failed`, HTTP 422), `error.fields` lists every invalid field with a `code` and the `message` below. A client that checks the same things before it calls the service (the web checkout) shows these same texts, so a customer sees one wording whichever side catches the mistake.

The texts are kept in one table, `FIELD_MESSAGES` in `payments/cards.py`. This document lists the same table word for word, and a test (`tests/test_cards.py`) fails when the two differ. To change a message, change the table and this document together.

## How to read it

- Fields are listed in the order the service reports them: `cardholder_name`, `number`, `expiry`, `security_code`. A refusal has at most one entry per field.
- The codes are `required`, `invalid_format`, `invalid_number`, `expired` and `invalid_length`.
- The message is shown as is (without the surrounding backticks). It never contains card data.
- The "Brand" column is `-` except for the security code length message, which has three variants.

## Messages

| Field | Code | Brand | Message | Rule |
|---|---|---|---|---|
| `cardholder_name` | `required` | - | `Enter the name on the card.` | Missing, null, or blank (only spaces). |
| `cardholder_name` | `invalid_format` | - | `Enter the name on the card, up to 100 characters.` | Not a string, longer than 100 characters after trimming, or contains a control character. |
| `number` | `required` | - | `Enter your card number.` | Missing, null, or empty after removing spaces and dashes. |
| `number` | `invalid_format` | - | `Enter the card number using digits only, 12 to 19 of them. Spaces and dashes are fine.` | Not a string, contains anything but digits, spaces and dashes, or has fewer than 12 or more than 19 digits. |
| `number` | `invalid_number` | - | `This card number is not valid. Check it and try again.` | 12 to 19 digits that fail the check digit (Luhn). |
| `expiry` | `required` | - | `Enter the expiry date, month and year.` | `expiry_month` or `expiry_year` missing or null. |
| `expiry` | `invalid_format` | - | `Enter a valid expiry date, month 1 to 12 and a year such as 2030.` | Month or year not an integer, month outside 1 to 12, or year neither two digits (0 to 99, meaning 2000 to 2099) nor four digits. |
| `expiry` | `expired` | - | `This card has expired. Check the expiry date or use another card.` | The month is before the current month (UTC). The current month is fine. |
| `security_code` | `required` | - | `Enter the security code from your card.` | Missing, null or empty. |
| `security_code` | `invalid_format` | - | `The security code has digits only.` | Not a string or contains anything but digits (ASCII 0 to 9). |
| `security_code` | `invalid_length` | `amex` | `Enter the 4-digit security code from the front of your American Express card.` | The brand is amex and the code is not exactly 4 digits. |
| `security_code` | `invalid_length` | `other_brands` | `Enter the 3-digit security code from the back of your card.` | The brand is visa, mastercard, discover, or a number that passes the check but starts like no known brand, and the code is not exactly 3 digits. |
| `security_code` | `invalid_length` | `brand_unknown` | `The security code has 3 or 4 digits.` | The brand is not known because the number is missing or malformed (`number` is `required` or `invalid_format`), and the code is not 3 or 4 digits. |

## The brand behind the security code message

The brand comes from the leading digits of the number, after removing spaces and dashes: 4 is visa, 34 and 37 are amex, 51 to 55 and 2221 to 2720 are mastercard, 6011, 65, 644 to 649 and 622126 to 622925 are discover, anything else is unknown. A number that fails the check digit still has a brand (its own code is `invalid_number`). Only a missing or malformed number leaves the brand not known, and then the "3 or 4 digits" text is used, so the security code is not blamed for the number.

## Not in this table

- The `message` of the `validation_failed` refusal itself is a summary sentence, not a field message.
- Decline messages (`decline_reason`) are listed in [test-cards.md](test-cards.md) and are unchanged.
