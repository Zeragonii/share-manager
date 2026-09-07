from __future__ import annotations

from datetime import datetime
import json

import httpx
from sqlalchemy.orm import Session

from ..models import NotificationDelivery, NotificationEndpoint


SEVERITY_RANK = {"info": 10, "warning": 20, "critical": 30}

EVENT_DEFINITIONS = {
    "payment.received": {"label": "Payment received", "severity": "info"},
    "customer.entered_grace": {"label": "Customer entered grace", "severity": "warning"},
    "customer.suspended": {"label": "Customer suspended", "severity": "critical"},
    "customer.reactivated": {"label": "Customer reactivated", "severity": "info"},
    "subscription.due_soon": {"label": "Subscription due soon", "severity": "warning"},
    "plex.invite_sent": {"label": "Plex invitation sent", "severity": "info"},
    "plex.reconcile_failed": {"label": "Plex reconciliation failed", "severity": "critical"},
    "backup.created": {"label": "Database backup created", "severity": "info"},
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
) -> list[NotificationDelivery]:
    """Dispatch one Share Manager event to every matching notification endpoint.

    Notification failures are recorded, not raised back into billing/entitlement logic.
    ``event_key`` enables per-endpoint deduplication for recurring checks such as due-soon.
    """
    severity = severity or EVENT_DEFINITIONS.get(event, {}).get("severity", "info")
    endpoints = db.query(NotificationEndpoint).filter(NotificationEndpoint.enabled.is_(True)).all()
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
