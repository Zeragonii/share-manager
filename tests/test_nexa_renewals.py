"""Phase 3: NEXA provider purchases are opt-in, payment-backed and idempotent."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.integrations.nexa import (
    NexaError, NexaExtensionError, NexaIntegration, decrypt_api_key,
)
from app.models import (
    BillingTier, Customer, Integration, NexaAccount, NexaExtensionJob,
    NexaPackagePolicy, Package, Payment, Subscription,
)
from app.services import nexa_renewals as renewals


def utc_ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def sandbox(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    policy_date = datetime(2026, 11, 22, 12)
    with factory() as db:
        c = Customer(name="Paid IPTV customer", status="active")
        p = Package(name="IPTV package", active=True)
        i = Integration(kind="nexa", name="NEXA", enabled=False, secret="encrypted")
        db.add_all([c, p, i]); db.flush()
        t = BillingTier(package_id=p.id, name="Monthly", price=10,
                        interval_unit="month", interval_count=1, active=True)
        db.add(t); db.flush()
        sub = Subscription(customer_id=c.id, billing_tier_id=t.id,
                           status="active", current_period_end=datetime(2027, 9, 15),
                           current_period_start=datetime(2027, 8, 15))
        db.add(sub)
        policy = NexaPackagePolicy(
            package_id=p.id, integration_id=i.id, enabled=True,
            monthly_provider_package_id="3", yearly_provider_package_id="5",
            updated_at=policy_date,
        )
        account = NexaAccount(
            customer_id=c.id, integration_id=i.id, provider_account_id="900",
            provider_username="nexa_user", account_type="username",
            provider_status="active", provider_expiry_ts=utc_ts(2027, 9, 1),
            recorded_package_id="3", automation_enabled=True,
            automation_approved_package_id="3", automation_approved_months=6,
            automation_approved_policy_at=policy_date,
            automation_armed_at=datetime(2026, 11, 22, 12),
        )
        db.add_all([policy, account]); db.commit()
    monkeypatch.setattr(renewals, "SessionLocal", factory)
    fake = FakeNexa()
    monkeypatch.setattr(renewals, "_client", lambda db, account: fake)
    monkeypatch.setattr(renewals.settings, "app_secret", "test-phase3-strong-app-secret")
    yield factory, fake
    engine.dispose()


class FakeNexa:
    def __init__(self):
        self.expiry = utc_ts(2027, 9, 1)
        self.new_expiry = utc_ts(2028, 3, 1)
        self.status = "active"
        self.post_calls = []
        self.fail_once = False
        self.rotate_password = False

    def packages(self):
        return {"packages": [
            {"id": "3", "name": "Standard 6", "months": 6, "credits": 6},
            {"id": "5", "name": "Standard 12", "months": 12, "credits": 12},
        ], "balance": 50}

    def lookup(self, *, username="", mac=""):
        return {"id": "900", "username": username, "status": self.status,
                "is_trial": False, "exp_date": self.expiry, "type": "username"}

    def extend(self, *, account_id, package_id, months, idempotency_key):
        self.post_calls.append((account_id, package_id, months, idempotency_key))
        if self.fail_once and len(self.post_calls) == 1:
            raise NexaExtensionError("NEXA extension timed out", retryable=True, uncertain=True)
        return {
            "previous_expiry": self.expiry,
            "new_expiry": self.new_expiry,
            "account_expiry": self.new_expiry,
            "credits_charged": 6,
            "credits_remaining": 44,
            "replayed": len(self.post_calls) > 1,
            "password_changed": self.rotate_password,
            "replacement_password": "renewed-player-password" if self.rotate_password else None,
        }


def make_payment(db, *, coverage_end=None, apply=True):
    payment = Payment(customer_id=1, subscription_id=1 if apply else None,
                      amount=10, source="manual", paid_at=datetime(2026, 11, 22),
                      coverage_start=datetime(2027, 8, 15) if apply else None,
                      coverage_end=coverage_end or datetime(2027, 9, 15) if apply else None,
                      billing_periods=1 if apply else None)
    db.add(payment); db.flush()
    return payment


def enqueue(factory):
    with factory() as db:
        payment = make_payment(db)
        count = renewals.enqueue_for_payment(db, payment)
        db.commit()
        return payment.id, count


def test_arming_does_not_retroactively_queue_payments(sandbox):
    factory, fake = sandbox
    with factory() as db:
        make_payment(db)
        db.commit()
    with factory() as db:
        assert db.query(NexaExtensionJob).count() == 0
    assert not renewals.process_one_extension()
    assert not fake.post_calls


def test_payment_outbox_only_queues_applied_new_payments(sandbox):
    factory, _fake = sandbox
    with factory() as db:
        ledger_only = make_payment(db, apply=False)
        assert renewals.enqueue_for_payment(db, ledger_only) == 0
        line = db.get(NexaAccount, 1)
        line.automation_enabled = False
        paid = make_payment(db)
        assert renewals.enqueue_for_payment(db, paid) == 0
        db.commit()
    with factory() as db:
        line = db.get(NexaAccount, 1)
        line.automation_enabled = True
        applied = make_payment(db)
        assert renewals.enqueue_for_payment(db, applied) == 1
        assert renewals.enqueue_for_payment(db, applied) == 0
        db.commit()
        assert db.query(NexaExtensionJob).count() == 1



def test_zero_amount_does_not_buy_provider_credits(sandbox):
    factory, fake = sandbox
    with factory() as db:
        payment = make_payment(db)
        payment.amount = 0
        assert renewals.enqueue_for_payment(db, payment) == 0
        db.commit()
    assert not renewals.process_one_extension()
    assert not fake.post_calls


def test_payment_extension_uses_provider_expiry_and_immutable_idempotency(sandbox):
    factory, fake = sandbox
    payment_id, count = enqueue(factory)
    assert count == 1
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        original_key = job.idempotency_key
        assert job.status == "queued" and not job.post_attempted
    assert renewals.process_one_extension()
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        account = db.get(NexaAccount, 1)
        assert job.status == "completed"
        assert job.payment_id == payment_id
        assert job.new_expiry_ts == fake.new_expiry
        assert job.credits_charged == 6
        assert account.provider_expiry_ts == fake.new_expiry
        assert job.idempotency_key == original_key
    assert fake.post_calls == [("900", "3", 6, original_key)]
    assert not renewals.process_one_extension()


def test_provider_already_covers_paid_date_no_credits_spent(sandbox):
    factory, fake = sandbox
    fake.expiry = utc_ts(2028, 5, 1)
    enqueue(factory)
    assert renewals.process_one_extension()
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        assert job.status == "not_needed"
        assert job.post_attempted is False
        assert db.get(NexaAccount, 1).provider_expiry_ts == fake.expiry
    assert not fake.post_calls


def test_timeout_retries_identical_key_and_payload(sandbox):
    factory, fake = sandbox
    fake.fail_once = True
    enqueue(factory)
    with factory() as db:
        key = db.query(NexaExtensionJob).one().idempotency_key
    assert renewals.process_one_extension()
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        assert job.status == "retry"
        assert job.post_attempted is True
        job.next_attempt_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    assert renewals.process_one_extension()
    assert fake.post_calls == [("900", "3", 6, key), ("900", "3", 6, key)]
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        assert job.status == "completed" and job.replayed is True


def test_edited_payment_cancels_undispatched_job(sandbox):
    factory, fake = sandbox
    enqueue(factory)
    with factory() as db:
        pay = db.get(Payment, 1)
        renewals.on_payment_changed(db, pay)
        db.commit()
        assert db.query(NexaExtensionJob).one().status == "cancelled"
    assert not renewals.process_one_extension()
    assert not fake.post_calls


def test_voided_payment_is_rejected_by_worker(sandbox):
    factory, fake = sandbox
    enqueue(factory)
    with factory() as db:
        db.get(Payment, 1).voided_at = datetime.utcnow()
        db.commit()
    assert renewals.process_one_extension()
    with factory() as db:
        assert db.query(NexaExtensionJob).one().status == "review"
    assert not fake.post_calls


def test_mapping_change_prevents_spending(sandbox):
    factory, fake = sandbox
    enqueue(factory)
    with factory() as db:
        db.get(NexaPackagePolicy, 1).updated_at = datetime.utcnow()
        db.commit()
    assert renewals.process_one_extension()
    with factory() as db:
        assert db.query(NexaExtensionJob).one().status == "review"
    assert not fake.post_calls


def test_blocked_nexa_account_does_not_get_extended(sandbox):
    factory, fake = sandbox
    fake.status = "blocked"
    enqueue(factory)
    assert renewals.process_one_extension()
    assert not fake.post_calls
    with factory() as db:
        assert db.query(NexaExtensionJob).one().status == "review"


def test_rotated_password_is_encrypted_and_requires_manual_ack(sandbox):
    factory, fake = sandbox
    fake.rotate_password = True
    enqueue(factory)
    assert renewals.process_one_extension()
    with factory() as db:
        line = db.get(NexaAccount, 1)
        assert line.replacement_password_encrypted
        assert "renewed-player-password" not in line.replacement_password_encrypted
        assert decrypt_api_key(line.replacement_password_encrypted,
                               "test-phase3-strong-app-secret") == "renewed-player-password"
        assert line.replacement_password_ack_at is None
        assert db.query(NexaExtensionJob).one().password_changed is True


def test_account_has_unpaid_coverage_beyond_six_months_warns(sandbox):
    factory, fake = sandbox
    future = datetime(2028, 6, 1)
    with factory() as db:
        db.get(Subscription, 1).current_period_end = future
        pay = make_payment(db, coverage_end=future)
        assert renewals.enqueue_for_payment(db, pay) == 1
        db.commit()
    assert renewals.process_one_extension()
    with factory() as db:
        job = db.query(NexaExtensionJob).one()
        assert job.status == "completed"
        assert "further manual" in job.last_error
        assert job.credits_charged == 6


def test_nexa_extension_http_retry_and_strict_payload(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        assert url == "https://neva.yabadabado.store/api/reseller/v1/accounts/900/extend"
        assert kwargs["json"] == {"package_id": 3, "months": 6}
        assert kwargs["headers"]["Idempotency-Key"] == "sm-nexa-test-123"
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json={
            "success": True, "replayed": False, "password_changed": True,
            "account": {"id": 900, "exp_date": utc_ts(2028, 3, 1), "password": "new-password"},
            "previous_expiry": utc_ts(2027, 9, 1),
            "new_expiry": utc_ts(2028, 3, 1),
            "credits_charged": 6, "credits_remaining": 44,
        })
    monkeypatch.setattr(httpx, "post", post)
    response = NexaIntegration("fake-key").extend(
        account_id="900", package_id="3", months=6,
        idempotency_key="sm-nexa-test-123")
    assert response["password_changed"] and response["replacement_password"] == "new-password"
    assert response["credits_charged"] == 6
    assert len(calls) == 1
    with pytest.raises(NexaExtensionError):
        NexaIntegration("fake-key").extend(
            account_id="../evil", package_id="3", months=6,
            idempotency_key="sm-nexa-test-123")


def test_templates_parse():
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app" / "templates"))
    for name in ("nexa_accounts.html", "nexa_password.html", "nexa_package.html"):
        env.get_template(name)
