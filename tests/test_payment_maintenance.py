from datetime import datetime
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Package, Subscription
from app.services.billing import apply_payment, initialize_subscription_period
from app.services.payment_maintenance import payment_is_latest_coverage_event, rollback_voided_latest_payment, recalculate_after_latest_payment_change


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    package = Package(name="Plex")
    tier = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1, grace_period_days=3, package=package)
    customer = Customer(name="Example")
    sub = Subscription(customer=customer, billing_tier=tier, status="active")
    initialize_subscription_period(sub, datetime(2026, 9, 1))
    db.add_all([package, tier, customer, sub]); db.commit()
    return db, customer, sub


def test_only_latest_applied_payment_is_coverage_editable():
    db, customer, sub = make_db()
    first = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 9, 10), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    second = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 9, 20), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    assert payment_is_latest_coverage_event(db, first) is False
    assert payment_is_latest_coverage_event(db, second) is True


def test_changing_periods_on_latest_payment_recalculates_current_end():
    db, customer, sub = make_db()
    payment = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 9, 10), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    payment.billing_periods = 3
    recalculate_after_latest_payment_change(db, payment)
    assert payment.coverage_end == datetime(2027, 1, 1)
    assert sub.current_period_end == datetime(2027, 1, 1)


def test_voiding_latest_payment_rolls_subscription_back_to_previous_coverage():
    db, customer, sub = make_db()
    first = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 9, 10), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    second = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 9, 20), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    rollback_voided_latest_payment(db, second)
    second.voided_at = datetime(2026, 9, 21)
    db.commit()
    assert sub.current_period_end == first.coverage_end
