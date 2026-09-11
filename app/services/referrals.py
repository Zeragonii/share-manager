import secrets
from datetime import datetime
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


def redeem_credits(db: Session, customer: Customer, *, actor: str):
    settings = ensure_referral_settings(db)
    if not settings.enabled:
        raise ValueError("The referral programme is currently disabled")
    cost = max(1, int(settings.credits_per_reward or 10))
    periods = max(1, int(settings.reward_periods or 1))
    # Lock the customer row so two simultaneous redemptions cannot overspend.
    locked = db.query(Customer).filter(Customer.id == customer.id).with_for_update().one()
    balance = credit_balance(db, locked.id)
    if balance < cost:
        raise ValueError(f"At least {cost} referral credits are required")
    from .billing import apply_subscription_credit
    credit = apply_subscription_credit(
        db,
        customer=locked,
        periods=periods,
        granted_at=datetime.utcnow(),
        reason=f"Referral reward ({cost} credits)",
        granted_by=actor,
    )
    entry = ReferralCreditEntry(
        customer_id=locked.id,
        kind="redeem",
        credits=-cost,
        description=f"Redeemed {cost} referral credits for {periods} complimentary billing period(s)",
    )
    db.add(entry)
    db.flush()
    return credit, entry
