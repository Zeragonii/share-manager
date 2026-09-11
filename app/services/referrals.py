import secrets
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from ..models import Customer, Payment, ReferralCreditEntry, ReferralSettings, Subscription, BillingTier, AuditLog, ASSIGNED_SUBSCRIPTION_STATES

CODE_MIN = 10000
CODE_MAX = 99999


def ensure_referral_settings(db: Session) -> ReferralSettings:
    row = db.get(ReferralSettings, 1)
    if row is None:
        row = ReferralSettings(id=1, enabled=True, credits_per_reward=10, reward_periods=1)
        db.add(row)
        db.flush()
    return row


def generate_referral_code(db: Session) -> str:
    # 90k random 5-digit codes; collision loop is cheap at the intended scale.
    for _ in range(200):
        code = str(CODE_MIN + secrets.randbelow(CODE_MAX - CODE_MIN + 1))
        if not db.query(Customer.id).filter(Customer.referral_code == code).first():
            return code
    raise RuntimeError("Unable to allocate a unique referral code")


def ensure_customer_referral_code(db: Session, customer: Customer) -> str:
    if customer.referral_code:
        return customer.referral_code
    customer.referral_code = generate_referral_code(db)
    db.flush()
    return customer.referral_code


def assign_referrer(db: Session, customer: Customer, code: str | None, *, actor: str) -> Customer | None:
    clean = (code or "").strip()
    if not clean:
        customer.referrer_customer_id = None
        customer.referral_started_at = None
        db.add(AuditLog(actor=actor, action="referral.unlink", target_type="customer", target_id=str(customer.id), detail="Referral relationship removed"))
        db.flush()
        return None
    if clean == customer.referral_code:
        raise ValueError("A customer cannot refer themselves")
    referrer = db.query(Customer).filter(Customer.referral_code == clean, Customer.archived.is_(False)).first()
    if not referrer:
        raise ValueError("Referral code was not found")
    if referrer.id == customer.id:
        raise ValueError("A customer cannot refer themselves")
    customer.referrer_customer_id = referrer.id
    customer.referral_started_at = datetime.utcnow()
    db.add(AuditLog(actor=actor, action="referral.link", target_type="customer", target_id=str(customer.id), detail=f"Referred by customer {referrer.id} using code {clean}"))
    db.flush()
    return referrer


def award_for_payment(db: Session, payment: Payment) -> ReferralCreditEntry | None:
    existing = db.query(ReferralCreditEntry).filter(ReferralCreditEntry.payment_id == payment.id, ReferralCreditEntry.kind == "earn").first()
    if existing:
        return existing
    customer = db.query(Customer).filter(Customer.id == payment.customer_id).first()
    if not customer or not customer.referrer_customer_id or not customer.referral_started_at:
        return None
    # Referral is prospective. A payment entered before the link existed cannot earn.
    if payment.created_at and payment.created_at < customer.referral_started_at:
        return None
    settings = ensure_referral_settings(db)
    if not settings.enabled:
        return None
    sub = None
    if payment.subscription_id:
        sub = db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(Subscription.id == payment.subscription_id).first()
    if not sub:
        sub = (db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(
            Subscription.customer_id == customer.id, Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES)
        ).order_by(Subscription.id.desc()).first())
    if not sub or not sub.billing_tier:
        return None
    credits = int(sub.billing_tier.referral_credits or 0)
    if credits <= 0:
        return None
    row = ReferralCreditEntry(
        customer_id=customer.referrer_customer_id,
        referred_customer_id=customer.id,
        payment_id=payment.id,
        billing_tier_id=sub.billing_tier_id,
        kind="earn",
        credits=credits,
        description=f"Referral payment from customer {customer.id} · {sub.billing_tier.package.name if sub.billing_tier.package else 'Package'} / {sub.billing_tier.name}",
    )
    db.add(row)
    db.flush()
    return row


def reverse_for_payment(db: Session, payment: Payment) -> ReferralCreditEntry | None:
    earned = db.query(ReferralCreditEntry).filter(ReferralCreditEntry.payment_id == payment.id, ReferralCreditEntry.kind == "earn").first()
    if not earned:
        return None
    existing = db.query(ReferralCreditEntry).filter(ReferralCreditEntry.payment_id == payment.id, ReferralCreditEntry.kind == "reversal").first()
    if existing:
        return existing
    row = ReferralCreditEntry(
        customer_id=earned.customer_id,
        referred_customer_id=earned.referred_customer_id,
        payment_id=payment.id,
        billing_tier_id=earned.billing_tier_id,
        kind="reversal",
        credits=-abs(int(earned.credits)),
        description=f"Reversal of referral credits from voided payment {payment.id}",
    )
    db.add(row)
    db.flush()
    return row


def credit_balance(db: Session, customer_id: int) -> int:
    from sqlalchemy import func
    return int(db.query(func.coalesce(func.sum(ReferralCreditEntry.credits), 0)).filter(ReferralCreditEntry.customer_id == customer_id).scalar() or 0)


def redemption_quote(db: Session, customer: Customer, *, now: datetime | None = None):
    """Return the current tier, credit cost, and exact one-month coverage change."""
    now = now or datetime.utcnow()
    sub = (db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(
        Subscription.customer_id == customer.id, Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES)
    ).order_by(Subscription.id.desc()).first())
    if not sub or not sub.billing_tier:
        raise ValueError("Customer does not have a current subscription tier")
    tier = sub.billing_tier
    cost = int(tier.referral_redeem_cost or 0)
    if cost <= 0:
        raise ValueError("Referral redemption is disabled for this billing tier")
    if sub.current_period_end:
        cutoff = sub.grace_until or (sub.current_period_end + timedelta(days=max(0, int(tier.grace_period_days or 0))))
        coverage_start = sub.current_period_end if now <= cutoff else now
    else:
        coverage_start = now
    coverage_end = coverage_start + relativedelta(months=1)
    return sub, tier, cost, coverage_start, coverage_end


def redeem_credits(db: Session, customer: Customer, *, actor: str):
    settings = ensure_referral_settings(db)
    if not settings.enabled:
        raise ValueError("The referral programme is currently disabled")
    # Lock the customer row so two simultaneous redemptions cannot overspend.
    locked = db.query(Customer).filter(Customer.id == customer.id).with_for_update().one()
    sub, tier, cost, coverage_start, coverage_end = redemption_quote(db, locked)
    balance = credit_balance(db, locked.id)
    if balance < cost:
        raise ValueError(f"At least {cost} referral credits are required")

    previous_end = sub.current_period_end
    if sub.current_period_end is None:
        sub.started_at = coverage_start
    sub.current_period_start = coverage_start
    sub.current_period_end = coverage_end
    from .billing import grace_end
    sub.grace_until = grace_end(coverage_end, tier)
    sub.status = "active"
    sub.cancelled_at = None
    if not locked.exempt:
        locked.status = "active"

    entry = ReferralCreditEntry(
        customer_id=locked.id,
        billing_tier_id=tier.id,
        kind="redeem",
        credits=-cost,
        description=(f"Redeemed {cost} referral credits for 1 month of access · "
                     f"paid through {coverage_start:%Y-%m-%d} -> {coverage_end:%Y-%m-%d}"),
    )
    db.add(entry)
    db.flush()
    return sub, entry, coverage_start, coverage_end

