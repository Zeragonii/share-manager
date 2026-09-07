from datetime import datetime
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Package, Subscription
from app.services.billing import apply_payment, initialize_subscription_period, process_billing


def make_db(grace=3):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    package = Package(name="Plex")
    tier = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1, grace_period_days=grace, package=package)
    customer = Customer(name="Example")
    db.add_all([package, tier, customer]); db.flush()
    sub = Subscription(customer=customer, billing_tier=tier, status="active")
    initialize_subscription_period(sub, datetime(2026, 9, 1))
    db.add(sub); db.commit()
    return db, customer, sub


def test_payment_during_grace_extends_from_previous_expiry():
    db, customer, sub = make_db(grace=3)
    payment = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 10, 3), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    assert payment.coverage_start == datetime(2026, 10, 1)
    assert payment.coverage_end == datetime(2026, 11, 1)
    assert sub.current_period_end == datetime(2026, 11, 1)


def test_payment_after_grace_restarts_from_payment_date():
    db, customer, sub = make_db(grace=3)
    payment = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=datetime(2026, 10, 5), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    assert payment.coverage_start == datetime(2026, 10, 5)
    assert payment.coverage_end == datetime(2026, 11, 5)


def test_billing_cycle_moves_through_grace_and_suspension():
    db, customer, sub = make_db(grace=3)
    process_billing(db, datetime(2026, 10, 2))
    db.refresh(sub); db.refresh(customer)
    assert sub.status == "grace"
    assert customer.status == "grace"
    process_billing(db, datetime(2026, 10, 5))
    db.refresh(sub); db.refresh(customer)
    assert sub.status == "suspended"
    assert customer.status == "suspended"


def test_uninitialised_v01_subscription_is_not_auto_suspended():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    package = Package(name="Legacy")
    tier = BillingTier(name="Monthly", price=10, package=package)
    customer = Customer(name="Legacy User", status="active")
    sub = Subscription(customer=customer, billing_tier=tier, status="active", current_period_end=None)
    db.add_all([package, tier, customer, sub]); db.commit()
    process_billing(db, datetime(2030, 1, 1))
    db.refresh(sub); db.refresh(customer)
    assert sub.status == "active"
    assert customer.status == "active"


def test_multi_period_payment_auto_calculates_from_amount():
    db, customer, sub = make_db(grace=3)
    payment = apply_payment(db, customer=customer, amount=Decimal("30"), paid_at=datetime(2026, 9, 15), source="manual", external_reference=None, note=None, apply_to_subscription=True)
    db.commit()
    assert payment.billing_periods == 3
    assert payment.coverage_start == datetime(2026, 10, 1)
    assert payment.coverage_end == datetime(2027, 1, 1)
    assert sub.current_period_end == datetime(2027, 1, 1)


def test_multi_period_payment_manual_override_allows_special_amount():
    db, customer, sub = make_db(grace=3)
    payment = apply_payment(db, customer=customer, amount=Decimal("25"), paid_at=datetime(2026, 9, 15), source="manual", external_reference=None, note=None, apply_to_subscription=True, billing_periods=3)
    db.commit()
    assert payment.billing_periods == 3
    assert payment.coverage_end == datetime(2027, 1, 1)


def test_auto_period_calculation_requires_whole_multiple():
    db, customer, sub = make_db(grace=3)
    import pytest
    with pytest.raises(ValueError, match="whole-number multiple"):
        apply_payment(db, customer=customer, amount=Decimal("25"), paid_at=datetime(2026, 9, 15), source="manual", external_reference=None, note=None, apply_to_subscription=True)
