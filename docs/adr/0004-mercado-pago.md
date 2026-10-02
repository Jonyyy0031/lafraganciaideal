# 0004 — Mercado Pago as the first payment provider

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

The business operates in Mexico and needs card payments, interest-free installments (MSI) and
cash/transfer alternatives. The owner already prefers Mercado Pago. Decision:
[initiative README](../../plans/platform-infraestructura/README.md) #3.

## Decision

- Payments live behind a `PaymentGateway` port owned by the `payments` module; Mercado Pago
  (Checkout Pro: cards, MSI, OXXO, SPEI) is its first adapter.
- Payment confirmation comes **only** from verified webhooks (signature checked, processed
  idempotently by notification id), never from the browser redirect.
- Card data never touches our servers (hosted checkout).

## Alternatives considered

- **Stripe MX**: excellent developer experience, but not the owner's choice.
- **Openpay / Conekta**: viable in Mexico; no advantage for this business today.

## Consequences

- Adding or switching providers means a new adapter, not changes in orders or the domain.
- The API must be reachable from the internet for webhooks (a tunnel in development).
