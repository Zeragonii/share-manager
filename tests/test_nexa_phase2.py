"""NEXA Phase 2: local-only account linking and Package configuration tests."""
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.integrations.nexa import NexaError
from app.models import (
    AuditLog, Customer, Integration, NexaAccount, NexaPackagePolicy, Package,
)
from app.nexa_phase2 import (
    _apply_lookup, _expiry_label, _expiry_ts, register_nexa_phase2_routes,
    validate_package_policy,
)


CATALOG = [
    {"id": "3", "name": "Standard 6", "months": 6, "credits": 6},
    {"id": "5", "name": "Standard 12", "months": 12, "credits": 12},
    {"id": "6", "name": "Sports 4K 12", "months": 12, "credits": 24},
]
TEMPLATES = [{"id": "uk", "name": "UK channels", "source": "Own"},
             {"id": "world", "name": "World channels", "source": "Own"}]


class FakeProvider:
    def __init__(self):
        self.account_id = "150"
        self.read_calls = []
        self.write_calls = 0

    def packages(self):
        self.read_calls.append("packages")
        return {"packages": CATALOG, "balance": 80}

    def templates(self):
        self.read_calls.append("templates")
        return TEMPLATES

    def lookup(self, *, username="", mac=""):
        self.read_calls.append("lookup")
        return {
            "id": self.account_id, "username": username or "",
            "mac": mac or "", "type": "mag" if mac else "username",
            "status": "active", "is_trial": False,
            "exp_date": 1798761600,
        }


@pytest.fixture
def harness(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        integration = Integration(kind="nexa", name="NEXA IPTV", enabled=False,
                                  base_url="https://neva.yabadabado.store", secret="encrypted")
        session.add(integration)
        session.add(Customer(name="Existing IPTV customer"))
        session.add(Customer(name="Another customer"))
        session.add(Package(name="Monthly & yearly IPTV"))
        session.commit()
        integration_id = integration.id
    provider = FakeProvider()
    import app.nexa_phase2 as module
    monkeypatch.setattr(module, "_client", lambda db: (db.get(Integration, integration_id), provider))
    app = FastAPI()
    register_nexa_phase2_routes(
        app,
        auth=lambda request: None,
        render=lambda request, name, **ctx: JSONResponse({"page": name}),
    )

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, factory, provider
    engine.dispose()


def test_policy_validates_live_six_and_twelve_month_products():
    validate_package_policy(CATALOG, "3", "5", True)
    validate_package_policy(CATALOG, "", "6", True)
    with pytest.raises(NexaError, match="6-month"):
        validate_package_policy(CATALOG, "5", "", True)
    with pytest.raises(NexaError, match="12-month"):
        validate_package_policy(CATALOG, "", "3", True)
    with pytest.raises(NexaError, match="not available"):
        validate_package_policy(CATALOG, "999", "", True)
    with pytest.raises(NexaError, match="Select"):
        validate_package_policy(CATALOG, "", "", True)
    validate_package_policy(CATALOG, "", "", False)


def test_expiry_is_raw_provider_unix_seconds_not_assumed_date():
    obj = NexaAccount(provider_account_id="123", integration_id=1, customer_id=1)
    _apply_lookup(obj, {"exp_date": "1798761600", "status": "active", "type": "username"}, username="abc")
    assert obj.provider_expiry_ts == 1798761600
    assert obj.provider_status == "active"
    assert obj.provider_username == "abc"
    assert _expiry_label(obj.provider_expiry_ts).endswith("UTC")
    assert _expiry_ts({"exp_date": None}) is None
    with pytest.raises(NexaError, match="unsupported expiry"):
        _expiry_ts({"exp_date": "invalid"})


def test_link_existing_account_without_provider_writes(harness):
    client, factory, provider = harness
    response = client.post("/integrations/nexa/accounts/link", data={
        "customer_id": "1", "username": "existing_iptv",
        "recorded_package_id": "3", "desired_template_id": "uk",
    }, follow_redirects=False)
    assert response.status_code == 303
    with factory() as session:
        link = session.query(NexaAccount).one()
        assert link.customer_id == 1
        assert link.provider_account_id == "150"
        assert link.provider_expiry_ts == 1798761600
        assert link.recorded_package_id == "3"
        assert link.desired_template_id == "uk"
        assert link.confirmed_template_id is None
        assert link.automation_enabled is False
    assert "lookup" in provider.read_calls
    assert provider.write_calls == 0


def test_reject_link_to_second_customer_without_reassigning(harness):
    client, factory, provider = harness
    for customer_id in ("1", "2"):
        response = client.post("/integrations/nexa/accounts/link", data={
            "customer_id": customer_id, "username": "existing_iptv"
        }, follow_redirects=False)
        assert response.status_code == 303
    with factory() as session:
        links = session.query(NexaAccount).all()
        assert len(links) == 1
        assert links[0].customer_id == 1
    assert provider.write_calls == 0


def test_refresh_rejects_changed_provider_id_without_updates(harness):
    client, factory, provider = harness
    client.post("/integrations/nexa/accounts/link", data={"customer_id": "1", "username": "existing_iptv"})
    provider.account_id = "999"
    response = client.post("/integrations/nexa/accounts/1/refresh", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    with factory() as session:
        assert session.get(NexaAccount, 1).provider_account_id == "150"


def test_settings_reset_manual_confirmation_when_template_changes(harness):
    client, factory, provider = harness
    client.post("/integrations/nexa/accounts/link", data={
        "customer_id": "1", "username": "existing_iptv", "desired_template_id": "uk",
    })
    # Cannot claim API verification without explicit operator confirmation.
    response = client.post("/integrations/nexa/accounts/1/confirm-template", data={},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]
    response = client.post("/integrations/nexa/accounts/1/confirm-template",
                           data={"confirmed_on_provider": "yes"}, follow_redirects=False)
    assert "notice=" in response.headers["location"]
    with factory() as session:
        assert session.get(NexaAccount, 1).confirmed_template_id == "uk"
    client.post("/integrations/nexa/accounts/1/settings",
                data={"desired_template_id": "world", "recorded_package_id": "3"},
                follow_redirects=False)
    with factory() as session:
        linked = session.get(NexaAccount, 1)
        assert linked.desired_template_id == "world"
        assert linked.confirmed_template_id is None
        assert linked.template_confirmed_at is None
        assert linked.automation_enabled is False


def test_package_policy_is_local_only_and_preserves_existing_package(harness):
    client, factory, provider = harness
    response = client.post("/packages/1/nexa", data={
        "enabled": "yes", "monthly_provider_package_id": "3",
        "yearly_provider_package_id": "5", "default_template_id": "uk"
    }, follow_redirects=False)
    assert "notice=" in response.headers["location"]
    with factory() as session:
        policy = session.get(NexaPackagePolicy, 1)
        assert policy.enabled is True
        assert policy.monthly_provider_package_id == "3"
        assert policy.yearly_provider_package_id == "5"
        assert policy.default_template_id == "uk"
        assert session.get(Package, 1).name == "Monthly & yearly IPTV"
    assert provider.write_calls == 0


def test_wrong_duration_does_not_create_entitlement(harness):
    client, factory, provider = harness
    response = client.post("/packages/1/nexa", data={
        "enabled": "yes", "monthly_provider_package_id": "5"
    }, follow_redirects=False)
    assert "error=" in response.headers["location"]
    with factory() as session:
        assert session.get(NexaPackagePolicy, 1) is None


def test_unlink_removes_only_local_record(harness):
    client, factory, provider = harness
    client.post("/integrations/nexa/accounts/link", data={"customer_id": "1", "username": "existing_iptv"})
    response = client.post("/integrations/nexa/accounts/1/unlink", follow_redirects=False)
    assert response.status_code == 303
    with factory() as session:
        assert session.query(NexaAccount).count() == 0
        assert session.query(AuditLog).filter(AuditLog.action == "nexa.account_unlink").count() == 1
    assert provider.write_calls == 0


def test_phase2_templates_compile():
    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(root))
    for name in ("nexa_package.html", "nexa_accounts.html", "packages.html", "integrations.html"):
        env.get_template(name)
