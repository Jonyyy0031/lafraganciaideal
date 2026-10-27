---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: payments
found: 2026-10-24
---

# A webhook without `x-request-id` can never validate: the platform injects one

## Found while

Writing the http tests of payments-mercadopago/002 (testing phase).

## What

- The signature manifest includes `request-id:<x-request-id>;` only when the header is present
  (plan 002, `domain/webhook_signature.py`, as the official SDK does).
- `BodySizeLimitMiddleware.__call__` (`apps/api/src/fragancia_api/shared/http/middleware.py:56-69`)
  adds a generated `X-Request-ID` to the ASGI scope when the request has none (or an empty one).
  The route then reads that generated value as `x_request_id`, so the manifest the server builds
  has a `request-id` the sender never signed.
- Reproduced in `tests/unit/payments/test_webhook_http.py::test_a_notification_without_a_request_id_is_accepted_when_signed_that_way`
  (a strict `xfail`): a notification signed without a request id answers 401, although the same
  header validates at the domain and application layers.

## Why it matters

Mercado Pago sends `x-request-id` on its webhooks today, so nothing breaks in practice. If it
ever omitted the header, every real notification would be refused with 401 and payments would
stop being confirmed, with only a `payments.webhook_signature_invalid` warning to show for it.

## Suggested next step

Fast-lane or small plan: have the route read the header from the raw request without the
platform-generated value (for example a distinct scope key set by the middleware only when it
generates the id), then flip the strict `xfail` into a normal test. Or accept and close, since
Mercado Pago always sends the header.
