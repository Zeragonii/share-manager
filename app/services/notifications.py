from __future__ import annotations

from datetime import datetime, timedelta
import json

import httpx
from sqlalchemy.orm import Session, joinedload

from ..models import BillingTier, NotificationDelivery, NotificationEndpoint, Subscription


SEVERITY_RANK = {"info": 10, "warning": 20, "critical": 30}



def parse_due_reminder_days(raw: str | None, fallback: int = 3) -> list[int]:
    """Parse a comma-separated due reminder schedule into unique day offsets.

    Values are whole calendar days before the paid-through date. ``0`` means
    the due date itself. Keeping this parser central means UI validation and
    the billing worker interpret schedules identically.
    """
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
    "payment.received": {"label": "Payment received", "severity": "info"},
    "customer.entered_grace": {"label": "Customer entered grace", "severity": "warning"},
    "customer.suspended": {"label": "Customer suspended", "severity": "critical"},
    "customer.reactivated": {"label": "Customer reactivated", "severity": "info"},
    "subscription.due_soon": {"label": "Subscription due soon", "severity": "warning"},
    "plex.invite_sent": {"label": "Plex invitation sent", "severity": "info"},
    "plex.reconcile_failed": {"label": "Plex reconciliation failed", "severity": "critical"},
    "backup.created": {"label": "Database backup created", "severity": "info"},
    "backup.failed": {"label": "Database backup failed", "severity": "critical"},
    "backup.restored": {"label": "Database restored", "severity": "warning"},
    "tautulli.sync_failed": {"label": "Tautulli sync failed", "severity": "critical"},
    "tautulli.user_unmatched": {"label": "Tautulli user unmatched", "severity": "warning"},
    "tautulli.customer_inactive": {"label": "Customer inactive 90+ days", "severity": "warning"},
    "tautulli.never_streamed": {"label": "Customer never streamed", "severity": "warning"},
    "tautulli.suspended_streaming": {"label": "Suspended customer streaming", "severity": "critical"},
}


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
        payload = {
            "title": title,
            "message": message,
            "data": {"share_manager_event": event, "severity": severity, **(data or {})},
        }
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    elif endpoint.kind == "discord":
        payload = {
            "content": f"**{title}**\n{message}",
            "allowed_mentions": {"parse": []},
        }
        response = httpx.post(endpoint.url, json=payload, timeout=timeout)
    elif endpoint.kind == "webhook":
        payload = {
            "event": event,
            "severity": severity,
            "title": title,
            "message": message,
            "data": data or {},
            "sent_at": datetime.utcnow().isoformat() + "Z",
        }
        response = httpx.post(endpoint.url, json=payload, timeout=timeout)
    else:
        raise ValueError(f"Unsupported notification integration kind: {endpoint.kind}")

    response.raise_for_status()
    detail = response.text[:1000] if response.text else "Sent successfully"
    return response.status_code, detail


def send_test(db: Session, endpoint: NotificationEndpoint) -> NotificationDelivery:
    return _deliver(
        db,
        endpoint,
        event="notification.test",
        severity="info",
        title="Share Manager test notification",
        message=f"Notifications from {endpoint.name} are working.",
        data={"endpoint": endpoint.name},
        event_key=None,
    )


def _deliver(
    db: Session,
    endpoint: NotificationEndpoint,
    *,
    event: str,
    severity: str,
    title: str,
    message: str,
    data: dict,
    event_key: str | None,
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        endpoint_id=endpoint.id,
        event=event,
        event_key=event_key,
        severity=severity,
        title=title,
        message=message,
        success=False,
    )
    db.add(delivery)
    db.flush()
    try:
        code, detail = _send(endpoint, title=title, message=message, event=event, severity=severity, data=data)
        delivery.success = True
        delivery.response_code = code
        delivery.detail = detail
    except httpx.HTTPStatusError as exc:
        delivery.response_code = exc.response.status_code
        delivery.detail = f"HTTP {exc.response.status_code}: notification endpoint rejected the request"
    except Exception as exc:
        # Do not persist exception strings that may contain secret-bearing webhook URLs.
        delivery.detail = f"{type(exc).__name__}: notification delivery failed"
    db.commit()
    return delivery


def notify_event(
    db: Session,
    *,
    event: str,
    title: str,
    message: str,
    severity: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    event_key: str | None = None,
    data: dict | None = None,
    only_endpoint_id: int | None = None,
) -> list[NotificationDelivery]:
    """Dispatch one Share Manager event to every matching notification endpoint.

    Notification failures are recorded, not raised back into billing/entitlement logic.
    ``event_key`` enables per-endpoint deduplication for recurring checks such as due-soon.
    """
    severity = severity or EVENT_DEFINITIONS.get(event, {}).get("severity", "info")
    endpoint_query = db.query(NotificationEndpoint).filter(NotificationEndpoint.enabled.is_(True))
    if only_endpoint_id is not None:
        endpoint_query = endpoint_query.filter(NotificationEndpoint.id == only_endpoint_id)
    endpoints = endpoint_query.all()
    deliveries: list[NotificationDelivery] = []
    for endpoint in endpoints:
        selected = _selected_events(endpoint)
        if event not in selected and "*" not in selected:
            continue
        if not _severity_allowed(endpoint, severity):
            continue
        if event_key:
            prior = db.query(NotificationDelivery).filter(
                NotificationDelivery.endpoint_id == endpoint.id,
                NotificationDelivery.event_key == event_key,
                NotificationDelivery.success.is_(True),
            ).first()
            if prior:
                continue
        payload_data = {"target_type": target_type, "target_id": target_id, **(data or {})}
        deliveries.append(_deliver(
            db,
            endpoint,
            event=event,
            severity=severity,
            title=title,
            message=message,
            data=payload_data,
            event_key=event_key,
        ))
    return deliveries


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
        max_days = max(reminder_days)
        cutoff = now + timedelta(days=max_days + 1)
        due = db.query(Subscription).options(
            joinedload(Subscription.customer),
            joinedload(Subscription.billing_tier).joinedload(BillingTier.package),
        ).filter(
            Subscription.status == "active",
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end >= now,
            Subscription.current_period_end < cutoff,
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
                db,
                event="subscription.due_soon",
                title="Subscription due soon",
                message=(
                    f"{customer.name} is due {when} on {sub.current_period_end:%Y-%m-%d} "
                    f"({sub.billing_tier.package.name} / {sub.billing_tier.name})."
                ),
                target_type="subscription",
                target_id=str(sub.id),
                event_key=f"due:{endpoint.id}:{sub.id}:{sub.current_period_end:%Y-%m-%d}:{days_left}",
                data={
                    "customer": customer.name,
                    "due_date": sub.current_period_end.strftime("%Y-%m-%d"),
                    "days_remaining": days_left,
                },
                only_endpoint_id=endpoint.id,
            )
            sent += sum(1 for delivery in deliveries if delivery.success)
    return sent
