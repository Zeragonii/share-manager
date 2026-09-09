from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.db import Base
from app.models import (
    AuditLog, BillingTier, Customer, Integration, Package, PackageEntitlement,
    PlexReconcileJob, Subscription,
)
from app.services import reconcile as service
from app.services.billing import apply_payment, initialize_subscription_period


NOW = datetime(2026, 9, 9, 12)


class Clock(datetime):
    value = NOW

    @classmethod
    def utcnow(cls):
        return cls.value


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'retries.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    db = factory()
    package = Package(name="Plex")
    tier = BillingTier(name="Monthly", price=10, interval_unit="month", interval_count=1,
                       grace_period_days=3, package=package)
    customer = Customer(name="Example", plex_username="example", status="active")
    sub = Subscription(customer=customer, billing_tier=tier, status="active")
    initialize_subscription_period(sub, datetime(2026, 1, 1))
    integration = Integration(kind="plex", name="Server", base_url="http://plex.invalid", secret="fake")
    entitlement = PackageEntitlement(package=package, integration=integration,
                                     resource_id="1", resource_name="Movies")
    db.add_all([customer, sub, integration, entitlement])
    db.commit()
    monkeypatch.setattr(Clock, "value", NOW)
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(service, "datetime", Clock)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "_notify_due_soon", Mock())
    monkeypatch.setattr(main, "notify_event", Mock())
    client = Mock()
    client.apply_libraries.return_value = {"state": "applied", "libraries": ["Movies"]}
    monkeypatch.setattr(service, "PlexIntegration", Mock(return_value=client))
    yield db, factory, customer, sub, integration, client
    db.close()
    engine.dispose()


def test_billing_retries_failed_suspension_without_new_transition(setup, monkeypatch):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = [RuntimeError("outage"), {"state": "removed", "libraries": []}]
    assert main.run_billing_cycle() == 1
    db.expire_all()
    job = db.get(PlexReconcileJob, (customer.id, integration.id))
    assert job.attempts == 1
    assert job.next_attempt_at == NOW + timedelta(minutes=1)
    assert customer.status == "suspended"
    assert main.run_billing_cycle() == 0  # Not yet due; no repeat status event.
    assert client.apply_libraries.call_count == 1
    monkeypatch.setattr(Clock, "value", NOW + timedelta(minutes=15))
    assert main.run_billing_cycle() == 0
    assert client.apply_libraries.call_count == 2
    assert client.apply_libraries.call_args.args[1] == []
    db.expire_all()
    assert db.get(PlexReconcileJob, (customer.id, integration.id)) is None
    assert db.query(AuditLog).filter_by(action="billing.status").count() == 1
    suspension_events = [call for call in main.notify_event.call_args_list
                         if call.kwargs.get("event") == "customer.suspended"]
    assert len(suspension_events) == 1


def test_retry_survives_new_engine_and_uses_payment_reactivation(setup):
    db, factory, customer, sub, integration, client = setup
    customer.status = sub.status = "suspended"
    db.commit()
    client.apply_libraries.side_effect = RuntimeError("outage")
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    payment = apply_payment(db, customer=customer, amount=Decimal("10"), paid_at=NOW,
                            source="manual", external_reference=None, note=None)
    db.commit()
    payment_id = payment.id
    url = str(db.bind.url)
    db.close()
    # Recreate the engine as well as the session: work is stored in the DB.
    restarted_engine = create_engine(url)
    restarted_factory = sessionmaker(bind=restarted_engine, autoflush=False)
    client.apply_libraries.side_effect = None
    with restarted_factory() as restarted:
        assert service.retry_pending_reconciliations(restarted, now=NOW + timedelta(minutes=15)) == 1
        assert restarted.query(PlexReconcileJob).count() == 0
        from app.models import Payment
        assert restarted.get(Payment, payment_id).amount == Decimal("10")
    restarted_engine.dispose()
    assert client.apply_libraries.call_args.args[1] == ["Movies"]


@pytest.mark.parametrize("skip", ["exempt", "unlinked"])
def test_retry_respects_new_exemption_or_removed_identity(setup, skip):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = RuntimeError("outage")
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    if skip == "exempt":
        customer.exempt = True
    else:
        customer.plex_username = None
    db.commit()
    service.retry_pending_reconciliations(db, now=NOW + timedelta(minutes=15))
    assert client.apply_libraries.call_count == 1
    assert db.query(PlexReconcileJob).count() == 0


def test_disabled_server_waits_until_reenabled(setup):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = RuntimeError("outage")
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    integration.enabled = False
    db.commit()
    assert service.retry_pending_reconciliations(db, now=NOW + timedelta(minutes=15)) == 0
    assert db.query(PlexReconcileJob).count() == 1
    integration.enabled = True
    db.commit()
    client.apply_libraries.side_effect = None
    assert service.retry_pending_reconciliations(db, now=NOW + timedelta(minutes=15)) == 1
    assert db.query(PlexReconcileJob).count() == 0


def test_backoff_is_capped_and_explicit_action_bypasses_it(setup):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = RuntimeError("outage")
    now = NOW
    for minutes in (1, 2, 4, 8, 16, 32, 60, 60):
        with pytest.raises(RuntimeError):
            service.reconcile_customer(db, customer, now=now)
        job = db.get(PlexReconcileJob, (customer.id, integration.id))
        assert job.next_attempt_at == now + timedelta(minutes=minutes)
        assert job.last_error == "RuntimeError"
        now = job.next_attempt_at
    client.apply_libraries.side_effect = None
    service.reconcile_customer(db, customer, now=NOW)
    assert db.query(PlexReconcileJob).count() == 0


def test_one_failed_server_does_not_block_or_repeat_successful_server(setup, monkeypatch):
    db, factory, customer, sub, integration, client = setup
    other = Integration(kind="plex", name="Second", base_url="http://second.invalid", secret="fake")
    db.add(other)
    db.commit()
    failed = Mock()
    failed.apply_libraries.side_effect = RuntimeError("outage")
    good = Mock()
    good.apply_libraries.return_value = {"state": "removed", "libraries": []}
    monkeypatch.setattr(service, "PlexIntegration", lambda url, token: failed if url == integration.base_url else good)
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    assert good.apply_libraries.call_count == 1
    jobs = db.query(PlexReconcileJob).all()
    assert len(jobs) == 1 and jobs[0].integration_id == integration.id
    failed.apply_libraries.side_effect = None
    failed.apply_libraries.return_value = {"state": "applied", "libraries": ["Movies"]}
    service.retry_pending_reconciliations(db, now=NOW + timedelta(minutes=15))
    assert good.apply_libraries.call_count == 1
    assert db.query(PlexReconcileJob).count() == 0


def test_pending_invitation_is_success_and_does_not_create_repeat_invites(setup):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.return_value = {"state": "pending", "libraries": ["Movies"]}
    service.reconcile_customer(db, customer)
    service.retry_pending_reconciliations(db, now=NOW + timedelta(hours=2))
    assert client.apply_libraries.call_count == 1
    assert db.query(PlexReconcileJob).count() == 0


def test_work_is_durable_before_external_call(setup):
    db, factory, customer, sub, integration, client = setup
    observed = []
    def inspect_work(*args, **kwargs):
        with factory() as independent:
            observed.append(independent.query(PlexReconcileJob).count())
        raise RuntimeError("outage")
    client.apply_libraries.side_effect = inspect_work
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    assert observed == [1]
    assert db.query(PlexReconcileJob).count() == 1


def test_interrupted_attempt_remains_available_after_restart(setup):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        service.reconcile_customer(db, customer)
    db.close()
    client.apply_libraries.side_effect = None
    with factory() as restarted:
        assert restarted.query(PlexReconcileJob).one().attempts == 0
        assert service.retry_pending_reconciliations(restarted, now=NOW) == 1
        assert restarted.query(PlexReconcileJob).count() == 0


def test_retry_reads_new_package_assignment(setup):
    db, factory, customer, sub, integration, client = setup
    client.apply_libraries.side_effect = RuntimeError("outage")
    with pytest.raises(RuntimeError):
        service.reconcile_customer(db, customer)
    # Another request changes the package while the retry session retains an
    # older customer/subscription identity map.
    with factory() as other_request:
        other_request.get(Subscription, sub.id).status = "cancelled"
        package = Package(name="New package")
        tier = BillingTier(name="New tier", price=20, package=package)
        other_request.add(Subscription(customer_id=customer.id, billing_tier=tier, status="active"))
        other_request.add(PackageEntitlement(package=package, integration_id=integration.id,
                                             resource_id="2", resource_name="TV"))
        other_request.commit()
    client.apply_libraries.side_effect = None
    service.retry_pending_reconciliations(db, now=NOW + timedelta(minutes=15))
    assert client.apply_libraries.call_args.args[1] == ["TV"]


def test_enqueue_reconciliation_does_not_call_plex_and_is_due_immediately(setup):
    db, factory, customer, sub, integration, client = setup
    count = service.enqueue_reconciliation(db, customer, now=NOW)
    db.commit()
    assert count == 1
    assert client.apply_libraries.call_count == 0
    job = db.get(PlexReconcileJob, (customer.id, integration.id))
    assert job is not None
    assert job.attempts == 0
    assert job.next_attempt_at == NOW


def test_dedicated_queue_cycle_processes_enqueued_work(setup):
    db, factory, customer, sub, integration, client = setup
    service.enqueue_reconciliation(db, customer, now=NOW)
    db.commit()
    assert main.run_reconcile_queue_cycle() == 1
    assert client.apply_libraries.call_count == 1
    db.expire_all()
    assert db.get(PlexReconcileJob, (customer.id, integration.id)) is None
