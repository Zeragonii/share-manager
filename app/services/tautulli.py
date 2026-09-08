from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..integrations.tautulli import TautulliIntegration, TautulliUser
from ..models import AuditLog, Customer, Integration, TautulliActivity, TautulliSettings
from .notifications import notify_event


@dataclass
class TautulliSyncResult:
    matched: int
    unmatched: int
    synced: int


_live_lock = Lock()
_live_cache: dict[str, Any] = {"sampled_at": None, "sessions": [], "error": None}


def get_tautulli_settings(db: Session) -> TautulliSettings:
    row = db.get(TautulliSettings, 1)
    if row is None:
        row = TautulliSettings(id=1, sync_interval_minutes=30, live_refresh_seconds=10)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def match_customer(db: Session, user: TautulliUser) -> Customer | None:
    if user.user_id:
        by_id = db.query(Customer).filter(Customer.plex_user_id == str(user.user_id)).first()
        if by_id:
            return by_id
    identities = {_norm(user.username), _norm(user.email), _norm(user.friendly_name)} - {""}
    if not identities:
        return None
    customers = db.query(Customer).filter(Customer.plex_username.is_not(None)).all()
    for customer in customers:
        if _norm(customer.plex_username) in identities or (_norm(customer.email) and _norm(customer.email) in identities):
            return customer
    return None


def _client(settings_row: TautulliSettings) -> TautulliIntegration:
    integration = settings_row.integration
    if not integration or integration.kind != "tautulli" or not integration.enabled:
        raise RuntimeError("Tautulli integration is not enabled")
    return TautulliIntegration(integration.base_url or "", integration.secret or "")


def sync_tautulli(db: Session, *, now: datetime | None = None) -> TautulliSyncResult:
    now = now or datetime.utcnow()
    settings_row = get_tautulli_settings(db)
    client = _client(settings_row)
    matched = 0
    unmatched = 0
    synced = 0
    unmatched_names: list[str] = []
    try:
        users = client.users()
        for user in users:
            customer = match_customer(db, user)
            if not customer:
                unmatched += 1
                unmatched_names.append(user.friendly_name or user.username or user.user_id)
                continue
            matched += 1
            stats = client.watch_time_stats(user.user_id)
            history = client.latest_history(user.user_id)
            last_streamed = None
            last_title = None
            if history:
                last_streamed = history.get("last_streamed_at")
                last_title = history.get("last_title")
            if not last_streamed:
                details = client.user_details(user.user_id)
                try:
                    ts = int(details.get("last_seen") or 0)
                    if ts > 0:
                        last_streamed = datetime.utcfromtimestamp(ts)
                except (TypeError, ValueError):
                    pass
            activity = db.query(TautulliActivity).filter(TautulliActivity.customer_id == customer.id).first()
            if not activity:
                activity = TautulliActivity(customer_id=customer.id, tautulli_user_id=user.user_id)
                db.add(activity)
            activity.tautulli_user_id = user.user_id
            activity.tautulli_username = user.friendly_name or user.username
            activity.last_streamed_at = last_streamed
            activity.last_title = last_title
            activity.watch_time_30d = stats["watch_time_30d"]
            activity.plays_30d = stats["plays_30d"]
            activity.watch_time_lifetime = stats["watch_time_lifetime"]
            activity.plays_lifetime = stats["plays_lifetime"]
            activity.synced_at = now
            synced += 1

            if not customer.exempt and customer.status in {"active", "grace"}:
                if last_streamed and last_streamed <= now - timedelta(days=90):
                    notify_event(
                        db, event="tautulli.customer_inactive", title="Customer inactive for 90 days",
                        message=f"{customer.name} has not streamed since {last_streamed:%Y-%m-%d}.",
                        severity="warning", target_type="customer", target_id=str(customer.id),
                        event_key=f"tautulli-inactive:{customer.id}:{now:%Y-%m}", data={"customer": customer.name},
                    )
                elif not last_streamed and customer.created_at <= now - timedelta(days=14):
                    notify_event(
                        db, event="tautulli.never_streamed", title="Customer has never streamed",
                        message=f"{customer.name} has no Tautulli playback history after at least 14 days.",
                        severity="warning", target_type="customer", target_id=str(customer.id),
                        event_key=f"tautulli-never:{customer.id}:{now:%Y-%m}", data={"customer": customer.name},
                    )

        settings_row.last_sync_at = now
        settings_row.last_sync_success_at = now
        settings_row.last_sync_error = None
        settings_row.last_matched_count = matched
        settings_row.last_unmatched_count = unmatched
        db.add(AuditLog(actor="system", action="tautulli.sync", target_type="integration", target_id=str(settings_row.integration_id), detail=f"Matched {matched}; unmatched {unmatched}; synced {synced}"))
        db.commit()
        if unmatched:
            notify_event(
                db, event="tautulli.user_unmatched", title="Tautulli users could not be matched",
                message=f"{unmatched} Tautulli user(s) could not be matched to Share Manager customers.",
                severity="warning", target_type="integration", target_id=str(settings_row.integration_id),
                event_key=f"tautulli-unmatched:{now:%Y-%m-%d}", data={"count": unmatched, "users": unmatched_names[:10]},
            )
        return TautulliSyncResult(matched=matched, unmatched=unmatched, synced=synced)
    except Exception as exc:
        settings_row.last_sync_at = now
        settings_row.last_sync_error = f"{type(exc).__name__}: {exc}"[:1000]
        db.add(AuditLog(actor="system", action="tautulli.sync.failed", target_type="integration", target_id=str(settings_row.integration_id), detail=type(exc).__name__))
        db.commit()
        notify_event(
            db, event="tautulli.sync_failed", title="Tautulli sync failed",
            message="Share Manager could not refresh Tautulli usage analytics.",
            severity="critical", target_type="integration", target_id=str(settings_row.integration_id),
            event_key=f"tautulli-sync-failed:{now:%Y-%m-%d-%H}", data={"error_type": type(exc).__name__},
        )
        raise


def sync_due(settings_row: TautulliSettings, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if not settings_row.integration or not settings_row.integration.enabled:
        return False
    if settings_row.last_sync_at is None:
        return True
    return settings_row.last_sync_at + timedelta(minutes=max(5, settings_row.sync_interval_minutes)) <= now


def get_live_activity(db: Session, *, max_age_seconds: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.utcnow()
    settings_row = get_tautulli_settings(db)
    ttl = max(5, min(60, max_age_seconds or settings_row.live_refresh_seconds or 10))
    with _live_lock:
        sampled = _live_cache.get("sampled_at")
        if sampled and (now - sampled).total_seconds() < ttl:
            return dict(_live_cache)
        try:
            client = _client(settings_row)
            raw_sessions = client.activity()
            activities = {a.tautulli_user_id: a for a in db.query(TautulliActivity).all()}
            sessions = []
            for session in raw_sessions:
                user_id = str(session.get("user_id") or "")
                activity = activities.get(user_id)
                customer = db.get(Customer, activity.customer_id) if activity else None
                if customer is None and user_id:
                    customer = db.query(Customer).filter(Customer.plex_user_id == user_id).first()
                if customer is None and session.get("username"):
                    identity = str(session.get("username")).strip().lower()
                    customer = db.query(Customer).filter(or_(func.lower(Customer.plex_username) == identity, func.lower(Customer.email) == identity)).first()
                item = {**session, "customer_id": customer.id if customer else None, "customer_name": customer.name if customer else session.get("username")}
                sessions.append(item)
                if customer and customer.status == "suspended" and not customer.exempt:
                    key = session.get("session_key") or f"{customer.id}:{session.get('title')}"
                    notify_event(
                        db, event="tautulli.suspended_streaming", title="Suspended customer is streaming",
                        message=f"{customer.name} appears to be streaming {session.get('title') or 'Plex content'} despite being suspended.",
                        severity="critical", target_type="customer", target_id=str(customer.id),
                        event_key=f"tautulli-suspended-stream:{key}", data={"customer": customer.name, "title": session.get("title")},
                    )
            _live_cache.update({"sampled_at": now, "sessions": sessions, "error": None})
        except Exception as exc:
            # Preserve last known sessions on a transient failure and mark the sample stale.
            _live_cache["error"] = f"{type(exc).__name__}: live activity unavailable"
        return dict(_live_cache)


def dashboard_usage(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.utcnow()
    rows = db.query(TautulliActivity).all()
    active_7d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at >= now - timedelta(days=7))
    active_30d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at >= now - timedelta(days=30))
    inactive_90d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at < now - timedelta(days=90))
    never = sum(1 for a in rows if a.last_streamed_at is None)
    total_30d = sum(int(a.watch_time_30d or 0) for a in rows)
    average_30d = int(total_30d / active_30d) if active_30d else 0
    return {"active_7d": active_7d, "active_30d": active_30d, "inactive_90d": inactive_90d, "never": never, "watch_time_30d": total_30d, "average_30d": average_30d}
