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
    SubscriptionCredit,
)


def add_billing_intervals(value: datetime, tier: BillingTier, periods: int = 1) -> datetime:
    periods = max(1, int(periods or 1))
    count = max(1, int(tier.interval_count or 1)) * periods
    if tier.interval_unit == "week":
        return value + relativedelta(weeks=count)
    if tier.interval_unit == "year":
        return value + relativedelta(years=count)
    return value + relativedelta(months=count)


def add_billing_interval(value: datetime, tier: BillingTier) -> datetime:
    return add_billing_intervals(value, tier, 1)


def payment_period_count(amount: Decimal, tier: BillingTier, requested_periods: int | None = None) -> int:
    """Resolve how many billing periods a payment buys.

    A manual period count wins, which lets an operator record discounts, prepayments,
    or other arrangements without changing the tier price. With no override, the
    amount must be an exact whole-number multiple of the tier price.
    """
    if requested_periods is not None:
        periods = int(requested_periods)
        if periods < 1:
            raise ValueError("Billing periods must be at least 1")
        return periods

    price = Decimal(str(tier.price))
    amount = Decimal(str(amount))
    if price <= 0:
        raise ValueError("Tier price must be greater than zero to calculate billing periods automatically")
    ratio = amount / price
    integral = ratio.to_integral_value()
    if ratio != integral or integral < 1:
        raise ValueError(
            f"£{amount:.2f} is not a whole-number multiple of the £{price:.2f} tier price; "
            "enter the number of billing periods manually"
        )
    return int(integral)


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
    billing_periods: int | None = None,
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
        # Capture the exact pre-payment entitlement state so voiding the latest
        # payment can faithfully restore it. This also covers manually initialised
        # coverage that has no older payment/credit ledger event.
        payment.prior_state_captured = True
        payment.prior_started_at = sub.started_at
        payment.prior_period_start = sub.current_period_start
        payment.prior_period_end = sub.current_period_end
        payment.prior_grace_until = sub.grace_until
        payment.prior_subscription_status = sub.status
        payment.prior_customer_status = customer.status
        tier = sub.billing_tier
        periods = payment_period_count(amount, tier, billing_periods)
        # Renewals paid before expiry OR during grace extend from the existing expiry.
        # Once grace has fully elapsed, a payment begins a fresh period from receipt.
        if sub.current_period_end:
            cutoff = sub.grace_until or grace_end(sub.current_period_end, tier)
            coverage_start = sub.current_period_end if paid_at <= cutoff else paid_at
        else:
            # First billing event: the subscription begins on the date payment was received.
            coverage_start = paid_at
            sub.started_at = paid_at

        coverage_end = add_billing_intervals(coverage_start, tier, periods)
        payment.subscription = sub
        payment.coverage_start = coverage_start
        payment.coverage_end = coverage_end
        payment.billing_periods = periods
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



def apply_subscription_credit(
    db: Session,
    *,
    customer: Customer,
    periods: int,
    granted_at: datetime,
    reason: str | None,
    granted_by: str,
) -> SubscriptionCredit:
    """Grant complimentary billing periods without creating a fake payment."""
    periods = int(periods)
    if periods < 1:
        raise ValueError("Complimentary periods must be at least 1")

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
    if not sub:
        # A complimentary grant may intentionally bring a former subscriber
        # back onto their most recent tier. Historical rows therefore remain a
        # valid tier source even though ordinary reconciliation ignores them.
        sub = (
            db.query(Subscription)
            .options(joinedload(Subscription.billing_tier))
            .filter(Subscription.customer_id == customer.id)
            .order_by(Subscription.id.desc())
            .first()
        )
    if not sub:
        raise ValueError("Customer has never been assigned a subscription tier")

    tier = sub.billing_tier
    if sub.current_period_end:
        cutoff = sub.grace_until or grace_end(sub.current_period_end, tier)
        coverage_start = sub.current_period_end if granted_at <= cutoff else granted_at
    else:
        coverage_start = granted_at
        sub.started_at = granted_at

    coverage_end = add_billing_intervals(coverage_start, tier, periods)
    credit = SubscriptionCredit(
        customer_id=customer.id,
        subscription=sub,
        billing_periods=periods,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        reason=(reason or '').strip() or None,
        granted_at=granted_at,
        granted_by=granted_by,
    )

    sub.current_period_start = coverage_start
    sub.current_period_end = coverage_end
    sub.grace_until = grace_end(coverage_end, tier)
    sub.status = "active"
    sub.cancelled_at = None
    if not customer.exempt:
        customer.status = "active"

    db.add(credit)
    db.flush()
    return credit

def desired_billing_status(sub: Subscription, now: datetime) -> str | None:
    """Return automatic access status for a subscription.

    ``manual_access_end`` is a temporary access guarantee. While the override is
    in the future, access remains active regardless of the paid-through date.
    Once that date is reached, the override expires and normal billing/grace
    rules take over again. Billing history is never rewritten.
    """
    if sub.manual_access_end is not None and now < sub.manual_access_end:
        return "active"
    if not sub.current_period_end:
        return None
    if now < sub.current_period_end:
        return "active"
    cutoff = sub.grace_until or grace_end(sub.current_period_end, sub.billing_tier)
    if now < cutoff:
        return "grace"
    return "suspended"


def process_billing(db: Session, now: datetime | None = None, *, customer_id: int | None = None) -> list[Customer]:
    """Apply transitions globally, or for one customer's immediate coverage edit."""
    now = now or datetime.utcnow()
    changed_customers: dict[int, Customer] = {}
    query = (
        db.query(Subscription)
        .options(joinedload(Subscription.customer), joinedload(Subscription.billing_tier))
        .filter(Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES), Subscription.customer.has(Customer.archived.is_(False)))
    )
    if customer_id is not None:
        query = query.filter(Subscription.customer_id == customer_id)
    subs = query.all()
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
            detail=(f"{previous} -> {desired}; manual access until {sub.manual_access_end:%Y-%m-%d}" if sub.manual_access_end else f"{previous} -> {desired}; period end {sub.current_period_end:%Y-%m-%d}"),
        ))
    db.commit()
    return list(changed_customers.values())
