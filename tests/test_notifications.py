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


def test_due_reminder_schedule_parser_normalizes_and_validates():
    from app.services.notifications import parse_due_reminder_days, format_due_reminder_days

    assert parse_due_reminder_days("1, 7,3,1,0") == [7, 3, 1, 0]
    assert format_due_reminder_days("1,7,3,1,0") == "7,3,1,0"
    try:
        parse_due_reminder_days("3,tomorrow")
        assert False, "invalid schedule should raise"
    except ValueError:
        pass


def test_due_soon_reminders_are_per_endpoint_and_deduplicated():
    from datetime import datetime, timedelta
    from app.services.notifications import notify_due_reminders
    from app.models import BillingTier, Customer, Package, Subscription

    db = db_session()
    package = Package(name="Plex", description="General")
    tier = BillingTier(package=package, name="Monthly", price=10, interval_unit="month", interval_count=1)
    customer = Customer(name="Matt", status="active", exempt=False)
    now = datetime(2026, 9, 7, 9, 0, 0)
    sub = Subscription(
        customer=customer,
        billing_tier=tier,
        status="active",
        started_at=now - timedelta(days=27),
        current_period_start=now - timedelta(days=27),
        current_period_end=datetime(2026, 9, 10, 9, 0, 0),
    )
    three_day = NotificationEndpoint(
        kind="webhook", name="Three day", enabled=True, url="http://three",
        events="subscription.due_soon", min_severity="info", due_reminder_days="3,1",
    )
    seven_day = NotificationEndpoint(
        kind="webhook", name="Seven day", enabled=True, url="http://seven",
        events="subscription.due_soon", min_severity="info", due_reminder_days="7",
    )
    db.add_all([package, tier, customer, sub, three_day, seven_day]); db.commit()

    with patch("app.services.notifications.httpx.post", return_value=FakeResponse()) as post:
        notify_due_reminders(db, now=now, fallback_days=3)
        notify_due_reminders(db, now=now, fallback_days=3)

    # Only the endpoint configured for 3 days receives it, and only once.
    assert post.call_count == 1
    assert post.call_args.args[0] == "http://three"
    delivery = db.query(NotificationDelivery).filter(NotificationDelivery.success.is_(True)).one()
    assert delivery.endpoint_id == three_day.id
    assert delivery.event_key.endswith(":3")
