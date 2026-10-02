# 0006 — Transactional outbox from day one

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

In web-rh, events are published in memory after the commit, and an outbox was left for later.
Here the critical flows are money and orders: losing `payment.approved` would leave a paid
order as unpaid; losing `order.paid` would skip the stock update or the confirmation to the
customer. Decision: [initiative README](../../plans/platform-infraestructura/README.md) #1 (lighter than web-rh, but not here).

## Decision

Domain events are stored in an outbox table **in the same transaction** as the change that
produced them. The worker relays them to subscribers with at-least-once delivery and retries;
every subscriber is idempotent. External inputs (Mercado Pago webhooks) are stored by their
provider id before processing, so duplicates are ignored.

## Alternatives considered

- **In-memory bus after commit** (web-rh today): simplest, but a crash between commit and
  publish loses the event.
- **External broker (RabbitMQ, Kafka)**: more infrastructure without solving the dual-write
  problem by itself.

## Consequences

- One more table and a relay loop in the worker from the first module that publishes events.
- Subscribers must tolerate duplicates and out-of-order delivery.
