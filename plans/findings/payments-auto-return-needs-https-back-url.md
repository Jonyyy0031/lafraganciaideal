---
status: planned # open → deferred | planned | resolved | discarded (the user decides)
plan: payments-mercadopago/002
module: payments
found: 2026-10-24
---

# Mercado Pago refuses the preference when the back URL is not https

## Found while

Running the sandbox check of payments-mercadopago/001 against the real Mercado Pago API, with
the developer's own test application (decision 1).

## What

- `MercadoPagoGateway._body` always sends `"auto_return": "approved"`, with every back URL set
  to `{STOREFRONT_URL}/pedido/pago`
  (`apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py`).
- In development `STOREFRONT_URL` is `http://localhost:4200`. With that body, the real API
  answers `400 invalid_auto_return`, "auto_return invalid. back_url.success must be defined".
  The gateway logs `payments.gateway_error status=400`, and the guest gets
  503 `PAYMENTS_GATEWAY_UNAVAILABLE`. No link can be had in development.
- On 2026-10-24, with the same body and only the back URLs or `auto_return` changed:

  | Back URL                                       | `auto_return` | Answer |
  | ---------------------------------------------- | ------------- | ------ |
  | `http://localhost:4200/pedido/pago`            | `approved`    | 400    |
  | `http://127.0.0.1:4200/pedido/pago`            | `approved`    | 400    |
  | `http://lafraganciaideal.example/pedido/pago`  | `approved`    | 400    |
  | `https://lafraganciaideal.example/pedido/pago` | `approved`    | 201    |
  | `https://localhost:4200/pedido/pago`           | `approved`    | 201    |
  | `http://localhost:4200/pedido/pago`            | not sent      | 201    |

  So `auto_return` needs an `https` success URL; the host does not matter.
- A preference without `auto_return` was paid in the sandbox with a test card (the developer,
  2026-10-24), so the rest of the body is accepted end to end.
- The same run answered review M2: the API accepted the millisecond `expiration_date_to` and
  echoed it unchanged.

## Why it matters

- Production is unaffected: its `STOREFRONT_URL` is `https`.
- Development and any plain-`http` staging can never create a link, so the sandbox flow cannot
  be tried at all. Plan 002's live verification needs a link to pay.
- The unit tests and the fake Mercado Pago accept the body, so nothing caught it.

## Suggested next step

A small plan in payments-mercadopago, before or as the first step of 002. Two options:

- Send `auto_return` only when the back URL is `https` (without it the guest clicks "Volver al
  sitio" by hand).
- Or fail fast at startup when `STOREFRONT_URL` is not `https` while a token is configured.

Plus a gateway test for each, and an update to the fake Mercado Pago so it refuses this body the
way the real API does.
