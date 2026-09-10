from __future__ import annotations

from datetime import datetime, timedelta
import base64
import json

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import webpush, WebPushException
from sqlalchemy.orm import Session, joinedload

from ..models import (
    BillingTier,
    Customer,
    CustomerNotificationPreference,
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationPlatformSettings,
    PushSubscription,
    Subscription,
    Payment,
)

SEVERITY_RANK = {"info": 10, "warning": 20, "critical": 30}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def ensure_platform_settings(db: Session) -> NotificationPlatformSettings:
    row = db.get(NotificationPlatformSettings, 1)
    if row:
        return row
    private = ec.generate_private_key(ec.SECP256R1())
    private_number = private.private_numbers().private_value.to_bytes(32, "big")
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    row = NotificationPlatformSettings(
        id=1,
        vapid_private_key=_b64url(private_number),
        vapid_public_key=_b64url(public_bytes),
        vapid_subject="mailto:admin@share-manager.local",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def parse_due_reminder_days(raw: str | None, fallback: int = 3) -> list[int]:
    value = (raw or "").strip()
    if not value:
        value = str(max(0, int(fallback)))
    days: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            day = int(token)
        except ValueError as exc:
            raise ValueError("Due reminders must be comma-separated whole numbers, e.g. 7,3,1") from exc
        if day < 0 or day > 365:
            raise ValueError("Due reminder days must be between 0 and 365")
        days.add(day)
    if not days:
        raise ValueError("Enter at least one due reminder day")
    return sorted(days, reverse=True)


def format_due_reminder_days(raw: str | None, fallback: int = 3) -> str:
    return ",".join(str(day) for day in parse_due_reminder_days(raw, fallback))


EVENT_DEFINITIONS = {
    "payment.received": {"label": "Payment received", "severity": "info", "customer": True},
    "customer.entered_grace": {"label": "Customer entered grace", "severity": "warning", "customer": True},
    "customer.suspended": {"label": "Customer suspended", "severity": "critical", "customer": True},
    "customer.reactivated": {"label": "Customer reactivated", "severity": "info", "customer": True},
    "subscription.due_soon": {"label": "Subscription due soon", "severity": "warning", "customer": True},
    "plex.invite_sent": {"label": "Plex invitation sent", "severity": "info", "customer": True},
    "plex.reconcile_failed": {"label": "Plex reconciliation failed", "severity": "critical", "customer": False},
    "backup.created": {"label": "Database backup created", "severity": "info", "customer": False},
    "backup.failed": {"label": "Database backup failed", "severity": "critical", "customer": False},
    "backup.restored": {"label": "Database restored", "severity": "warning", "customer": False},
    "tautulli.sync_failed": {"label": "Tautulli sync failed", "severity": "critical", "customer": False},
    "tautulli.user_unmatched": {"label": "Tautulli user unmatched", "severity": "warning", "customer": False},
    "tautulli.customer_inactive": {"label": "Customer inactive 90+ days", "severity": "warning", "customer": False},
    "tautulli.never_streamed": {"label": "Customer never streamed", "severity": "warning", "customer": False},
    "tautulli.suspended_streaming": {"label": "Suspended customer streaming", "severity": "critical", "customer": False},
    "stream.limit_enforced": {"label": "Stream limit enforced", "severity": "warning", "customer": True},
    "stream.limit_enforcement_failed": {"label": "Stream limit enforcement failed", "severity": "critical", "customer": False},
    "portal.notification.test": {"label": "Portal push test", "severity": "info", "customer": True},
}

CUSTOMER_PUSH_EVENTS = {key for key, meta in EVENT_DEFINITIONS.items() if meta.get("customer")}


def _selected_events(endpoint: NotificationEndpoint) -> set[str]:
    raw = endpoint.events or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _severity_allowed(endpoint: NotificationEndpoint, severity: str) -> bool:
    return SEVERITY_RANK.get(severity, 10) >= SEVERITY_RANK.get(endpoint.min_severity or "info", 10)


def _ha_service_name(target: str | None) -> str:
    value = (target or "notify").strip()
    if value.startswith("notify."):
        value = value.split(".", 1)[1]
    return value or "notify"


def _send(endpoint: NotificationEndpoint, *, title: str, message: str, event: str, severity: str, data: dict) -> tuple[int | None, str]:
    timeout = httpx.Timeout(10.0)
    if endpoint.kind == "home_assistant":
        service = _ha_service_name(endpoint.target)
        url = f"{endpoint.url.rstrip('/')}/api/services/notify/{service}"
        headers = {"Authorization": f"Bearer {endpoint.secret}", "Content-Type": "application/json"}
        payload = {"title": title, "message": message, "data": {"share_manager_event": event, "severity": severity, **(data or {})}}
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    elif endpoint.kind == "discord":
        response = httpx.post(endpoint.url, json={"content": f"**{title}**\n{message}", "allowed_mentions": {"parse": []}}, timeout=timeout)
    elif endpoint.kind == "webhook":
        response = httpx.post(endpoint.url, json={
            "event": event, "severity": severity, "title": title, "message": message,
            "data": data or {}, "sent_at": datetime.utcnow().isoformat() + "Z",
        }, timeout=timeout)
    else:
        raise ValueError(f"Unsupported notification integration kind: {endpoint.kind}")
    response.raise_for_status()
    return response.status_code, response.text[:1000] if response.text else "Sent successfully"


def _event_customer_id(db: Session, *, target_type: str | None, target_id: str | None, data: dict) -> int | None:
    raw = data.get("customer_id") if data else None
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    if target_type == "customer" and target_id:
        try:
            return int(target_id)
        except ValueError:
            return None
    if target_type == "subscription" and target_id:
        try:
            sub = db.get(Subscription, int(target_id))
            return sub.customer_id if sub else None
        except ValueError:
            return None
    if target_type == "payment" and target_id:
        try:
            payment = db.get(Payment, int(target_id))
            return payment.customer_id if payment else None
        except ValueError:
            return None
    return None


def _preference_allows(db: Session, customer_id: int, event: str) -> bool:
    if event == "portal.notification.test":
        return True
    pref = db.get(CustomerNotificationPreference, customer_id)
    if pref is None:
        return event in CUSTOMER_PUSH_EVENTS
    if not pref.push_enabled:
        return False
    selected = {x.strip() for x in (pref.events or "").split(",") if x.strip()}
    return "*" in selected or event in selected


def _push_url(event: NotificationEvent, owner_type: str) -> str:
    data = json.loads(event.data_json or "{}")
    url = str(data.get("url") or "").strip()
    if url.startswith("/"):
        return url
    if owner_type == "customer":
        if event.event.startswith("stream."):
            return "/portal/activity"
        if event.event.startswith("payment.") or event.event.startswith("subscription."):
            return "/portal/history"
        return "/portal"
    return "/integrations"


def _deliver_endpoint(db: Session, endpoint: NotificationEndpoint, event_row: NotificationEvent) -> NotificationDelivery:
    data = json.loads(event_row.data_json or "{}")
    delivery = NotificationDelivery(
        endpoint_id=endpoint.id, notification_event_id=event_row.id, channel=endpoint.kind,
        event=event_row.event, event_key=event_row.event_key, severity=event_row.severity,
        title=event_row.title, message=event_row.message, success=False,
        recipient_type="integration", recipient_id=str(endpoint.id),
    )
    db.add(delivery); db.flush()
    try:
        code, detail = _send(endpoint, title=event_row.title, message=event_row.message, event=event_row.event, severity=event_row.severity, data=data)
        delivery.success = True; delivery.response_code = code; delivery.detail = detail
    except httpx.HTTPStatusError as exc:
        delivery.response_code = exc.response.status_code
        delivery.detail = f"HTTP {exc.response.status_code}: notification endpoint rejected the request"
    except Exception as exc:
        delivery.detail = f"{type(exc).__name__}: notification delivery failed"
    db.commit()
    return delivery


def _deliver_push(db: Session, subscription: PushSubscription, event_row: NotificationEvent) -> NotificationDelivery:
    platform = ensure_platform_settings(db)
    payload = json.dumps({
        "event": event_row.event,
        "severity": event_row.severity,
        "title": event_row.title,
        "message": event_row.message,
        "url": _push_url(event_row, subscription.owner_type),
        "tag": event_row.event_key or f"share-manager-{event_row.id}",
    })
    delivery = NotificationDelivery(
        notification_event_id=event_row.id, channel="web_push", push_subscription_id=subscription.id,
        event=event_row.event, event_key=event_row.event_key, severity=event_row.severity,
        title=event_row.title, message=event_row.message, success=False,
        recipient_type=subscription.owner_type,
        recipient_id=str(subscription.customer_id) if subscription.customer_id else "admin",
    )
    db.add(delivery); db.flush()
    try:
        response = webpush(
            subscription_info={"endpoint": subscription.endpoint, "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}},
            data=payload,
            vapid_private_key=platform.vapid_private_key,
            vapid_claims={"sub": platform.vapid_subject},
            ttl=3600,
            timeout=10,
        )
        delivery.success = True
        delivery.response_code = getattr(response, "status_code", 201)
        delivery.detail = "Push accepted by browser push service"
        subscription.failure_count = 0
        subscription.last_error = None
        subscription.last_success_at = datetime.utcnow()
    except WebPushException as exc:
        status = getattr(exc, "status_code", None)
        delivery.response_code = status
        delivery.detail = f"WebPushException: push delivery failed{f' (HTTP {status})' if status else ''}"
        subscription.failure_count = int(subscription.failure_count or 0) + 1
        subscription.last_error_at = datetime.utcnow()
        subscription.last_error = delivery.detail
        if status in {404, 410}:
            subscription.enabled = False
            delivery.detail = f"HTTP {status}: push subscription expired and was disabled"
    except Exception as exc:
        delivery.detail = f"{type(exc).__name__}: push delivery failed"
        subscription.failure_count = int(subscription.failure_count or 0) + 1
        subscription.last_error_at = datetime.utcnow()
        subscription.last_error = delivery.detail
    db.commit()
    return delivery


def dispatch_event(db: Session, event_row: NotificationEvent, *, only_endpoint_id: int | None = None, include_push: bool = True, include_endpoints: bool = True) -> list[NotificationDelivery]:
    deliveries: list[NotificationDelivery] = []
    q = db.query(NotificationEndpoint).filter(NotificationEndpoint.enabled.is_(True)) if include_endpoints else db.query(NotificationEndpoint).filter(False)
    if only_endpoint_id is not None:
        q = q.filter(NotificationEndpoint.id == only_endpoint_id)
    for endpoint in q.all():
        selected = _selected_events(endpoint)
        if event_row.event not in selected and "*" not in selected:
            continue
        if not _severity_allowed(endpoint, event_row.severity):
            continue
        if event_row.event_key:
            prior = db.query(NotificationDelivery).filter(
                NotificationDelivery.endpoint_id == endpoint.id,
                NotificationDelivery.event_key == event_row.event_key,
                NotificationDelivery.success.is_(True),
            ).first()
            if prior:
                continue
        deliveries.append(_deliver_endpoint(db, endpoint, event_row))

    if include_push and only_endpoint_id is None:
        push_q = db.query(PushSubscription).filter(PushSubscription.enabled.is_(True))
        for sub in push_q.all():
            if sub.owner_type == "admin" and event_row.event == "portal.notification.test":
                continue
            if sub.owner_type == "customer":
                if not event_row.customer_id or sub.customer_id != event_row.customer_id:
                    continue
                customer = db.get(Customer, event_row.customer_id)
                if not customer or not customer.portal_enabled or customer.archived or customer.status == "cancelled":
                    continue
                if event_row.event not in CUSTOMER_PUSH_EVENTS or not _preference_allows(db, event_row.customer_id, event_row.event):
                    continue
            # Admin subscriptions receive the same operational event stream as integrations.
            if event_row.event_key:
                prior = db.query(NotificationDelivery).filter(
                    NotificationDelivery.push_subscription_id == sub.id,
                    NotificationDelivery.event_key == event_row.event_key,
                    NotificationDelivery.success.is_(True),
                ).first()
                if prior:
                    continue
            deliveries.append(_deliver_push(db, sub, event_row))
    return deliveries


def notify_event(
    db: Session, *, event: str, title: str, message: str, severity: str | None = None,
    target_type: str | None = None, target_id: str | None = None, event_key: str | None = None,
    data: dict | None = None, only_endpoint_id: int | None = None, include_endpoints: bool = True, include_push: bool = True,
) -> list[NotificationDelivery]:
    """Record a canonical event then fan it out through configured delivery channels.

    Existing HA/Discord/webhook behavior remains synchronous for compatibility; Web Push
    uses the same event record and delivery ledger. Delivery failures never roll back the
    application action that emitted the event.
    """
    data = data or {}
    severity = severity or EVENT_DEFINITIONS.get(event, {}).get("severity", "info")
    customer_id = _event_customer_id(db, target_type=target_type, target_id=target_id, data=data)
    event_row = NotificationEvent(
        event=event, event_key=event_key, severity=severity, title=title, message=message,
        target_type=target_type, target_id=target_id, customer_id=customer_id,
        data_json=json.dumps(data, default=str),
    )
    db.add(event_row); db.commit(); db.refresh(event_row)
    return dispatch_event(db, event_row, only_endpoint_id=only_endpoint_id, include_push=(include_push and only_endpoint_id is None), include_endpoints=include_endpoints)


def send_test(db: Session, endpoint: NotificationEndpoint) -> NotificationDelivery:
    event_row = NotificationEvent(event="notification.test", severity="info", title="Share Manager test notification", message=f"Notifications from {endpoint.name} are working.", data_json=json.dumps({"endpoint": endpoint.name}))
    db.add(event_row); db.commit(); db.refresh(event_row)
    return _deliver_endpoint(db, endpoint, event_row)


def save_push_subscription(db: Session, *, owner_type: str, customer_id: int | None, endpoint: str, p256dh: str, auth_key: str, user_agent: str | None) -> PushSubscription:
    if owner_type not in {"admin", "customer"}:
        raise ValueError("Invalid push subscription owner")
    if owner_type == "customer" and not customer_id:
        raise ValueError("Customer push subscription requires a customer")
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    now = datetime.utcnow()
    if row is None:
        row = PushSubscription(owner_type=owner_type, customer_id=customer_id, endpoint=endpoint, p256dh=p256dh, auth=auth_key, user_agent=user_agent, enabled=True, created_at=now, updated_at=now)
        db.add(row)
    else:
        row.owner_type = owner_type; row.customer_id = customer_id; row.p256dh = p256dh; row.auth = auth_key
        row.user_agent = user_agent; row.enabled = True; row.updated_at = now; row.failure_count = 0; row.last_error = None
    db.commit(); db.refresh(row)
    if owner_type == "customer":
        pref = db.get(CustomerNotificationPreference, customer_id)
        if pref is None:
            pref = CustomerNotificationPreference(customer_id=customer_id, push_enabled=True, events=",".join(sorted(CUSTOMER_PUSH_EVENTS - {"portal.notification.test"})), updated_at=now)
            db.add(pref)
        else:
            pref.push_enabled = True
            pref.updated_at = now
        db.commit()
    return row


def disable_push_subscription(db: Session, *, endpoint: str, owner_type: str, customer_id: int | None = None) -> bool:
    q = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint, PushSubscription.owner_type == owner_type)
    if owner_type == "customer":
        q = q.filter(PushSubscription.customer_id == customer_id)
    row = q.first()
    if not row:
        return False
    row.enabled = False; row.updated_at = datetime.utcnow(); db.commit()
    return True


def customer_push_status(db: Session, customer_id: int) -> dict:
    pref = db.get(CustomerNotificationPreference, customer_id)
    subscriptions = db.query(PushSubscription).filter(PushSubscription.owner_type == "customer", PushSubscription.customer_id == customer_id, PushSubscription.enabled.is_(True)).all()
    selected = set((pref.events if pref else ",".join(sorted(CUSTOMER_PUSH_EVENTS))).split(","))
    return {"enabled": bool(subscriptions) and (pref.push_enabled if pref else True), "devices": len(subscriptions), "events": selected, "available_events": {k: v for k, v in EVENT_DEFINITIONS.items() if k in CUSTOMER_PUSH_EVENTS and k != "portal.notification.test"}}


def update_customer_preferences(db: Session, customer_id: int, *, enabled: bool, events: list[str]) -> CustomerNotificationPreference:
    valid = [e for e in events if e in CUSTOMER_PUSH_EVENTS]
    row = db.get(CustomerNotificationPreference, customer_id)
    if row is None:
        row = CustomerNotificationPreference(customer_id=customer_id)
        db.add(row)
    row.push_enabled = enabled
    row.events = ",".join(sorted(set(valid)))
    row.updated_at = datetime.utcnow()
    db.commit(); db.refresh(row)
    return row


def send_portal_test(db: Session, customer: Customer) -> list[NotificationDelivery]:
    return notify_event(db, event="portal.notification.test", title="Share Manager notifications enabled", message="Push notifications are working on this device.", target_type="customer", target_id=str(customer.id), data={"customer_id": customer.id, "url": "/portal"}, include_endpoints=False)


def send_admin_push_test(db: Session) -> list[NotificationDelivery]:
    return notify_event(db, event="notification.test", title="Share Manager admin push", message="Admin Web Push notifications are working.", data={"url": "/integrations"})


def notify_due_reminders(db: Session, *, now: datetime, fallback_days: int = 3) -> int:
    """Send endpoint-specific staged subscription renewal reminders."""
    sent = 0
    endpoints = db.query(NotificationEndpoint).filter(NotificationEndpoint.enabled.is_(True)).all()
    for endpoint in endpoints:
        selected = _selected_events(endpoint)
        if "subscription.due_soon" not in selected and "*" not in selected:
            continue
        try:
            reminder_days = parse_due_reminder_days(endpoint.due_reminder_days, fallback=fallback_days)
        except ValueError:
            continue
        cutoff = now + timedelta(days=max(reminder_days) + 1)
        due = db.query(Subscription).options(joinedload(Subscription.customer), joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).filter(
            Subscription.status == "active", Subscription.current_period_end.is_not(None), Subscription.current_period_end >= now, Subscription.current_period_end < cutoff,
        ).all()
        for sub in due:
            customer = sub.customer
            if customer.exempt:
                continue
            days_left = (sub.current_period_end.date() - now.date()).days
            if days_left not in reminder_days:
                continue
            when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
            deliveries = notify_event(
                db, event="subscription.due_soon", title="Subscription due soon",
                message=f"{customer.name} is due {when} on {sub.current_period_end:%Y-%m-%d} ({sub.billing_tier.package.name} / {sub.billing_tier.name}).",
                target_type="subscription", target_id=str(sub.id),
                event_key=f"due:{endpoint.id}:{sub.id}:{sub.current_period_end:%Y-%m-%d}:{days_left}",
                data={"customer": customer.name, "customer_id": customer.id, "due_date": sub.current_period_end.strftime("%Y-%m-%d"), "days_remaining": days_left},
                only_endpoint_id=endpoint.id,
            )
            sent += sum(1 for delivery in deliveries if delivery.success)

    # Customer push reminders use the application default cadence, independent of admin endpoints.
    reminder_days = [max(0, int(fallback_days))]
    cutoff = now + timedelta(days=max(reminder_days) + 1)
    due = db.query(Subscription).options(joinedload(Subscription.customer), joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).filter(
        Subscription.status == "active", Subscription.current_period_end.is_not(None), Subscription.current_period_end >= now, Subscription.current_period_end < cutoff,
    ).all()
    for sub in due:
        if sub.customer.exempt:
            continue
        days_left = (sub.current_period_end.date() - now.date()).days
        if days_left not in reminder_days:
            continue
        when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        notify_event(
            db, event="subscription.due_soon", title="Subscription due soon",
            message=f"Your {sub.billing_tier.package.name} access is due {when} on {sub.current_period_end:%Y-%m-%d}.",
            target_type="subscription", target_id=str(sub.id),
            event_key=f"push-due:{sub.id}:{sub.current_period_end:%Y-%m-%d}:{days_left}",
            data={"customer_id": sub.customer_id, "due_date": sub.current_period_end.strftime("%Y-%m-%d"), "days_remaining": days_left, "url": "/portal/history"},
            include_endpoints=False,
        )
    return sent
