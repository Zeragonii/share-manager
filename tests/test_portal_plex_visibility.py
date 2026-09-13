from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Integration, Package, PackageEntitlement, Subscription
from app.main import _customer_has_plex_package


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_plex_portal_ui_requires_assigned_package_with_plex_library():
    db = make_db()
    customer = Customer(name="Test")
    nonplex = Package(name="Non Plex")
    nonplex_tier = BillingTier(package=nonplex, name="Monthly", price=10)
    db.add_all([customer, nonplex, nonplex_tier]); db.flush()
    db.add(Subscription(customer=customer, billing_tier=nonplex_tier, status="active")); db.commit()
    assert _customer_has_plex_package(db, customer.id) is False

    plex = Package(name="Plex")
    plex_tier = BillingTier(package=plex, name="Yearly", price=100)
    integration = Integration(kind="plex", name="Plex server", enabled=True)
    db.add_all([plex, plex_tier, integration]); db.flush()
    db.add(PackageEntitlement(package=plex, integration=integration, resource_type="library", resource_id="1", resource_name="Movies"))
    db.add(Subscription(customer=customer, billing_tier=plex_tier, status="suspended")); db.commit()
    assert _customer_has_plex_package(db, customer.id) is True


def test_cancelled_plex_package_does_not_enable_plex_portal_ui():
    db = make_db()
    customer = Customer(name="Test")
    plex = Package(name="Plex")
    tier = BillingTier(package=plex, name="Monthly", price=10)
    integration = Integration(kind="plex", name="Plex server", enabled=True)
    db.add_all([customer, plex, tier, integration]); db.flush()
    db.add(PackageEntitlement(package=plex, integration=integration, resource_type="library", resource_id="1", resource_name="Movies"))
    db.add(Subscription(customer=customer, billing_tier=tier, status="cancelled")); db.commit()
    assert _customer_has_plex_package(db, customer.id) is False
