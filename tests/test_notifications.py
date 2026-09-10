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


def test_critical_broadcast_audience_respects_master_push_setting_not_categories():
    from app.models import Customer, CustomerNotificationPreference, PushSubscription
    from app.services.notifications import critical_broadcast_audience

    db = db_session()
    enabled = Customer(name="Enabled", status="active", portal_enabled=True)
    disabled = Customer(name="Disabled", status="active", portal_enabled=True)
    cancelled = Customer(name="Cancelled", status="cancelled", portal_enabled=True)
    db.add_all([enabled, disabled, cancelled]); db.flush()
    db.add_all([
        CustomerNotificationPreference(customer_id=enabled.id, push_enabled=True, events=""),
        CustomerNotificationPreference(customer_id=disabled.id, push_enabled=False, events="payment.received"),
        CustomerNotificationPreference(customer_id=cancelled.id, push_enabled=True, events="*"),
        PushSubscription(owner_type="customer", customer_id=enabled.id, endpoint="https://push/1", p256dh="a", auth="b", enabled=True),
        PushSubscription(owner_type="customer", customer_id=enabled.id, endpoint="https://push/2", p256dh="a", auth="b", enabled=True),
        PushSubscription(owner_type="customer", customer_id=disabled.id, endpoint="https://push/3", p256dh="a", auth="b", enabled=True),
        PushSubscription(owner_type="customer", customer_id=cancelled.id, endpoint="https://push/4", p256dh="a", auth="b", enabled=True),
    ])
    db.commit()

    audience = critical_broadcast_audience(db)
    assert audience == {"customers": 1, "devices": 2}


def test_critical_broadcast_bypasses_event_categories_but_not_master_disable():
    from app.models import Customer, CustomerNotificationPreference, NotificationDelivery, PushSubscription
    from app.services.notifications import send_critical_customer_broadcast

    db = db_session()
    enabled = Customer(name="Enabled", status="active", portal_enabled=True)
    disabled = Customer(name="Disabled", status="active", portal_enabled=True)
    db.add_all([enabled, disabled]); db.flush()
    good_sub = PushSubscription(owner_type="customer", customer_id=enabled.id, endpoint="https://push/good", p256dh="a", auth="b", enabled=True)
    bad_sub = PushSubscription(owner_type="customer", customer_id=disabled.id, endpoint="https://push/bad", p256dh="a", auth="b", enabled=True)
    db.add_all([
        CustomerNotificationPreference(customer_id=enabled.id, push_enabled=True, events="payment.received"),
        CustomerNotificationPreference(customer_id=disabled.id, push_enabled=False, events="*"),
        good_sub, bad_sub,
    ]); db.commit()

    def fake_delivery(_db, sub, event):
        return NotificationDelivery(
            notification_event_id=event.id, channel="web_push", push_subscription_id=sub.id,
            event=event.event, severity=event.severity, title=event.title, message=event.message,
            success=True, recipient_type="customer", recipient_id=str(sub.customer_id),
        )

    with patch("app.services.notifications._deliver_push", side_effect=fake_delivery) as deliver:
        event, deliveries = send_critical_customer_broadcast(
            db, title="Maintenance", message="Service will be unavailable briefly.", url="/portal"
        )

    assert event.event == "system.critical_broadcast"
    assert event.severity == "critical"
    assert len(deliveries) == 1
    assert deliver.call_args.args[1].customer_id == enabled.id


def test_critical_broadcast_rejects_non_portal_destination():
    from app.services.notifications import send_critical_customer_broadcast

    db = db_session()
    try:
        send_critical_customer_broadcast(db, title="Maintenance", message="Notice", url="/integrations")
        assert False, "admin destinations must not be accepted for customer broadcasts"
    except ValueError as exc:
        assert "customer portal path" in str(exc)


def test_admin_push_preferences_default_to_all_and_can_filter_events():
    from app.models import AdminNotificationPreference, NotificationDelivery, PushSubscription
    from app.services.notifications import admin_push_status, notify_event, update_admin_preferences

    db = db_session()
    sub = PushSubscription(owner_type="admin", customer_id=None, endpoint="https://push/admin", p256dh="a", auth="b", enabled=True)
    db.add(sub); db.commit()

    status = admin_push_status(db)
    assert status["enabled"] is True
    assert "backup.failed" in status["events"]
    assert "tautulli.sync_failed" in status["events"]

    update_admin_preferences(db, enabled=True, events=["backup.failed"])
    status = admin_push_status(db)
    assert status["events"] == {"backup.failed"}

    def fake_delivery(_db, push_sub, event):
        return NotificationDelivery(
            notification_event_id=event.id, channel="web_push", push_subscription_id=push_sub.id,
            event=event.event, severity=event.severity, title=event.title, message=event.message,
            success=True, recipient_type="admin", recipient_id="admin",
        )

    with patch("app.services.notifications._deliver_push", side_effect=fake_delivery) as deliver:
        notify_event(db, event="backup.failed", title="Backup failed", message="x", include_endpoints=False)
        notify_event(db, event="tautulli.sync_failed", title="Tautulli failed", message="y", include_endpoints=False)
    assert deliver.call_count == 1


def test_admin_push_master_disable_blocks_normal_events_but_not_test():
    from app.models import NotificationDelivery, PushSubscription
    from app.services.notifications import notify_event, send_admin_push_test, update_admin_preferences

    db = db_session()
    sub = PushSubscription(owner_type="admin", customer_id=None, endpoint="https://push/admin2", p256dh="a", auth="b", enabled=True)
    db.add(sub); db.commit()
    update_admin_preferences(db, enabled=False, events=["backup.failed"])

    def fake_delivery(_db, push_sub, event):
        return NotificationDelivery(
            notification_event_id=event.id, channel="web_push", push_subscription_id=push_sub.id,
            event=event.event, severity=event.severity, title=event.title, message=event.message,
            success=True, recipient_type="admin", recipient_id="admin",
        )

    with patch("app.services.notifications._deliver_push", side_effect=fake_delivery) as deliver:
        notify_event(db, event="backup.failed", title="Backup failed", message="x", include_endpoints=False)
        assert deliver.call_count == 0
        send_admin_push_test(db)
        assert deliver.call_count == 1
