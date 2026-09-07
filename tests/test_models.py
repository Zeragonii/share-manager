from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Package, BillingTier, Customer, Subscription


def test_package_tiers_and_subscription_relationships():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    package = Package(name="Package 1")
    tier = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1, package=package)
    customer = Customer(name="Example")
    db.add_all([package, tier, customer]); db.flush()
    subscription = Subscription(customer_id=customer.id, billing_tier_id=tier.id)
    db.add(subscription); db.commit()
    assert subscription.billing_tier.package.name == "Package 1"


def test_billing_tier_current_subscription_count_ignores_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    package = Package(name="Package 2")
    tier = BillingTier(name="Monthly", price=15, interval_unit="month", interval_count=1, package=package)
    customer = Customer(name="History Test")
    db.add_all([package, tier, customer]); db.flush()
    db.add_all([
        Subscription(customer_id=customer.id, billing_tier_id=tier.id, status="cancelled"),
        Subscription(customer_id=customer.id, billing_tier_id=tier.id, status="cancelled"),
        Subscription(customer_id=customer.id, billing_tier_id=tier.id, status="active"),
    ])
    db.commit()
    db.refresh(tier)
    assert len(tier.subscriptions) == 3
    assert tier.current_subscription_count == 1

def test_archived_tier_is_not_currently_assignable_flag():
    tier = BillingTier(package_id=1, name="Legacy", price=10, interval_unit="month", interval_count=1, active=False)
    assert tier.active is False
