from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session, joinedload

from ..models import (
    ASSIGNED_SUBSCRIPTION_STATES,
    AuditLog,
    BillingTier,
    Customer,
    Payment,
    Subscription,
)


def add_billing_interval(value: datetime, tier: BillingTier) -> datetime:
    count = max(1, int(tier.interval_count or 1))
    if tier.interval_unit == "week":
        return value + relativedelta(weeks=count)
    if tier.interval_unit == "year":
        return value + relativedelta(years=count)
    return value + relativedelta(months=count)


def grace_end(period_end: datetime, tier: BillingTier) -> datetime:
    return period_end + timedelta(days=max(0, int(tier.grace_period_days or 0)))


def initialize_subscription_period(sub: Subscription, start: datetime, explicit_end: datetime | None = None) -> None:
    """Initialise billing dates for an existing or newly assigned subscription."""
    end = explicit_end or add_billing_interval(start, sub.billing_tier)
    sub.started_at = start
    sub.current_period_start = start
    sub.current_period_end = end
    sub.grace_until = grace_end(end, sub.billing_tier)
    sub.status = "active"
    sub.cancelled_at = None


def apply_payment(
    db: Session,
    *,
    customer: Customer,
    amount: Decimal,
    paid_at: datetime,
    source: str,
    external_reference: str | None,
    note: str | None,
    apply_to_subscription: bool = True,
) -> Payment:
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.billing_tier))
        .filter(
            Subscription.customer_id == customer.id,
            Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
        )
        .order_by(Subscription.id.desc())
        .first()
    )

    payment = Payment(
        customer_id=customer.id,
        amount=amount,
        paid_at=paid_at,
        source=source,
        external_reference=external_reference,
        note=note,
    )

    if apply_to_subscription and sub:
        tier = sub.billing_tier
        # Renewals paid before expiry OR during grace extend from the existing expiry.
        # Once grace has fully elapsed, a payment begins a fresh period from receipt.
        if sub.current_period_end:
            cutoff = sub.grace_until or grace_end(sub.current_period_end, tier)
            coverage_start = sub.current_period_end if paid_at <= cutoff else paid_at
        else:
            # First billing event: the subscription begins on the date payment was received.
            coverage_start = paid_at
            sub.started_at = paid_at

        coverage_end = add_billing_interval(coverage_start, tier)
        payment.subscription = sub
        payment.coverage_start = coverage_start
        payment.coverage_end = coverage_end
        sub.current_period_start = coverage_start
        sub.current_period_end = coverage_end
        sub.grace_until = grace_end(coverage_end, tier)
        sub.status = "active"
        sub.cancelled_at = None
        if not customer.exempt:
            customer.status = "active"

    db.add(payment)
    db.flush()
    return payment


def desired_billing_status(sub: Subscription, now: datetime) -> str | None:
    """Return automatic billing status, or None for subscriptions not initialised yet."""
    if not sub.current_period_end:
        return None
    if now < sub.current_period_end:
        return "active"
    cutoff = sub.grace_until or grace_end(sub.current_period_end, sub.billing_tier)
    if now < cutoff:
        return "grace"
    return "suspended"


def process_billing(db: Session, now: datetime | None = None) -> list[Customer]:
    """Apply expiry/grace state transitions and return customers whose access state changed."""
    now = now or datetime.utcnow()
    changed_customers: dict[int, Customer] = {}
    subs = (
        db.query(Subscription)
        .options(joinedload(Subscription.customer), joinedload(Subscription.billing_tier))
        .filter(Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
        .all()
    )
    for sub in subs:
        desired = desired_billing_status(sub, now)
        if desired is None:
            continue
        previous = sub.status
        customer = sub.customer
        sub_changed = desired != sub.status
        customer_changed = (not customer.exempt and customer.status != desired)
        if not sub_changed and not customer_changed:
            continue
        sub.status = desired
        if not customer.exempt:
            customer.status = desired
            changed_customers[customer.id] = customer
        db.add(AuditLog(
            action="billing.status",
            target_type="subscription",
            target_id=str(sub.id),
            detail=f"{previous} -> {desired}; period end {sub.current_period_end:%Y-%m-%d}",
        ))
    db.commit()
    return list(changed_customers.values())
