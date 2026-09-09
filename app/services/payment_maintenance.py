from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Payment, SubscriptionCredit
from .billing import add_billing_intervals, grace_end


def latest_entitlement_event(db: Session, subscription_id: int, exclude_payment_id: int | None = None):
    events = []
    payments = db.query(Payment).filter(
        Payment.subscription_id == subscription_id,
        Payment.coverage_end.is_not(None),
        Payment.voided_at.is_(None),
    ).all()
    for payment in payments:
        if exclude_payment_id is not None and payment.id == exclude_payment_id:
            continue
        events.append((payment.coverage_end, payment.coverage_start, "payment", payment.id))
    credits = db.query(SubscriptionCredit).filter(SubscriptionCredit.subscription_id == subscription_id).all()
    for credit in credits:
        events.append((credit.coverage_end, credit.coverage_start, "credit", credit.id))
    return max(events, key=lambda item: (item[0], item[3])) if events else None


def payment_is_latest_coverage_event(db: Session, payment: Payment) -> bool:
    if not payment.subscription_id or not payment.coverage_end:
        return True
    latest = latest_entitlement_event(db, payment.subscription_id)
    return bool(latest and latest[2] == "payment" and latest[3] == payment.id)


def recalculate_after_latest_payment_change(db: Session, payment: Payment) -> None:
    if not payment.subscription or not payment.coverage_start or not payment.billing_periods:
        return
    payment.coverage_end = add_billing_intervals(
        payment.coverage_start,
        payment.subscription.billing_tier,
        payment.billing_periods,
    )
    sub = payment.subscription
    sub.current_period_start = payment.coverage_start
    sub.current_period_end = payment.coverage_end
    sub.grace_until = grace_end(payment.coverage_end, sub.billing_tier)


def rollback_voided_latest_payment(db: Session, payment: Payment) -> None:
    if not payment.subscription:
        return
    sub = payment.subscription

    # Newer payments carry an exact snapshot of the state they replaced. Prefer
    # that over reconstructing history from ledger rows.
    if getattr(payment, "prior_state_captured", False):
        sub.started_at = payment.prior_started_at or sub.started_at
        sub.current_period_start = payment.prior_period_start
        sub.current_period_end = payment.prior_period_end
        sub.grace_until = payment.prior_grace_until
        if payment.prior_subscription_status:
            sub.status = payment.prior_subscription_status
        if not sub.customer.exempt and payment.prior_customer_status:
            sub.customer.status = payment.prior_customer_status
        return

    # Historical rows created before v0.5.3 do not have a snapshot. We can still
    # reconstruct safely when an older payment/credit exists. If there is no prior
    # ledger event, refusing the void is safer than silently granting indefinite
    # undated access or destroying manually initialised coverage.
    previous = latest_entitlement_event(db, sub.id, exclude_payment_id=payment.id)
    if previous:
        sub.current_period_start = previous[1]
        sub.current_period_end = previous[0]
        sub.grace_until = grace_end(previous[0], sub.billing_tier)
        return

    raise ValueError(
        "This legacy payment has no recoverable pre-payment billing state. "
        "Set the customer's correct billing dates first or leave this payment in place."
    )
