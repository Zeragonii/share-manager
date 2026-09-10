from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..integrations.tautulli import TautulliIntegration, TautulliUser
from ..models import ASSIGNED_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer, Integration, StreamLimitEvent, Subscription, TautulliActivity, TautulliSettings
from .notifications import notify_event


@dataclass
class TautulliSyncResult:
    matched: int
    unmatched: int
    synced: int


_live_lock = Lock()
_live_cache: dict[str, Any] = {"sampled_at": None, "sessions": [], "error": None}
_stream_observations: dict[int, dict[str, Any]] = {}
_recent_terminations: dict[str, datetime] = {}


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
        by_id = db.query(Customer).filter(Customer.plex_user_id == str(user.user_id), Customer.archived.is_(False)).first()
        if by_id:
            return by_id
    identities = {_norm(user.username), _norm(user.email), _norm(user.friendly_name)} - {""}
    if not identities:
        return None
    customers = db.query(Customer).filter(Customer.plex_username.is_not(None), Customer.archived.is_(False)).all()
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
        all_users = client.users(include_admin=True)
        settings_row.admin_user_ids = ",".join(sorted(user.user_id for user in all_users if user.is_admin)) or None
        users = [user for user in all_users if not user.is_admin]
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
                    customer = db.query(Customer).filter(Customer.plex_user_id == user_id, Customer.archived.is_(False)).first()
                if customer is None and session.get("username"):
                    identity = str(session.get("username")).strip().lower()
                    customer = db.query(Customer).filter(Customer.archived.is_(False), or_(func.lower(Customer.plex_username) == identity, func.lower(Customer.email) == identity)).first()
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



def remove_live_session_from_cache(session_key: str) -> None:
    """Remove a session from the shared live cache after an explicit termination.

    This is only a UI freshness helper; the next Tautulli sample remains authoritative.
    """
    key = str(session_key or "").strip()
    if not key:
        return
    with _live_lock:
        _live_cache["sessions"] = [
            item for item in _live_cache.get("sessions", [])
            if str(item.get("session_key") or "") != key
        ]


def customer_live_sessions(db: Session, customer: Customer, *, max_age_seconds: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Return only live sessions that Share Manager has matched to this customer."""
    live = get_live_activity(db, max_age_seconds=max_age_seconds, now=now)
    sessions = [item for item in live.get("sessions", []) if item.get("customer_id") == customer.id]
    return {**live, "sessions": sessions}


def terminate_customer_session(db: Session, customer: Customer, session_key: str) -> dict[str, Any]:
    """Terminate one live Tautulli session only after fresh ownership verification.

    The caller supplies only a session key. Ownership is resolved server-side from the
    authenticated customer and the current Tautulli activity response.
    """
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("A session key is required")

    settings_row = get_tautulli_settings(db)
    client = _client(settings_row)
    activity = db.query(TautulliActivity).filter(TautulliActivity.customer_id == customer.id).first()
    allowed_user_ids = {
        str(value).strip() for value in (
            activity.tautulli_user_id if activity else None,
            customer.plex_user_id,
        ) if value is not None and str(value).strip()
    }
    if not allowed_user_ids:
        raise PermissionError("This portal account is not matched to a Tautulli user")

    owned = None
    for session in client.activity():
        if str(session.get("session_key") or "") != key:
            continue
        if str(session.get("user_id") or "") in allowed_user_ids:
            owned = session
        break
    if owned is None:
        raise PermissionError("That stream is no longer active or does not belong to this account")

    title = str(owned.get("title") or "Plex stream")
    client.terminate_session(key, "This stream was stopped from your Share Manager customer portal.")
    remove_live_session_from_cache(key)
    return owned

def dashboard_usage(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.utcnow()
    rows = (db.query(TautulliActivity).join(Customer, Customer.id == TautulliActivity.customer_id).filter(Customer.archived.is_(False)).all())
    active_7d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at >= now - timedelta(days=7))
    active_30d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at >= now - timedelta(days=30))
    inactive_90d = sum(1 for a in rows if a.last_streamed_at and a.last_streamed_at < now - timedelta(days=90))
    never = sum(1 for a in rows if a.last_streamed_at is None)
    total_30d = sum(int(a.watch_time_30d or 0) for a in rows)
    average_30d = int(total_30d / active_30d) if active_30d else 0
    return {"active_7d": active_7d, "active_30d": active_30d, "inactive_90d": inactive_90d, "never": never, "watch_time_30d": total_30d, "average_30d": average_30d}


def enforce_stream_limits(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Enforce per-tier concurrent stream limits from the shared Tautulli live sample.

    A customer must be over limit in two distinct Tautulli samples before an excess
    session is terminated. 0 means unlimited. Exempt customers are intentionally
    included because billing exemption is separate from fair-use policy.
    """
    now = now or datetime.utcnow()
    settings_row = get_tautulli_settings(db)
    if not settings_row.integration or not settings_row.integration.enabled:
        _stream_observations.clear()
        return {"checked": 0, "enforced": 0, "failed": 0}

    live = get_live_activity(db, max_age_seconds=settings_row.live_refresh_seconds, now=now)
    if live.get("error"):
        return {"checked": 0, "enforced": 0, "failed": 0}
    sampled_at = live.get("sampled_at")
    if not sampled_at:
        return {"checked": 0, "enforced": 0, "failed": 0}

    admin_user_ids = {item for item in (settings_row.admin_user_ids or "").split(",") if item}
    by_customer: dict[int, list[dict[str, Any]]] = {}
    for session in live.get("sessions", []):
        if str(session.get("user_id") or "") in admin_user_ids:
            continue
        customer_id = session.get("customer_id")
        if customer_id:
            by_customer.setdefault(int(customer_id), []).append(session)

    checked = enforced = failed = 0
    active_customer_ids = set(by_customer)
    for customer_id in list(_stream_observations):
        if customer_id not in active_customer_ids:
            _stream_observations.pop(customer_id, None)

    # Expire termination cooldowns after two minutes.
    for key, when in list(_recent_terminations.items()):
        if when < now - timedelta(minutes=2):
            _recent_terminations.pop(key, None)

    client = _client(settings_row)
    for customer_id, sessions in by_customer.items():
        customer = db.get(Customer, customer_id)
        if not customer or customer.archived:
            continue
        sub = (
            db.query(Subscription)
            .filter(Subscription.customer_id == customer_id, Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
            .order_by(Subscription.started_at.desc(), Subscription.id.desc())
            .first()
        )
        if not sub:
            _stream_observations.pop(customer_id, None)
            continue
        tier = db.get(BillingTier, sub.billing_tier_id)
        limit = max(0, int(tier.stream_limit or 0)) if tier else 0
        if limit == 0:
            _stream_observations.pop(customer_id, None)
            continue
        checked += 1
        if len(sessions) <= limit:
            _stream_observations.pop(customer_id, None)
            continue

        def started_value(item: dict[str, Any]):
            value = item.get("started_at")
            return value if isinstance(value, datetime) else datetime.min

        ordered = sorted(sessions, key=started_value)
        excess = ordered[limit:]  # newest sessions beyond the allowance
        signature = tuple(sorted(str(x.get("session_key") or "") for x in excess))
        obs = _stream_observations.get(customer_id)
        if obs and obs.get("sampled_at") == sampled_at:
            continue  # same cached sample cannot count as a second strike
        if obs and obs.get("signature") == signature:
            strikes = int(obs.get("strikes", 1)) + 1
        else:
            strikes = 1
        _stream_observations[customer_id] = {"signature": signature, "strikes": strikes, "sampled_at": sampled_at}
        if strikes < 2:
            continue

        # Terminate newest excess first. When multiple sessions exceed the limit,
        # enforce each excess session in the same confirmed observation.
        for session in reversed(excess):
            session_key = str(session.get("session_key") or "").strip()
            if not session_key or session_key in _recent_terminations:
                continue
            title = str(session.get("title") or "Unknown title")
            player = str(session.get("player") or "") or None
            ip_address = str(session.get("ip_address") or "") or None
            success = False
            detail = None
            try:
                client.terminate_session(session_key, f"Concurrent stream limit reached. Your account allows {limit} concurrent stream{'s' if limit != 1 else ''}.")
                success = True
                enforced += 1
                _recent_terminations[session_key] = now
                detail = "Newest excess session terminated"
            except Exception as exc:
                failed += 1
                detail = f"{type(exc).__name__}: {exc}"[:1000]

            event = StreamLimitEvent(
                customer_id=customer.id, billing_tier_id=tier.id if tier else None,
                created_at=now, allowed_streams=limit, detected_streams=len(sessions),
                session_key=session_key, title=title, player=player, ip_address=ip_address,
                success=success, detail=detail,
            )
            db.add(event)
            db.add(AuditLog(actor="system", action="stream.limit_enforced" if success else "stream.limit_enforcement_failed", target_type="customer", target_id=str(customer.id), detail=f"{len(sessions)} detected / {limit} allowed · {title} · {detail}"))
            db.commit()
            notify_event(
                db,
                event="stream.limit_enforced" if success else "stream.limit_enforcement_failed",
                title="Concurrent stream limit enforced" if success else "Concurrent stream limit enforcement failed",
                message=(f"{customer.name} had {len(sessions)} concurrent streams with a limit of {limit}. " + (f"Terminated {title}." if success else f"Could not terminate {title}.")),
                severity="warning" if success else "critical",
                target_type="customer", target_id=str(customer.id),
                event_key=f"stream-limit:{'ok' if success else 'failed'}:{customer.id}:{session_key}:{int(now.timestamp())}",
                data={"customer": customer.name, "allowed": limit, "detected": len(sessions), "title": title, "player": player, "success": success},
            )
        _stream_observations.pop(customer_id, None)

    return {"checked": checked, "enforced": enforced, "failed": failed}
