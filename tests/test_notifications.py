from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import NotificationDelivery, NotificationEndpoint
from app.services.notifications import notify_event, send_test


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class FakeResponse:
    status_code = 200
    text = "ok"
    def raise_for_status(self):
        return None


def test_home_assistant_uses_kuma_style_notify_service_path():
    db = db_session()
    endpoint = NotificationEndpoint(
        kind="home_assistant", name="HA", enabled=True,
        url="http://ha.local:8123", secret="token", target="notify.mobile_app_phone",
        events="payment.received", min_severity="info",
    )
    db.add(endpoint); db.commit()
    with patch("app.services.notifications.httpx.post", return_value=FakeResponse()) as post:
        delivery = send_test(db, endpoint)
    assert delivery.success is True
    args, kwargs = post.call_args
    assert args[0] == "http://ha.local:8123/api/services/notify/mobile_app_phone"
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["json"]["title"] == "Share Manager test notification"


def test_event_selection_and_severity_filtering():
    db = db_session()
    db.add(NotificationEndpoint(kind="webhook", name="Warnings", enabled=True, url="http://hook", events="customer.suspended", min_severity="warning"))
    db.commit()
    with patch("app.services.notifications.httpx.post", return_value=FakeResponse()) as post:
        notify_event(db, event="payment.received", title="Payment", message="paid", severity="info")
        assert post.call_count == 0
        notify_event(db, event="customer.suspended", title="Suspended", message="no access", severity="critical")
        assert post.call_count == 1


def test_event_key_deduplicates_successful_recurring_notifications():
    db = db_session()
    endpoint = NotificationEndpoint(kind="webhook", name="Hook", enabled=True, url="http://hook", events="subscription.due_soon", min_severity="info")
    db.add(endpoint); db.commit()
    with patch("app.services.notifications.httpx.post", return_value=FakeResponse()) as post:
        notify_event(db, event="subscription.due_soon", title="Due", message="soon", event_key="due:1:2026-10-01")
        notify_event(db, event="subscription.due_soon", title="Due", message="soon", event_key="due:1:2026-10-01")
    assert post.call_count == 1
    assert db.query(NotificationDelivery).filter(NotificationDelivery.success.is_(True)).count() == 1
