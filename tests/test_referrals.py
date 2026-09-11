from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Package, Subscription
from app.services.billing import apply_payment, initialize_subscription_period
from app.services.referrals import ensure_customer_referral_code, assign_referrer, award_for_payment, reverse_for_payment, credit_balance


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    p = Package(name="Plex")
    t = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1, referral_credits=3, package=p)
    referrer = Customer(name="Referrer")
    referred = Customer(name="Referred")
    db.add_all([p, t, referrer, referred]); db.flush()
    ensure_customer_referral_code(db, referrer); ensure_customer_referral_code(db, referred)
    sub = Subscription(customer=referred, billing_tier=t, status="active")
    initialize_subscription_period(sub, datetime(2026, 9, 1)); db.add(sub); db.commit()
    return db, referrer, referred, sub


def test_qualifying_payment_awards_snapshot_credits_once():
    db, referrer, referred, sub = make_db()
    assign_referrer(db, referred, referrer.referral_code, actor="admin"); db.commit()
    payment = apply_payment(db, customer=referred, amount=Decimal("10"), paid_at=datetime(2026, 9, 11), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    first = award_for_payment(db, payment); second = award_for_payment(db, payment); db.commit()
    assert first.id == second.id
    assert first.credits == 3
    assert credit_balance(db, referrer.id) == 3


def test_void_reverses_exact_original_award():
    db, referrer, referred, sub = make_db()
    assign_referrer(db, referred, referrer.referral_code, actor="admin"); db.commit()
    payment = apply_payment(db, customer=referred, amount=Decimal("10"), paid_at=datetime(2026, 9, 11), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    award = award_for_payment(db, payment); db.commit()
    sub.billing_tier.referral_credits = 99; db.commit()
    reversal = reverse_for_payment(db, payment); db.commit()
    assert award.credits == 3
    assert reversal.credits == -3
    assert credit_balance(db, referrer.id) == 0


def test_relationship_is_prospective():
    db, referrer, referred, sub = make_db()
    payment = apply_payment(db, customer=referred, amount=Decimal("10"), paid_at=datetime(2026, 9, 11), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    assign_referrer(db, referred, referrer.referral_code, actor="admin"); db.commit()
    assert award_for_payment(db, payment) is None


def test_redeem_uses_tier_cost_and_extends_exactly_one_calendar_month():
    from app.models import ReferralCreditEntry
    from app.services.referrals import redeem_credits
    db, referrer, referred, sub = make_db()
    tier = sub.billing_tier
    tier.referral_redeem_cost = 100
    ref_sub = Subscription(customer=referrer, billing_tier=tier, status="active", current_period_start=datetime(2026, 9, 20), current_period_end=datetime(2026, 12, 20), grace_until=datetime(2026, 12, 23))
    db.add(ref_sub)
    db.add(ReferralCreditEntry(customer_id=referrer.id, kind="earn", credits=120, description="test balance"))
    db.commit()
    reward_sub, entry, before, after = redeem_credits(db, referrer, actor="test")
    db.commit()
    assert before == datetime(2026, 12, 20)
    assert after == datetime(2027, 1, 20)
    assert reward_sub.current_period_end == datetime(2027, 1, 20)
    assert entry.credits == -100
    assert credit_balance(db, referrer.id) == 20


def test_admin_grant_adds_spendable_balance_without_being_referral_earn():
    from sqlalchemy import func
    from app.models import ReferralCreditEntry
    from app.services.referrals import grant_account_credits
    db, referrer, referred, sub = make_db()
    row = grant_account_credits(db, referrer, credits=25, reason="Goodwill", actor="admin")
    db.commit()
    assert row.kind == "admin_grant"
    assert credit_balance(db, referrer.id) == 25
    referral_earned = int(db.query(func.coalesce(func.sum(ReferralCreditEntry.credits), 0)).filter(
        ReferralCreditEntry.customer_id == referrer.id,
        ReferralCreditEntry.kind == "earn",
        ReferralCreditEntry.credits > 0,
    ).scalar() or 0)
    assert referral_earned == 0
