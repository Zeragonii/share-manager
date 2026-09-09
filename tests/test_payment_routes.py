from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.db import Base
from app.models import BillingTier, Customer, Package, PaymentSource, Subscription
from app.services.billing import apply_payment, initialize_subscription_period, process_billing


@pytest.mark.parametrize("operation", ["edit", "void"])
@pytest.mark.parametrize("linked", [True, False])
def test_payment_coverage_refresh_does_not_consume_other_customer_transition(monkeypatch, operation, linked):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        tier = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1,
                           grace_period_days=3, package=Package(name="Plex"))
        customer = Customer(name="Edited", status="active", plex_username="edited" if linked else None)
        other = Customer(name="Other", status="active", plex_username="other")
        sub = Subscription(customer=customer, billing_tier=tier, status="active")
        other_sub = Subscription(customer=other, billing_tier=tier, status="active")
        initialize_subscription_period(sub, datetime(2000, 1, 1))
        initialize_subscription_period(other_sub, datetime(2000, 1, 1))
        db.add_all([customer, other, sub, other_sub, PaymentSource(name="manual")])
        db.commit()
        # Two payments exercise rollback without changing the separate known
        # issue of voiding the only payment (outside v0.5.2's scope).
        for day in (10, 20):
            payment = apply_payment(db, customer=customer, amount=Decimal("10"),
                                    paid_at=datetime(2000, 1, day), source="manual",
                                    external_reference=None, note=None)
            db.commit()
        monkeypatch.setattr(main, "auth", lambda request: None)
        reconcile = Mock()
        monkeypatch.setattr(main, "reconcile_customer", reconcile)
        if operation == "edit":
            response = main.edit_payment(None, payment.id, amount=Decimal("20"), paid_at="2000-01-20",
                                         source="manual", external_reference="", note="", billing_periods="2", db=db)
        else:
            response = main.delete_payment(None, payment.id, db=db)
        assert response.status_code == 303
        assert "notice=" in response.headers["location"]
        assert customer.status == sub.status == "suspended"
        assert other.status == other_sub.status == "active"
        assert reconcile.call_count == int(linked)
        if linked:
            assert reconcile.call_args.args[1].id == customer.id
        changed = process_billing(db, now=datetime(2030, 1, 1))
        assert [c.id for c in changed] == [other.id]
    engine.dispose()
