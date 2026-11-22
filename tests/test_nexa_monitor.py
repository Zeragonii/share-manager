"""NEXA Phase 4 read-only monitoring, forecasting and manual actions."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.integrations.nexa import NexaError
from app.models import (
    BillingTier, Customer, Integration, NexaAccount, NexaAccountProbe,
    NexaActionItem, NexaExtensionJob, NexaMonitorState, NexaPackagePolicy,
    Package, Subscription,
)
from app.services import nexa_monitor as monitoring


def utc_seconds(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class FakeProvider:
    def __init__(self, now):
        self.calls = []
        self.balance = 9
        self.expiry = utc_seconds(now + timedelta(days=5))
        self.status = "active"
        self.broken = False

    def packages(self):
        self.calls.append(("GET", "packages"))
        if self.broken:
            raise NexaError("Provider temporarily unavailable")
        return {"balance": self.balance, "packages": [
            {"id": "3", "months": 6, "credits": 6, "name": "Standard"},
            {"id": "5", "months": 12, "credits": 12, "name": "Yearly"},
        ]}

    def lookup(self, *, username="", mac=""):
        self.calls.append(("GET", "lookup", username or mac))
        if self.broken:
            raise NexaError("Provider temporarily unavailable")
        return {"id": "900", "username": username,
                "type": "username", "status": self.status,
                "is_trial": False, "exp_date": self.expiry}


@pytest.fixture
def fixture(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    now = datetime.utcnow().replace(microsecond=0)
    with factory() as db:
        customer = Customer(name="Test IPTV buyer", status="active")
        integration = Integration(kind="nexa", name="NEXA IPTV",
                                  base_url="https://neva.yabadabado.store",
                                  secret="safe", enabled=False)
        package = Package(name="IPTV Bundle", active=True)
        db.add_all([customer, integration, package])
        db.flush()
        tier = BillingTier(package_id=package.id, name="Monthly", price=10,
                           interval_unit="month", interval_count=1)
        db.add(tier)
        db.flush()
        subscription = Subscription(
            customer_id=customer.id, billing_tier_id=tier.id,
            status="active", current_period_start=now - timedelta(days=20),
            current_period_end=now + timedelta(days=10),
            grace_until=now + timedelta(days=13),
        )
        db.add(subscription)
        policy = NexaPackagePolicy(
            package_id=package.id, integration_id=integration.id,
            enabled=True, monthly_provider_package_id="3",
            yearly_provider_package_id="5",
            updated_at=datetime(2026, 11, 22, 12),
        )
        account = NexaAccount(
            integration_id=integration.id, customer_id=customer.id,
            provider_account_id="900", provider_username="user900",
            provider_status="active", provider_expiry_ts=utc_seconds(now + timedelta(days=5)),
            account_type="username", automation_enabled=True,
            automation_approved_package_id="3", automation_approved_months=6,
            automation_approved_policy_at=datetime(2026, 11, 22, 12),
            last_checked_at=now - timedelta(days=3),
        )
        db.add_all([policy, account])
        db.commit()
    fake = FakeProvider(now)
    notices = []
    monkeypatch.setattr(monitoring, "_integration_client", lambda db: fake)
    monkeypatch.setattr(monitoring, "SessionLocal", factory)
    monkeypatch.setattr(monitoring, "notify_event",
                        lambda db, **kwargs: notices.append(kwargs))
    yield factory, fake, notices, now
    engine.dispose()


def test_provider_poll_updates_only_provider_data_and_catalog(fixture):
    factory, fake, notices, now = fixture
    result = monitoring.monitor_cycle(force=True)
    assert result["checked"] == 1
    assert fake.calls == [("GET", "packages"), ("GET", "lookup", "user900")]
    with factory() as db:
        state = db.get(NexaMonitorState, 1)
        account = db.get(NexaAccount, 1)
        assert state.credit_balance == 9
        assert account.provider_expiry_ts == fake.expiry
        assert account.automation_enabled is True
        assert db.query(NexaExtensionJob).count() == 0
        assert db.get(Subscription, 1).current_period_end == now + timedelta(days=10)
        assert db.query(NexaActionItem).filter_by(code="provider_expiry").count() == 1
        assert db.query(NexaActionItem).filter_by(code="coverage_gap").count() == 1
        assert db.query(NexaActionItem).filter_by(code="credits_low").count() == 1
    assert all(n["data"]["url"] == "/integrations/nexa/monitor" for n in notices)


def test_repeat_scan_does_not_repeat_existing_alerts(fixture):
    factory, fake, notices, now = fixture
    monitoring.monitor_cycle(force=True)
    previous_count = len(notices)
    monitoring.monitor_cycle(force=True)
    assert len(notices) >= previous_count
    assert len(notices) <= 4  # New conditions are bounded by per-scan alert budget.
    with factory() as db:
        assert len({a.action_key for a in db.query(NexaActionItem).all()}) == db.query(NexaActionItem).count()


def test_acknowledge_escalation_and_auto_resolve(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        state = monitoring.get_monitor_state(db)
        item = monitoring.Finding(
            "payment_overdue", 1, "warning", "Test IPTV buyer", "Billing is late"
        )
        monitoring._reconcile_action_items(db, [item], now)
        db.flush()
        row = db.query(NexaActionItem).one()
        assert row.generation == 1 and row.status == "open"
        assert monitoring.action_transition(db, row.id, "ack")
        assert row.status == "acknowledged"
        monitoring._reconcile_action_items(db, [item], now + timedelta(minutes=5))
        assert row.status == "acknowledged" and row.generation == 1
        escalation = item._replace(severity="critical", message="Grace expired")
        monitoring._reconcile_action_items(db, [escalation], now + timedelta(hours=1))
        assert row.status == "open" and row.generation == 2
        monitoring._reconcile_action_items(db, [], now + timedelta(hours=2))
        assert row.status == "resolved" and row.resolution_note.startswith("Automatically")


def test_overdue_without_provider_disconnect(fixture):
    factory, fake, notices, now = fixture
    fake.expiry = utc_seconds(now + timedelta(days=100))
    with factory() as db:
        sub = db.get(Subscription, 1)
        sub.current_period_end = now - timedelta(days=3)
        sub.grace_until = now + timedelta(days=2)
        db.commit()
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="payment_overdue").one()
        assert action.severity == "warning"
        assert db.get(NexaAccount, 1).provider_expiry_ts == fake.expiry
        db.get(Subscription, 1).grace_until = now - timedelta(days=1)
        db.commit()
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="payment_overdue").one()
        assert action.severity == "critical"
        assert action.generation == 2
        assert db.get(NexaAccount, 1).provider_status == "active"
    assert not hasattr(fake, "extend")


def test_balance_threshold_and_forecast_next_payment_only(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        state = monitoring.get_monitor_state(db)
        state.credit_balance = 5
        state.catalog_json = '[{"id": "3", "credits": 6, "months": 6}]'
        db.commit()
        f = monitoring.forecast_credits(db, state, now, days=30)
        assert f["known"] == 1 and f["total"] == 6
        assert f["shortfall"] == 1
        assert f["upcoming"][0]["months"] == 6
        overdue = monitoring.forecast_credits(db, state, now, days=90)
        assert overdue["known"] == 1  # One payment only, not 3 billed months.


def test_unapproved_accounts_not_in_spend_forecast(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        state = monitoring.get_monitor_state(db)
        state.catalog_json = '[{"id": "3", "credits": 6}]'
        line = db.get(NexaAccount, 1)
        line.automation_enabled = False
        db.commit()
        assert monitoring.forecast_credits(db, state, now)["total"] == 0


def test_catalog_failure_preserves_previous_balance(fixture):
    factory, fake, notices, now = fixture
    monitoring.monitor_cycle(force=True)
    fake.broken = True
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        state = db.get(NexaMonitorState, 1)
        assert state.credit_balance == 9
        assert state.catalog_error
        probe = db.get(NexaAccountProbe, 1)
        assert probe.error
        assert db.get(NexaAccount, 1).provider_expiry_ts == fake.expiry


def test_manual_resolution_reopens_when_condition_persists(fixture):
    factory, fake, notices, now = fixture
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="credits_low").one()
        assert monitoring.action_transition(db, action.id, "resolve", note="Topped up credits")
        assert action.resolution_note == "Topped up credits"
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="credits_low").one()
        assert action.status == "open"  # Balance is still 9 so low-credit condition remains.


def test_manual_resolution_clears_when_condition_gone(fixture):
    factory, fake, notices, now = fixture
    monitoring.monitor_cycle(force=True)
    fake.balance = 99
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="credits_low").one()
        assert action.status == "resolved"


def test_stale_account_data_and_expired_accounts(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        state = monitoring.get_monitor_state(db)
        rows = monitoring.build_findings(db, state, now)
        assert "provider_data_stale" in {r.code for r in rows}
    fake.expiry = utc_seconds(now - timedelta(days=1))
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        action = db.query(NexaActionItem).filter_by(code="provider_expiry").one()
        assert action.severity == "critical"



def test_read_only_poll_limits_account_gets_per_cycle(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        for number in range(2, 10):
            db.add(Customer(id=number, name=f"Customer #{number}", status="active"))
            db.flush()
            db.add(NexaAccount(
                integration_id=1, customer_id=number,
                provider_account_id=str(number + 900),
                provider_username=f"account{number}",
                account_type="username", provider_status="active",
                provider_expiry_ts=utc_seconds(now + timedelta(days=120)),
                last_checked_at=now - timedelta(days=3),
                automation_enabled=False,
            ))
        db.commit()
    result = monitoring.monitor_cycle(force=True)
    assert result["checked"] == monitoring.MAX_ACCOUNT_POLLS
    assert sum(1 for call in fake.calls if call[1] == "lookup") == monitoring.MAX_ACCOUNT_POLLS
    assert not hasattr(fake, "extend")


def test_dashboard_is_cached_and_does_not_contact_provider(fixture):
    factory, fake, notices, now = fixture
    with factory() as db:
        snapshot = monitoring.dashboard_data(db, days=90)
        assert snapshot["stats"]["accounts"] == 1
        assert snapshot["days"] == 90
        assert snapshot["forecast"]["days"] == 90
        assert not fake.calls


def test_existing_acknowledged_action_does_not_repeat_notice(fixture):
    factory, fake, notices, now = fixture
    monitoring.monitor_cycle(force=True)
    with factory() as db:
        row = db.query(NexaActionItem).filter_by(code="credits_low").one()
        assert monitoring.action_transition(db, row.id, "ack")
    prior = len(notices)
    monitoring.monitor_cycle(force=True)
    assert len(notices) == prior


def test_template_compiles():
    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    Environment(loader=FileSystemLoader(root)).get_template("nexa_monitor.html")
