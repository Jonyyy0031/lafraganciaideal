from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from fragancia_api.modules.payments.domain.payment import Payment, PaymentOutcome
from fragancia_api.modules.payments.infrastructure.tables import payments
from fragancia_api.shared.infrastructure.database import Database


class SqlPaymentRepository:
    """Payments in `payments.payments`. Joins the active transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, payment: Payment) -> Payment:
        # Insert first so the row (and its lock) exists even for a payment seen for the first
        # time: `SELECT … FOR UPDATE` alone locks nothing when there is no row, and two
        # concurrent deliveries would both confirm (plan 002 review H1). A concurrent insert of
        # the same id waits here until the other transaction ends.
        await self._database.session.execute(
            insert(payments)
            .values(
                payment_id=payment.payment_id,
                created_at=payment.created_at,
                **self._values(payment),
            )
            .on_conflict_do_nothing(index_elements=[payments.c.payment_id])
        )
        row = (
            await self._database.session.execute(
                select(payments)
                .where(payments.c.payment_id == payment.payment_id)
                .with_for_update()
            )
        ).one()
        return Payment(
            payment_id=row.payment_id,
            order_id=row.order_id,
            status=row.status,
            status_detail=row.status_detail,
            amount_cents=row.amount_cents,
            currency=row.currency,
            payment_type=row.payment_type,
            approved_at=row.approved_at,
            outcome=PaymentOutcome(row.outcome) if row.outcome is not None else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def save(self, payment: Payment) -> None:
        values = self._values(payment)
        await self._database.session.execute(
            insert(payments)
            .values(payment_id=payment.payment_id, created_at=payment.created_at, **values)
            .on_conflict_do_update(index_elements=[payments.c.payment_id], set_=values)
        )

    @staticmethod
    def _values(payment: Payment) -> dict[str, Any]:
        """Every column but `payment_id` and `created_at`."""
        return {
            "order_id": payment.order_id,
            "status": payment.status,
            "status_detail": payment.status_detail,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "payment_type": payment.payment_type,
            "approved_at": payment.approved_at,
            "outcome": payment.outcome.value if payment.outcome is not None else None,
            "updated_at": payment.updated_at,
        }
