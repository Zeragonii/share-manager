"""NEXA Phase 5: safe manual/payment-backed account creation and credentials."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.integrations.nexa import (
    NexaError, NexaProvisionError, NexaIntegration, decrypt_api_key,
)
from app.models import (
    AuditLog, BillingTier, Customer, Integration, NexaAccount,
    NexaPackagePolicy, NexaProvisionJob, Package, Payment, Subscription,
)
from app.services import nexa_provisioning as provision


def utc_seconds(when):
    return int(when.replace(tzinfo=timezone.utc).timestamp())


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.return_type = None
        self.templates_available = True
        self.known_provider_account = False
        self.expiry = utc_seconds(datetime.utcnow() + timedelta(days=179))

    def packages(self):
        self.calls.append(("get", "packages"))
        return {"balance": 40, "packages": [
            {"id": "3", "name": "Standard 6", "months": 6, "credits": 6,
             "account_types": ["username", "mag"]},
            {"id": "5", "name": "Standard 12", "months": 12, "credits": 12,
             "account_types": ["username", "mag"]},
        ]}

    def templates(self):
        self.calls.append(("get", "templates"))
        return [{"id": "uk_sports", "name": "UK Sports"}] if self.templates_available else []

    def lookup(self, *, username="", mac=""):
        self.calls.append(("get", "lookup", username or mac))
        if self.known_provider_account:
            return {"id": "999", "username": username, "mac": mac}
        raise NexaError("No matching NEXA account belongs to this reseller")

    def create_account(self, **kwargs):
        self.calls.append(("post", dict(kwargs)))
        if self.fail_once and len([c for c in self.calls if c[0] == "post"]) == 1:
            raise NexaProvisionError("NEXA creation timed out; outcome uncertain", retryable=True, uncertain=True)
        return {
            "id": "777",
            "type": kwargs["account_type"],
            "username": kwargs.get("username", ""),
            "mac": kwargs.get("mac", ""),
            "exp_date": self.expiry,
            "password": "generated-secret-password" if kwargs["account_type"] == "username" else None,
            "credits_charged": 6,
            "credits_remaining": 34,
            "replayed": len([c for c in self.calls if c[0] == "post"]) > 1,
        }


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    now = datetime.utcnow().replace(microsecond=0)
    policy_time = datetime(2026, 11, 22, 12)
    with factory() as db:
        customer = Customer(name="First IPTV customer", status="active", archived=False)
        integration = Integration(kind="nexa", name="NEXA", enabled=False,
                                  secret="fake-encrypted-key")
        package = Package(name="Plex plus IPTV", active=True)
        db.add_all([customer, integration, package])
        db.flush()
        tier = BillingTier(
            package_id=package.id, name="Monthly", price=10,
            interval_unit="month", interval_count=1, active=True,
        )
        db.add(tier)
        db.flush()
        db.add(Subscription(
            customer_id=customer.id, billing_tier_id=tier.id,
            status="active", current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        ))
        db.add(NexaPackagePolicy(
            package_id=package.id, integration_id=integration.id,
            enabled=True, monthly_provider_package_id="3",
            yearly_provider_package_id="5",
            default_template_id="uk_sports",
            updated_at=policy_time,
        ))
        db.commit()
    provider = Provider()
    monkeypatch.setattr(provision, "_client", lambda integration: provider)
    monkeypatch.setattr(provision, "SessionLocal", factory)
    monkeypatch.setattr(settings, "app_secret", "phase-five-stable-very-strong-secret")
    yield factory, provider, now
    engine.dispose()


def stage(db, *, mode="payment", account_type="username", username="buyer_001", mac="",
          approve_spend=True, allow_unpaid_override=False, arm_future_renewals=True,
          template_id="uk_sports", customer_id=1):
    return provision.stage_request(
        db, customer_id=customer_id, mode=mode,
        account_type=account_type, username=username, mac=mac,
        approve_spend=approve_spend, allow_unpaid_override=allow_unpaid_override,
        arm_future_renewals=arm_future_renewals, template_id=template_id,
    )


def make_payment(db, now):
    payment = Payment(
        customer_id=1, subscription_id=1, amount=Decimal("10"),
        paid_at=now, source="manual", coverage_start=now,
        coverage_end=now + timedelta(days=60), billing_periods=1,
    )
    db.add(payment)
    db.get(Subscription, 1).current_period_end = now + timedelta(days=60)
    db.flush()
    return payment


def test_staging_never_purchases_or_uses_old_payments(setup):
    factory, fake, now = setup
    with factory() as db:
        p = make_payment(db, now)
        db.commit()
        j = stage(db, mode="payment")
        db.commit()
        assert j.status == "awaiting_payment"
    assert provision.process_one_provision() is False
    assert not [v for v in fake.calls if v[0] == "post"]
    with factory() as db:
        assert db.query(NexaAccount).count() == 0


def test_next_applied_payment_releases_staged_intent(setup):
    factory, fake, now = setup
    with factory() as db:
        job = stage(db, mode="payment")
        original_key = job.idempotency_key
        db.commit()
    with factory() as db:
        payment = make_payment(db, now)
        assert provision.activate_for_payment(db, payment) == 1
        assert provision.activate_for_payment(db, payment) == 0
        payment_id = payment.id
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        job = db.query(NexaProvisionJob).one()
        line = db.query(NexaAccount).one()
        assert job.status == "completed" and job.provider_account_id == "777"
        assert job.payment_id == payment_id
        assert job.created_nexa_account_id == line.id
        assert line.provider_expiry_ts == job_provider_expiry(fake)
        assert line.desired_template_id == "uk_sports"
        assert line.confirmed_template_id == "uk_sports"
        assert line.template_confirmed_by == "nexa_api"
        assert line.recorded_package_id == "3"
        assert line.automation_enabled and line.automation_approved_months == 6
        assert line.automation_approved_package_id == "3"
        assert line.initial_password_encrypted
        assert "generated-secret-password" not in line.initial_password_encrypted
        assert decrypt_api_key(line.initial_password_encrypted, settings.app_secret) == "generated-secret-password"
    posts = [v[1] for v in fake.calls if v[0] == "post"]
    assert len(posts) == 1
    assert posts[0] == {
        "package_id": "3", "months": 6, "account_type": "username",
        "username": "buyer_001", "mac": "", "template_id": "uk_sports",
        "idempotency_key": original_key,
    }
    assert not provision.process_one_provision()


def job_provider_expiry(fake):
    # Source of truth is the exact Unix-seconds value returned by NEXA.
    return fake.expiry


def test_cannot_manual_provision_without_payment_or_explicit_override(setup):
    factory, fake, now = setup
    with factory() as db:
        with pytest.raises(ValueError, match="No active applied payment"):
            stage(db, mode="manual")
        db.rollback()
        assert db.query(NexaProvisionJob).count() == 0
    with factory() as db:
        job = stage(db, mode="manual", allow_unpaid_override=True)
        db.commit()
        assert job.status == "queued"
    assert not [x for x in fake.calls if x[0] == "post"]
    assert provision.process_one_provision()
    with factory() as db:
        assert db.query(NexaAccount).count() == 1


def test_approved_manual_purchase_can_use_existing_paid_coverage(setup):
    factory, fake, now = setup
    with factory() as db:
        payment = make_payment(db, now)
        db.commit()
        job = stage(db, mode="manual")
        db.commit()
        assert job.payment_id == payment.id
        assert job.status == "queued"
    assert provision.process_one_provision()
    assert len([v for v in fake.calls if v[0] == "post"]) == 1


def test_duplicate_submissions_and_identifiers_are_rejected(setup):
    factory, fake, now = setup
    with factory() as db:
        stage(db, mode="payment")
        db.flush()
        with pytest.raises(ValueError, match="already has an active"):
            stage(db, mode="payment", username="another_name")
    with factory() as db:
        first = stage(db, mode="payment")
        db.commit()
    with factory() as db:
        db.add(Customer(name="Another buyer", status="active"))
        db.commit()
        # Account creation cannot be staged for a customer without a billing Package.
        with pytest.raises(ValueError, match="active Share Manager subscription"):
            stage(db, customer_id=2, mode="payment", username="another_name")
    assert not [v for v in fake.calls if v[0] == "post"]


def test_timeout_replays_same_purchase_identity(setup):
    factory, fake, now = setup
    fake.fail_once = True
    with factory() as db:
        j = stage(db, mode="manual", allow_unpaid_override=True)
        original_key = j.idempotency_key
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        job = db.query(NexaProvisionJob).one()
        assert job.status == "retry"
        assert job.post_attempted is True
        job.next_attempt_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        job = db.query(NexaProvisionJob).one()
        assert job.status == "completed" and job.replayed is True
    posts = [v[1] for v in fake.calls if v[0] == "post"]
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[1]["idempotency_key"] == original_key
    with factory() as db:
        assert db.query(NexaAccount).count() == 1


def test_mag_provisions_without_a_password(setup):
    factory, fake, now = setup
    with factory() as db:
        stage(db, mode="manual", account_type="mag",
              username="", mac="00:1A:79:42:56:78",
              allow_unpaid_override=True)
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        line = db.query(NexaAccount).one()
        assert line.account_type == "mag"
        assert line.provider_mac == "00:1A:79:42:56:78"
        assert line.initial_password_encrypted is None
    post = [v for v in fake.calls if v[0] == "post"][0][1]
    assert post["username"] == "" and post["mac"] == "00:1A:79:42:56:78"


def test_inaccessible_template_blocks_before_spending(setup):
    factory, fake, now = setup
    fake.templates_available = False
    with factory() as db:
        with pytest.raises(ValueError, match="template is unavailable"):
            stage(db, mode="payment")
    assert not [v for v in fake.calls if v[0] == "post"]


def test_existing_provider_account_blocks_creation_and_suggests_linking(setup):
    factory, fake, now = setup
    fake.known_provider_account = True
    with factory() as db:
        stage(db, mode="manual", allow_unpaid_override=True)
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        j = db.query(NexaProvisionJob).one()
        assert j.status == "review"
        assert "already has an account" in j.last_error
        assert db.query(NexaAccount).count() == 0
    assert not [x for x in fake.calls if x[0] == "post"]


def test_payment_void_cancels_unsent_provision(setup):
    factory, fake, now = setup
    with factory() as db:
        stage(db)
        payment = make_payment(db, now)
        assert provision.activate_for_payment(db, payment) == 1
        payment.voided_at = now
        provision.on_payment_changed(db, payment)
        db.commit()
    assert not provision.process_one_provision()
    with factory() as db:
        assert db.query(NexaProvisionJob).one().status == "cancelled"
    assert not [x for x in fake.calls if x[0] == "post"]


def test_policy_change_blocks_creation_before_provider_write(setup):
    factory, fake, now = setup
    with factory() as db:
        stage(db, mode="manual", allow_unpaid_override=True)
        db.commit()
    with factory() as db:
        db.get(NexaPackagePolicy, 1).updated_at = datetime.utcnow()
        db.commit()
    assert provision.process_one_provision()
    with factory() as db:
        assert db.query(NexaProvisionJob).one().status == "review"
    assert not [x for x in fake.calls if x[0] == "post"]


def test_no_consent_no_purchase(setup):
    factory, fake, now = setup
    with factory() as db:
        with pytest.raises(ValueError, match="Explicit approval"):
            stage(db, mode="manual", allow_unpaid_override=True, approve_spend=False)
    assert not [x for x in fake.calls if x[0] == "post"]


def test_endpoint_create_with_template_and_safe_retries(monkeypatch):
    calls = []
    def fake_post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(201, json={
            "success": True, "replayed": False,
            "account": {
                "id": 777, "type": "username", "username": "buyer_001",
                "password": "generated-private", "exp_date": 1798761600,
            },
            "credits_charged": 6, "credits_remaining": 44,
        })
    monkeypatch.setattr(httpx, "post", fake_post)
    result = NexaIntegration("safe-test-key").create_account(
        package_id="3", months=6, account_type="username",
        username="buyer_001", template_id="uk_sports",
        idempotency_key="sm-nexa-create-test-123",
    )
    assert result["password"] == "generated-private"
    assert result["exp_date"] == 1798761600
    assert calls[0][0].endswith("/api/reseller/v1/accounts")
    assert calls[0][1]["json"] == {
        "package_id": 3, "months": 6, "type": "username",
        "username": "buyer_001", "template_id": "uk_sports",
    }
    assert calls[0][1]["headers"]["Idempotency-Key"] == "sm-nexa-create-test-123"
    assert calls[0][1]["follow_redirects"] is False
    with pytest.raises(NexaProvisionError, match="Specify only a MAG"):
        NexaIntegration("safe-test-key").create_account(
            package_id="3", months=6, account_type="mag",
            mac="../invalid", idempotency_key="sm-nexa-create-test-123",
        )


def test_provider_mag_response_without_optional_mac_is_accepted(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(201, json={
        "success": True,
        "account": {"id": 888, "type": "mag", "exp_date": 1798761600},
        "credits_charged": 6,
    }))
    result = NexaIntegration("safe-test-key").create_account(
        package_id="3", months=6, account_type="mag",
        mac="00:1A:79:42:56:78", idempotency_key="sm-nexa-create-test-123",
    )
    assert result["mac"] == "00:1A:79:42:56:78"
    assert result["password"] is None


def test_provider_http_409_requires_manual_review(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(409, json={
        "success": False, "message": "never output secrets"
    }))
    with pytest.raises(NexaProvisionError, match="conflicting creation") as exc:
        NexaIntegration("safe-test-key").create_account(
            package_id="3", months=6, account_type="username",
            username="buyer_001", idempotency_key="sm-nexa-create-test-123",
        )
    assert not exc.value.retryable
    assert "never output" not in str(exc.value)


def test_templates_compile():
    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(root))
    for name in ("nexa_provision.html", "nexa_initial_credentials.html",
                 "nexa_accounts.html", "integrations.html", "nexa_monitor.html"):
        env.get_template(name)
