import asyncio
import logging
import os
import subprocess
import tempfile
import re
import secrets
import string
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from urllib.parse import quote_plus, urlsplit, urlencode

from fastapi import Depends, FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.engine import make_url

from .config import settings
from .db import get_db, SessionLocal, engine
from .models import (
    ACCESS_SUBSCRIPTION_STATES,
    ASSIGNED_SUBSCRIPTION_STATES,
    AuditLog,
    BillingTier,
    BackupSettings,
    Customer,
    Integration,
    Package,
    PackageEntitlement,
    Payment,
    PaymentSource,
    Subscription,
    SubscriptionCredit,
    NotificationEndpoint,
    NotificationDelivery,
    NotificationEvent,
    PushSubscription,
    CustomerNotificationPreference,
    AdminNotificationPreference,
    ScheduledCustomerBroadcast,
    RequestsPlatformSettings,
    NewsBanner,
    TautulliActivity,
    TautulliSettings,
    TautulliWatchHistory,
    StreamLimitEvent,
    SupportTicket,
    SupportTicketMessage,
)
from .integrations.plex import PlexIntegration
from .integrations.tautulli import TautulliIntegration, TautulliError
from .security import (
    logged_in, make_session, valid_credentials,
    hash_portal_password, verify_portal_password, make_portal_session, read_portal_session,
)
from .services.billing import apply_payment, apply_subscription_credit, desired_billing_status, initialize_subscription_period, process_billing
from .services.reconcile import enqueue_reconciliation, reconcile_customer, retry_pending_reconciliations
from .services.payment_maintenance import payment_is_latest_coverage_event, recalculate_after_latest_payment_change, rollback_voided_latest_payment
from .services.notifications import (
    EVENT_DEFINITIONS, CUSTOMER_PUSH_EVENTS, format_due_reminder_days, notify_due_reminders, notify_event, send_test,
    ensure_platform_settings, save_push_subscription, disable_push_subscription, customer_push_status,
    update_customer_preferences, admin_push_status, update_admin_preferences, send_portal_test, send_admin_push_test,
    critical_broadcast_audience, send_critical_customer_broadcast,
    retry_failed_deliveries, schedule_critical_customer_broadcast, cancel_scheduled_broadcast, process_scheduled_broadcasts,
)
from .services.backups import create_backup, list_backups, apply_retention, scheduled_backup_due, safe_backup_path, validate_backup, restore_backup, get_backup_policy, validate_application_schema, BackupStorageError
from .services.tautulli import (
    get_tautulli_settings, sync_tautulli, sync_due, get_live_activity, dashboard_usage,
    enforce_stream_limits, customer_live_sessions, terminate_customer_session,
    backfill_watch_history_page, watch_history_backfill_status, force_full_watch_history_resync,
)
from .services.tickets import (
    TICKET_CATEGORIES, TICKET_STATUSES, TICKET_PRIORITIES,
    create_ticket, add_customer_reply, add_admin_reply, add_internal_note, change_status, change_priority,
)
from .version import APP_VERSION



logger = logging.getLogger("share-manager")

PORTAL_LOGIN_ATTEMPTS: dict[str, list[datetime]] = {}
PORTAL_LOGIN_LIMIT = 5
PORTAL_LOGIN_WINDOW = timedelta(minutes=15)
PORTAL_ALPHABET = string.ascii_letters + string.digits


def generate_portal_password(length: int = 12) -> str:
    return "".join(secrets.choice(PORTAL_ALPHABET) for _ in range(length))


def _portal_username_base(customer: Customer) -> str:
    raw = (customer.plex_username or customer.email or customer.name or f"customer{customer.id}").strip().lower()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    base = re.sub(r"[^a-z0-9._-]+", "", raw)
    return base[:100] or f"customer{customer.id}"


def _portal_username_suggestion(db: Session, customer: Customer) -> str:
    base = _portal_username_base(customer)
    candidate = base
    suffix = 2
    while db.query(Customer).filter(Customer.id != customer.id, func.lower(Customer.portal_username) == candidate.lower()).first():
        candidate = f"{base[:95]}{suffix}"
        suffix += 1
    return candidate


def _disable_customer_portal(customer: Customer, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    if customer.portal_enabled or customer.portal_password_hash:
        customer.portal_enabled = False
        customer.portal_password_hash = None
        customer.portal_session_version = int(customer.portal_session_version or 1) + 1
        customer.portal_disabled_at = now


def _portal_customer(request: Request, db: Session) -> Customer | None:
    payload = read_portal_session(request)
    if not payload:
        return None
    try:
        customer_id = int(payload.get("customer_id"))
        version = int(payload.get("version"))
    except (TypeError, ValueError):
        return None
    customer = db.get(Customer, customer_id)
    if not customer:
        return None
    if (not customer.portal_enabled or customer.archived or customer.status == "cancelled" or
            version != int(customer.portal_session_version or 1)):
        return None
    return customer


RESTORE_IN_PROGRESS = Event()
DB_WORK_LOCK = Lock()

def serialized_db_worker(func):
    def wrapped(*args, **kwargs):
        with DB_WORK_LOCK:
            return func(*args, **kwargs)
    return wrapped


def _parse_date(value: str | None, fallback: datetime | None = None) -> datetime | None:
    if not value:
        return fallback
    return datetime.strptime(value, "%Y-%m-%d")


def _format_duration(seconds: int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _relative_time(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "Never"
    now = now or datetime.utcnow()
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _notify_reconcile_failure(db: Session, customer: Customer, exc: Exception) -> None:
    db.add(AuditLog(action="plex.reconcile.error", target_type="customer", target_id=str(customer.id), detail=str(exc)))
    db.commit()
    notify_event(
        db,
        event="plex.reconcile_failed",
        title="Plex reconciliation failed",
        message=f"{customer.name}: {exc}",
        target_type="customer",
        target_id=str(customer.id),
        data={"customer": customer.name},
    )


def _notify_due_soon(db: Session, now: datetime) -> None:
    notify_due_reminders(db, now=now, fallback_days=settings.notification_due_soon_days)




@serialized_db_worker
def run_backup_cycle() -> bool:
    """Create the daily scheduled backup when due and apply the database-backed retention policy."""
    if RESTORE_IN_PROGRESS.is_set():
        return False
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        policy = get_backup_policy(db, settings)
        if not policy.enabled:
            return False
        try:
            if not scheduled_backup_due(settings.backup_dir, now=now, hour=policy.schedule_hour):
                return False
            backup = create_backup(settings.database_url, settings.backup_dir, automatic=True, now=now)
            removed = apply_retention(
                settings.backup_dir,
                daily=policy.retention_daily,
                weekly=policy.retention_weekly,
                monthly=policy.retention_monthly,
            )
            db.add(AuditLog(actor="system", action="backup.scheduled", target_type="backup", target_id=backup.name, detail=f"{backup.size} bytes; pruned {len(removed)} old automatic backup(s)"))
            db.commit()
            notify_event(db, event="backup.created", title="Scheduled database backup created", message=f"{backup.name} was created successfully.", target_type="backup", target_id=backup.name, data={"filename": backup.name, "size": backup.size, "automatic": True})
            return True
        except Exception as exc:
            db.add(AuditLog(actor="system", action="backup.failed", target_type="backup", detail=type(exc).__name__))
            db.commit()
            notify_event(db, event="backup.failed", title="Scheduled backup failed", message="Share Manager could not create its scheduled database backup.", severity="critical", target_type="backup", data={"error_type": type(exc).__name__})
            return False
    finally:
        db.close()

@serialized_db_worker
def run_billing_cycle() -> int:
    """Run automatic expiry/grace transitions, notifications and Plex reconciliation."""
    if RESTORE_IN_PROGRESS.is_set():
        return 0
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        changed = process_billing(db, now=now)
        for customer in changed:
            if customer.status == "grace":
                notify_event(db, event="customer.entered_grace", title="Customer entered grace", message=f"{customer.name} has entered their billing grace period.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})
            elif customer.status == "suspended":
                notify_event(db, event="customer.suspended", title="Customer suspended", message=f"{customer.name} has been suspended after their billing grace period expired.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})
            elif customer.status == "active":
                notify_event(db, event="customer.reactivated", title="Customer reactivated", message=f"{customer.name} is active again.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})

            if not customer.plex_username or customer.exempt:
                continue
            try:
                reconcile_customer(db, customer)
            except Exception as exc:
                _notify_reconcile_failure(db, customer, exc)
        retry_pending_reconciliations(db, now=now, on_error=_notify_reconcile_failure)
        _notify_due_soon(db, now)
        return len(changed)
    finally:
        db.close()


@serialized_db_worker
def run_reconcile_queue_cycle() -> int:
    """Process due durable Plex reconciliation jobs independently of billing."""
    if RESTORE_IN_PROGRESS.is_set():
        return 0
    db = SessionLocal()
    try:
        return retry_pending_reconciliations(
            db,
            now=datetime.utcnow(),
            on_error=_notify_reconcile_failure,
        )
    finally:
        db.close()


@serialized_db_worker
def run_tautulli_cycle() -> bool:
    if RESTORE_IN_PROGRESS.is_set():
        return False
    db = SessionLocal()
    try:
        row = get_tautulli_settings(db)
        if not sync_due(row):
            return False
        sync_tautulli(db)
        return True
    except Exception:
        return False
    finally:
        db.close()


@serialized_db_worker
def run_tautulli_backfill_cycle() -> bool:
    if RESTORE_IN_PROGRESS.is_set():
        return False
    db = SessionLocal()
    try:
        row = get_tautulli_settings(db)
        if not row.integration or not row.integration.enabled:
            return False
        result = backfill_watch_history_page(db, page_size=500)
        return result is not None
    except Exception:
        logger.exception("Tautulli watch-history backfill cycle failed")
        return False
    finally:
        db.close()


@serialized_db_worker
def run_stream_limit_cycle() -> bool:
    if RESTORE_IN_PROGRESS.is_set():
        return False
    db = SessionLocal()
    try:
        result = enforce_stream_limits(db)
        return bool(result.get("enforced") or result.get("failed"))
    except Exception:
        logger.exception("Stream-limit enforcement cycle failed")
        return False
    finally:
        db.close()


@serialized_db_worker
def run_notification_cycle() -> tuple[int, int]:
    """Process due scheduled broadcasts and retry transient notification failures."""
    if RESTORE_IN_PROGRESS.is_set():
        return (0, 0)
    db = SessionLocal()
    try:
        scheduled = process_scheduled_broadcasts(db, now=datetime.utcnow())
        retried = retry_failed_deliveries(db, now=datetime.utcnow())
        return scheduled, retried
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async def billing_loop():
        # A short initial delay lets the container finish coming up cleanly.
        await asyncio.sleep(5)
        while True:
            try:
                await asyncio.to_thread(run_billing_cycle)
            except Exception:
                # The next scheduled cycle gets another chance; request handling stays up.
                logger.exception("Billing worker cycle failed")
            await asyncio.sleep(max(1, settings.billing_check_interval_minutes) * 60)

    async def backup_loop():
        await asyncio.sleep(10)
        interval = max(1, settings.backup_check_interval_minutes)
        while True:
            try:
                await asyncio.to_thread(run_backup_cycle)
                # Read the interval from the database after every cycle so UI changes
                # take effect without a container restart. Keep this inside the same
                # failure boundary so a transient DB issue cannot kill the worker.
                db = SessionLocal()
                try:
                    interval = get_backup_policy(db, settings).check_interval_minutes
                finally:
                    db.close()
            except Exception:
                logger.exception("Backup worker cycle failed")
            await asyncio.sleep(max(1, interval) * 60)

    async def reconcile_queue_loop():
        # Bulk/operator changes are queued instead of blocking HTTP requests on Plex.
        await asyncio.sleep(3)
        while True:
            try:
                await asyncio.to_thread(run_reconcile_queue_cycle)
            except Exception:
                logger.exception("Plex reconciliation queue worker cycle failed")
            await asyncio.sleep(5)

    async def tautulli_loop():
        await asyncio.sleep(15)
        while True:
            try:
                await asyncio.to_thread(run_tautulli_cycle)
            except Exception:
                logger.exception("Tautulli worker cycle failed")
            await asyncio.sleep(60)

    async def tautulli_backfill_loop():
        # Full history is imported separately from the normal analytics sync so
        # Sync Now and page loads remain quick even for very large Tautulli histories.
        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.to_thread(run_tautulli_backfill_cycle)
            except Exception:
                logger.exception("Tautulli watch-history backfill worker cycle failed")
            await asyncio.sleep(5)

    async def notification_loop():
        await asyncio.sleep(12)
        while True:
            try:
                await asyncio.to_thread(run_notification_cycle)
            except Exception:
                logger.exception("Notification scheduler/retry worker cycle failed")
            await asyncio.sleep(30)

    async def stream_limit_loop():
        await asyncio.sleep(8)
        while True:
            interval = 10
            try:
                await asyncio.to_thread(run_stream_limit_cycle)
                db = SessionLocal()
                try:
                    interval = get_tautulli_settings(db).live_refresh_seconds
                finally:
                    db.close()
            except Exception:
                logger.exception("Stream-limit enforcement worker cycle failed")
            await asyncio.sleep(max(10, int(interval or 10)))

    billing_task = asyncio.create_task(billing_loop())
    backup_task = asyncio.create_task(backup_loop())
    reconcile_task = asyncio.create_task(reconcile_queue_loop())
    tautulli_task = asyncio.create_task(tautulli_loop())
    tautulli_backfill_task = asyncio.create_task(tautulli_backfill_loop())
    stream_limit_task = asyncio.create_task(stream_limit_loop())
    notification_task = asyncio.create_task(notification_loop())
    try:
        yield
    finally:
        billing_task.cancel()
        backup_task.cancel()
        reconcile_task.cancel()
        tautulli_task.cancel()
        tautulli_backfill_task.cancel()
        stream_limit_task.cancel()
        notification_task.cancel()
        with suppress(asyncio.CancelledError):
            await billing_task
        with suppress(asyncio.CancelledError):
            await backup_task
        with suppress(asyncio.CancelledError):
            await reconcile_task
        with suppress(asyncio.CancelledError):
            await tautulli_task
        with suppress(asyncio.CancelledError):
            await tautulli_backfill_task
        with suppress(asyncio.CancelledError):
            await stream_limit_task


app = FastAPI(title="Share Manager", version=APP_VERSION, lifespan=lifespan)

@app.middleware("http")
async def restore_maintenance_mode(request: Request, call_next):
    if RESTORE_IN_PROGRESS.is_set() and request.url.path not in {"/health"}:
        return JSONResponse({"error": "Database restore in progress"}, status_code=503, headers={"Retry-After": "10"})
    return await call_next(request)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["format_duration"] = _format_duration
templates.env.globals["relative_time"] = _relative_time


def auth(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


def render(request: Request, name: str, **ctx):
    # Keep the admin navigation ticket badge available on every admin page.
    # Use a short-lived session here rather than making every route remember to
    # calculate the same count. If the database is temporarily unavailable, the
    # page can still render without the badge.
    admin_open_ticket_count = 0
    requests_platform = None
    active_news_banner = None
    nav_db = SessionLocal()
    try:
        requests_platform = nav_db.get(RequestsPlatformSettings, 1)
        if request.url.path.startswith("/portal"):
            now_utc = datetime.utcnow()
            active_news_banner = (
                nav_db.query(NewsBanner)
                .filter(
                    NewsBanner.cancelled_at.is_(None),
                    NewsBanner.starts_at <= now_utc,
                    NewsBanner.ends_at > now_utc,
                )
                .order_by(NewsBanner.starts_at.desc())
                .first()
            )
        if logged_in(request):
            admin_open_ticket_count = (
                nav_db.query(func.count(SupportTicket.id))
                .filter(SupportTicket.status != "closed")
                .scalar()
                or 0
            )
    except Exception:
        logger.debug("Could not calculate shared navigation context", exc_info=True)
    finally:
        nav_db.close()
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request,
            "app_version": APP_VERSION,
            "admin_open_ticket_count": admin_open_ticket_count,
            "requests_platform": requests_platform,
            "active_news_banner": active_news_banner,
            **ctx,
        },
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    return FileResponse("app/static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        "app/static/service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Service-Worker-Allowed": "/"},
    )


@app.get("/portal/manifest.webmanifest", include_in_schema=False)
def portal_pwa_manifest():
    return FileResponse("app/static/portal-manifest.webmanifest", media_type="application/manifest+json")


@app.get("/portal/service-worker.js", include_in_schema=False)
def portal_service_worker():
    return FileResponse(
        "app/static/portal-service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Service-Worker-Allowed": "/portal"},
    )


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not valid_credentials(username, password):
        return render(request, "login.html", error="Invalid username or password")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("sm_session", make_session(), httponly=True, samesite="lax", secure=settings.session_cookie_secure, max_age=settings.session_max_age_seconds)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("sm_session")
    return response


@app.get("/portal/login", response_class=HTMLResponse)
def portal_login_page(request: Request, db: Session = Depends(get_db)):
    if _portal_customer(request, db):
        return RedirectResponse("/portal", status_code=303)
    return render(request, "portal_login.html", error=None)


@app.post("/portal/login", response_class=HTMLResponse)
def portal_login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    clean_username = username.strip().lower()
    host = request.client.host if request.client else "unknown"
    key = f"{host}:{clean_username}"
    now = datetime.utcnow()
    recent = [ts for ts in PORTAL_LOGIN_ATTEMPTS.get(key, []) if now - ts < PORTAL_LOGIN_WINDOW]
    if len(recent) >= PORTAL_LOGIN_LIMIT:
        PORTAL_LOGIN_ATTEMPTS[key] = recent
        return render(request, "portal_login.html", error="Too many sign-in attempts. Try again in a few minutes." )
    customer = db.query(Customer).filter(func.lower(Customer.portal_username) == clean_username).first() if clean_username else None
    valid = bool(customer and customer.portal_enabled and not customer.archived and customer.status != "cancelled" and verify_portal_password(password, customer.portal_password_hash))
    if not valid:
        recent.append(now)
        PORTAL_LOGIN_ATTEMPTS[key] = recent
        return render(request, "portal_login.html", error="Invalid username or password")
    PORTAL_LOGIN_ATTEMPTS.pop(key, None)
    customer.portal_last_login_at = now
    db.commit()
    response = RedirectResponse("/portal", status_code=303)
    response.set_cookie("sm_portal_session", make_portal_session(customer.id, int(customer.portal_session_version or 1)), httponly=True, samesite="lax", secure=settings.session_cookie_secure, max_age=settings.session_max_age_seconds, path="/portal")
    return response


@app.post("/portal/logout")
def portal_logout():
    response = RedirectResponse("/portal/login", status_code=303)
    response.delete_cookie("sm_portal_session", path="/portal")
    return response


@app.get("/portal", response_class=HTMLResponse)
def portal_dashboard(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        response = RedirectResponse("/portal/login", status_code=303)
        response.delete_cookie("sm_portal_session", path="/portal")
        return response
    sub = db.query(Subscription).options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).filter(
        Subscription.customer_id == customer.id,
        Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
    ).order_by(Subscription.id.desc()).first()
    if not sub:
        sub = db.query(Subscription).options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).filter(Subscription.customer_id == customer.id).order_by(Subscription.id.desc()).first()
    effective_status = "exempt" if customer.exempt else customer.status
    push_status = customer_push_status(db, customer.id)
    return render(
        request, "portal_dashboard.html", customer=customer, subscription=sub, effective_status=effective_status, push_status=push_status,
        notice=request.query_params.get("notice"), error=request.query_params.get("error"), now=datetime.utcnow(),
    )


@app.post("/portal/password")
def portal_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    customer = _portal_customer(request, db)
    if not customer:
        response = RedirectResponse("/portal/login", status_code=303)
        response.delete_cookie("sm_portal_session", path="/portal")
        return response

    if not verify_portal_password(current_password, customer.portal_password_hash):
        return RedirectResponse("/portal?error=" + quote_plus("Current password is incorrect."), status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/portal?error=" + quote_plus("New passwords do not match."), status_code=303)
    if len(new_password) < 12:
        return RedirectResponse("/portal?error=" + quote_plus("New password must be at least 12 characters."), status_code=303)
    if len(new_password.encode("utf-8")) > 72:
        return RedirectResponse("/portal?error=" + quote_plus("New password is too long; use no more than 72 UTF-8 bytes."), status_code=303)

    customer.portal_password_hash = hash_portal_password(new_password)
    customer.portal_session_version = int(customer.portal_session_version or 1) + 1
    db.add(AuditLog(
        actor=f"portal:{customer.id}", action="portal.password.change", target_type="customer",
        target_id=str(customer.id), detail="Customer changed own portal password; other portal sessions revoked",
    ))
    db.commit()

    response = RedirectResponse("/portal?notice=" + quote_plus("Password updated. Other portal sessions have been signed out."), status_code=303)
    response.set_cookie(
        "sm_portal_session", make_portal_session(customer.id, int(customer.portal_session_version or 1)),
        httponly=True, samesite="lax", secure=settings.session_cookie_secure,
        max_age=settings.session_max_age_seconds, path="/portal",
    )
    return response


@app.get("/portal/history", response_class=HTMLResponse)
def portal_history(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        response = RedirectResponse("/portal/login", status_code=303)
        response.delete_cookie("sm_portal_session", path="/portal")
        return response

    payments = (
        db.query(Payment)
        .filter(Payment.customer_id == customer.id)
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
        .all()
    )
    credits = (
        db.query(SubscriptionCredit)
        .filter(SubscriptionCredit.customer_id == customer.id)
        .order_by(SubscriptionCredit.granted_at.desc(), SubscriptionCredit.id.desc())
        .all()
    )
    subscriptions = (
        db.query(Subscription)
        .options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package))
        .filter(Subscription.customer_id == customer.id)
        .order_by(Subscription.started_at.desc(), Subscription.id.desc())
        .all()
    )

    active_payments = [payment for payment in payments if not payment.voided_at]
    total_paid = sum((Decimal(payment.amount) for payment in active_payments), Decimal("0.00"))
    complimentary_periods = sum(int(credit.billing_periods or 0) for credit in credits)

    return render(
        request,
        "portal_history.html",
        customer=customer,
        payments=payments,
        credits=credits,
        subscriptions=subscriptions,
        payment_count=len(active_payments),
        total_paid=total_paid,
        complimentary_periods=complimentary_periods,
    )


@app.get("/portal/api/push/public-key")
def portal_push_public_key(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"public_key": ensure_platform_settings(db).vapid_public_key}

@app.post("/portal/api/push/subscribe")
async def portal_push_subscribe(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json(); keys = body.get("keys") or {}
        endpoint = str(body.get("endpoint") or "").strip(); p256dh = str(keys.get("p256dh") or "").strip(); auth_key = str(keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth_key:
            raise ValueError("Incomplete browser push subscription")
        save_push_subscription(db, owner_type="customer", customer_id=customer.id, endpoint=endpoint, p256dh=p256dh, auth_key=auth_key, user_agent=request.headers.get("user-agent"))
        db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="notification.push.subscribe", target_type="customer", target_id=str(customer.id), detail="Customer enabled Web Push on a device")); db.commit()
        return {"ok": True, "devices": customer_push_status(db, customer.id)["devices"]}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

@app.post("/portal/api/push/unsubscribe")
async def portal_push_unsubscribe(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json(); endpoint = str(body.get("endpoint") or "").strip()
    if endpoint:
        disable_push_subscription(db, endpoint=endpoint, owner_type="customer", customer_id=customer.id)
    db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="notification.push.unsubscribe", target_type="customer", target_id=str(customer.id), detail="Customer disabled Web Push on a device")); db.commit()
    return {"ok": True, "devices": customer_push_status(db, customer.id)["devices"]}

@app.post("/portal/notifications/preferences")
def portal_notification_preferences(request: Request, push_enabled: str | None = Form(None), ticket_notifications_default: str | None = Form(None), events: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    update_customer_preferences(db, customer.id, enabled=bool(push_enabled), events=events, ticket_notifications_default=bool(ticket_notifications_default))
    db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="notification.preferences.updated", target_type="customer", target_id=str(customer.id), detail=f"push={'enabled' if push_enabled else 'disabled'}; events={','.join(events)}")); db.commit()
    return RedirectResponse("/portal?notice=Notification+preferences+saved", status_code=303)

@app.post("/portal/api/push/test")
def portal_push_test(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    deliveries = send_portal_test(db, customer)
    success = sum(1 for d in deliveries if d.channel == "web_push" and d.success)
    return {"ok": success > 0, "delivered": success}

@app.get("/api/notifications/push/public-key")
def admin_push_public_key(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"public_key": ensure_platform_settings(db).vapid_public_key}

@app.post("/api/notifications/push/subscribe")
async def admin_push_subscribe(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json(); keys = body.get("keys") or {}
        endpoint = str(body.get("endpoint") or "").strip(); p256dh = str(keys.get("p256dh") or "").strip(); auth_key = str(keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth_key:
            raise ValueError("Incomplete browser push subscription")
        save_push_subscription(db, owner_type="admin", customer_id=None, endpoint=endpoint, p256dh=p256dh, auth_key=auth_key, user_agent=request.headers.get("user-agent"))
        db.add(AuditLog(actor=settings.admin_username, action="notification.push.subscribe", target_type="admin", detail="Admin enabled Web Push on a device")); db.commit()
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

@app.post("/api/notifications/push/unsubscribe")
async def admin_push_unsubscribe(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json(); endpoint = str(body.get("endpoint") or "").strip()
    if endpoint:
        disable_push_subscription(db, endpoint=endpoint, owner_type="admin")
    return {"ok": True}

@app.post("/api/notifications/push/test")
def admin_push_test(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    deliveries = send_admin_push_test(db)
    success = sum(1 for d in deliveries if d.channel == "web_push" and d.recipient_type == "admin" and d.success)
    return {"ok": success > 0, "delivered": success}

@app.post("/notifications/admin/preferences")
def admin_notification_preferences(request: Request, push_enabled: str | None = Form(None), events: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    update_admin_preferences(db, enabled=bool(push_enabled), events=events)
    db.add(AuditLog(actor=settings.admin_username, action="notification.admin_preferences.updated", target_type="admin", detail=f"push={'enabled' if push_enabled else 'disabled'}; events={','.join(events)}"))
    db.commit()
    return RedirectResponse("/integrations?notice=Admin+push+preferences+updated#notifications", status_code=303)


def _ticket_message_json(msg: SupportTicketMessage, *, customer_view: bool = False, customer_name: str | None = None) -> dict:
    if customer_view:
        author = "You" if msg.author_type == "customer" else "Support"
    elif msg.author_type == "customer":
        author = customer_name or msg.author_label or "Customer"
    elif msg.author_type == "internal":
        author = f"Internal note · {msg.author_label}"
    else:
        author = "Support"
    return {
        "id": msg.id,
        "author_type": msg.author_type,
        "author": author,
        "body": msg.body,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "created_label": msg.created_at.strftime("%d %b %Y · %H:%M") if msg.created_at else "",
        "internal": msg.author_type == "internal",
    }


def _ticket_state_json(ticket: SupportTicket) -> dict:
    return {
        "reference": ticket.reference,
        "status": ticket.status,
        "status_label": TICKET_STATUSES.get(ticket.status, ticket.status),
        "priority": ticket.priority,
        "priority_label": TICKET_PRIORITIES.get(ticket.priority, ticket.priority),
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "updated_label": _relative_time(ticket.updated_at),
    }


@app.get("/portal/tickets", response_class=HTMLResponse)
def portal_tickets(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    tickets = db.query(SupportTicket).filter(SupportTicket.customer_id == customer.id).order_by(SupportTicket.updated_at.desc()).all()
    active = [t for t in tickets if t.status != "closed"]
    closed = [t for t in tickets if t.status == "closed"]
    pref = db.get(CustomerNotificationPreference, customer.id)
    return render(request, "portal_tickets.html", customer=customer, active_tickets=active, closed_tickets=closed,
                  categories=TICKET_CATEGORIES, statuses=TICKET_STATUSES, priorities=TICKET_PRIORITIES,
                  ticket_notify_default=(pref.ticket_notifications_default if pref else True),
                  notice=request.query_params.get("notice"), error=request.query_params.get("error"))


@app.post("/portal/tickets/new")
def portal_ticket_create(request: Request, category: str = Form(...), subject: str = Form(...), description: str = Form(...), notifications_enabled: str | None = Form(None), db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    try:
        ticket = create_ticket(db, customer, category=category, subject=subject, description=description, notifications_enabled=bool(notifications_enabled))
        return RedirectResponse(f"/portal/tickets/{ticket.reference}?notice=" + quote_plus("Support ticket created"), status_code=303)
    except ValueError as exc:
        return RedirectResponse("/portal/tickets?error=" + quote_plus(str(exc)), status_code=303)


def _customer_ticket_or_none(db: Session, customer: Customer, reference: str) -> SupportTicket | None:
    return db.query(SupportTicket).options(joinedload(SupportTicket.messages)).filter(SupportTicket.reference == reference, SupportTicket.customer_id == customer.id).first()


@app.get("/portal/tickets/{reference}", response_class=HTMLResponse)
def portal_ticket_detail(reference: str, request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    ticket = _customer_ticket_or_none(db, customer, reference)
    if not ticket:
        return RedirectResponse("/portal/tickets?error=Ticket+not+found", status_code=303)
    if ticket.customer_unread:
        ticket.customer_unread = False; db.commit()
    messages = [m for m in ticket.messages if m.visible_to_customer]
    return render(request, "portal_ticket_detail.html", customer=customer, ticket=ticket, messages=messages,
                  categories=TICKET_CATEGORIES, statuses=TICKET_STATUSES, priorities=TICKET_PRIORITIES,
                  notice=request.query_params.get("notice"), error=request.query_params.get("error"))


@app.post("/portal/tickets/{reference}/reply")
def portal_ticket_reply(reference: str, request: Request, body: str = Form(...), db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    ticket = _customer_ticket_or_none(db, customer, reference)
    if not ticket:
        return RedirectResponse("/portal/tickets?error=Ticket+not+found", status_code=303)
    try:
        add_customer_reply(db, ticket, customer, body)
        return RedirectResponse(f"/portal/tickets/{reference}?notice=" + quote_plus("Reply sent"), status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/portal/tickets/{reference}?error=" + quote_plus(str(exc)), status_code=303)


@app.post("/portal/tickets/{reference}/notifications")
def portal_ticket_notifications(reference: str, request: Request, enabled: str | None = Form(None), db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    ticket = _customer_ticket_or_none(db, customer, reference)
    if not ticket:
        return RedirectResponse("/portal/tickets?error=Ticket+not+found", status_code=303)
    ticket.notifications_enabled = bool(enabled); ticket.updated_at = datetime.utcnow()
    db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="ticket.notifications", target_type="support_ticket", target_id=ticket.reference, detail="enabled" if enabled else "disabled")); db.commit()
    return RedirectResponse(f"/portal/tickets/{reference}?notice=" + quote_plus("Ticket notification preference updated"), status_code=303)


@app.get("/portal/api/tickets/{reference}/updates")
def portal_ticket_updates(reference: str, request: Request, after: int = 0, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ticket = db.query(SupportTicket).filter(SupportTicket.reference == reference, SupportTicket.customer_id == customer.id).first()
    if not ticket:
        return JSONResponse({"error": "Ticket not found"}, status_code=404)
    messages = (
        db.query(SupportTicketMessage)
        .filter(
            SupportTicketMessage.ticket_id == ticket.id,
            SupportTicketMessage.visible_to_customer.is_(True),
            SupportTicketMessage.id > max(0, after),
        )
        .order_by(SupportTicketMessage.id.asc())
        .all()
    )
    if ticket.customer_unread:
        ticket.customer_unread = False
        db.commit()
    return {
        "ticket": _ticket_state_json(ticket),
        "messages": [_ticket_message_json(m, customer_view=True) for m in messages],
        "latest_message_id": messages[-1].id if messages else after,
    }


@app.get("/portal/activity", response_class=HTMLResponse)
def portal_activity(
    request: Request,
    device: str | None = None,
    library: str | None = None,
    media_type: str | None = None,
    q: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: str | None = None,
    rows: str | None = None,
    db: Session = Depends(get_db),
):
    customer = _portal_customer(request, db)
    if not customer:
        response = RedirectResponse("/portal/login", status_code=303)
        response.delete_cookie("sm_portal_session", path="/portal")
        return response
    activity = db.query(TautulliActivity).filter(TautulliActivity.customer_id == customer.id).first()
    settings_row = get_tautulli_settings(db)
    live = customer_live_sessions(db, customer, max_age_seconds=settings_row.live_refresh_seconds)
    sub = db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(
        Subscription.customer_id == customer.id,
        Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
    ).order_by(Subscription.id.desc()).first()
    if not sub:
        sub = db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(Subscription.customer_id == customer.id).order_by(Subscription.id.desc()).first()
    events = db.query(StreamLimitEvent).filter(StreamLimitEvent.customer_id == customer.id).order_by(StreamLimitEvent.created_at.desc()).limit(25).all()

    watch_query = db.query(TautulliWatchHistory).filter(TautulliWatchHistory.customer_id == customer.id)
    clean_device = (device or "").strip()
    clean_library = (library or "").strip()
    clean_media_type = (media_type or "").strip()
    clean_q = (q or "").strip()
    if clean_device:
        watch_query = watch_query.filter(or_(TautulliWatchHistory.player == clean_device, TautulliWatchHistory.platform == clean_device))
    if clean_library:
        watch_query = watch_query.filter(TautulliWatchHistory.library_name == clean_library)
    if clean_media_type:
        watch_query = watch_query.filter(TautulliWatchHistory.media_type == clean_media_type)
    if clean_q:
        watch_query = watch_query.filter(func.lower(TautulliWatchHistory.title).contains(clean_q.lower()))
    parsed_from = parsed_to = None
    try:
        if (from_date or "").strip():
            parsed_from = datetime.strptime(from_date.strip(), "%Y-%m-%d")
            watch_query = watch_query.filter(TautulliWatchHistory.watched_at >= parsed_from)
    except ValueError:
        from_date = None
    try:
        if (to_date or "").strip():
            parsed_to = datetime.strptime(to_date.strip(), "%Y-%m-%d") + timedelta(days=1)
            watch_query = watch_query.filter(TautulliWatchHistory.watched_at < parsed_to)
    except ValueError:
        to_date = None

    try:
        per_page = int((rows or "10").strip())
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in {10, 25, 50}:
        per_page = 10
    try:
        current_page = max(1, int((page or "1").strip()))
    except (TypeError, ValueError):
        current_page = 1

    watch_filtered_total = watch_query.count()
    watch_total_pages = max(1, (watch_filtered_total + per_page - 1) // per_page)
    current_page = min(current_page, watch_total_pages)
    raw_watch_history = (
        watch_query.order_by(TautulliWatchHistory.watched_at.desc(), TautulliWatchHistory.id.desc())
        .offset((current_page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    browser_names = ("chrome", "firefox", "safari", "edge", "opera", "brave", "browser", "web")
    watch_history = []
    for item in raw_watch_history:
        player = (item.player or "").strip()
        platform = (item.platform or "").strip()
        player_lower = player.lower()
        platform_label = "Web" if any(name in player_lower for name in browser_names) else (platform or "—")
        watch_history.append({
            "title": item.title,
            "watched_at": item.watched_at,
            "media_type": item.media_type,
            "library_name": item.library_name or "—",
            "device_label": player or platform or "Unknown device",
            "platform_label": platform_label,
            "duration_seconds": item.duration_seconds,
        })

    base_history = db.query(TautulliWatchHistory).filter(TautulliWatchHistory.customer_id == customer.id)
    device_rows = base_history.with_entities(TautulliWatchHistory.player, TautulliWatchHistory.platform).distinct().all()
    devices = sorted({str(player or platform).strip() for player, platform in device_rows if str(player or platform or "").strip()}, key=str.lower)
    libraries = sorted({row[0] for row in base_history.with_entities(TautulliWatchHistory.library_name).distinct().all() if row[0]}, key=str.lower)
    media_types = sorted({row[0] for row in base_history.with_entities(TautulliWatchHistory.media_type).distinct().all() if row[0]}, key=str.lower)
    total_cached = base_history.count()

    return render(
        request, "portal_activity.html", customer=customer, activity=activity, live=live,
        live_refresh_seconds=settings_row.live_refresh_seconds, subscription=sub, stream_events=events,
        watch_history=watch_history, watch_devices=devices, watch_libraries=libraries, watch_media_types=media_types,
        watch_total_cached=total_cached, watch_filtered_total=watch_filtered_total, watch_page=current_page,
        watch_per_page=per_page, watch_total_pages=watch_total_pages,
        watch_filters={"device": clean_device, "library": clean_library, "media_type": clean_media_type, "q": clean_q, "from_date": from_date or "", "to_date": to_date or ""},
        watch_pagination_base=urlencode({k: v for k, v in {"q": clean_q, "device": clean_device, "library": clean_library, "media_type": clean_media_type, "from_date": from_date or "", "to_date": to_date or "", "rows": per_page}.items() if str(v) != ""}),
        notice=request.query_params.get("notice"), error=request.query_params.get("error"), now=datetime.utcnow(),
    )


@app.get("/portal/api/activity", response_class=JSONResponse)
def portal_activity_api(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    settings_row = get_tautulli_settings(db)
    live = customer_live_sessions(db, customer, max_age_seconds=settings_row.live_refresh_seconds)
    sessions = []
    for item in live.get("sessions", []):
        sessions.append({
            "session_key": item.get("session_key"),
            "title": item.get("title") or "Unknown title",
            "state": item.get("state") or "playing",
            "player": item.get("player"),
        })
    sampled_at = live.get("sampled_at")
    return {
        "available": not bool(live.get("error")),
        "stale": bool(live.get("error")),
        "sampled_at": sampled_at.isoformat() + "Z" if isinstance(sampled_at, datetime) else None,
        "sessions": sessions,
        "refresh_seconds": settings_row.live_refresh_seconds,
    }


@app.post("/portal/activity/stop")
def portal_stop_stream(request: Request, session_key: str = Form(...), db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        response = RedirectResponse("/portal/login", status_code=303)
        response.delete_cookie("sm_portal_session", path="/portal")
        return response
    try:
        session = terminate_customer_session(db, customer, session_key)
        title = str(session.get("title") or "stream")[:180]
        db.add(AuditLog(actor=f"portal:{customer.id}", action="portal.stream.stop", target_type="customer", target_id=str(customer.id), detail=f"Customer stopped own stream: {title}"))
        db.commit()
        return RedirectResponse("/portal/activity?notice=" + quote_plus(f"Stopped {title}"), status_code=303)
    except PermissionError as exc:
        return RedirectResponse("/portal/activity?error=" + quote_plus(str(exc)), status_code=303)
    except Exception:
        logger.exception("Customer portal stream termination failed for customer %s", customer.id)
        return RedirectResponse("/portal/activity?error=" + quote_plus("Could not stop that stream. Please try again."), status_code=303)


@app.get("/tickets", response_class=HTMLResponse)
def admin_tickets(request: Request, status: str | None = None, category: str | None = None, priority: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    query = db.query(SupportTicket).options(joinedload(SupportTicket.customer)).filter(SupportTicket.status != "closed")
    clean_status=(status or "").strip(); clean_category=(category or "").strip(); clean_priority=(priority or "").strip(); clean_q=(q or "").strip()
    if clean_status in TICKET_STATUSES and clean_status != "closed": query=query.filter(SupportTicket.status==clean_status)
    if clean_category in TICKET_CATEGORIES: query=query.filter(SupportTicket.category==clean_category)
    if clean_priority in TICKET_PRIORITIES: query=query.filter(SupportTicket.priority==clean_priority)
    if clean_q:
        like=f"%{clean_q.lower()}%"; query=query.join(SupportTicket.customer).filter(or_(func.lower(SupportTicket.subject).like(like), func.lower(SupportTicket.reference).like(like), func.lower(Customer.name).like(like)))
    tickets=query.order_by(SupportTicket.admin_unread.desc(), SupportTicket.updated_at.desc()).all()
    unread=db.query(SupportTicket).filter(SupportTicket.status!="closed", SupportTicket.admin_unread.is_(True)).count()
    return render(request,"tickets.html",tickets=tickets,closed=False,unread_count=unread,categories=TICKET_CATEGORIES,statuses=TICKET_STATUSES,priorities=TICKET_PRIORITIES,filters={"status":clean_status,"category":clean_category,"priority":clean_priority,"q":clean_q},notice=request.query_params.get("notice"),error=request.query_params.get("error"))


@app.get("/tickets/closed", response_class=HTMLResponse)
def admin_closed_tickets(request: Request, q: str | None = None, db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    query=db.query(SupportTicket).options(joinedload(SupportTicket.customer)).filter(SupportTicket.status=="closed")
    clean_q=(q or "").strip()
    if clean_q:
        like=f"%{clean_q.lower()}%"; query=query.join(SupportTicket.customer).filter(or_(func.lower(SupportTicket.subject).like(like),func.lower(SupportTicket.reference).like(like),func.lower(Customer.name).like(like)))
    tickets=query.order_by(SupportTicket.closed_at.desc(),SupportTicket.updated_at.desc()).all()
    return render(request,"tickets.html",tickets=tickets,closed=True,unread_count=0,categories=TICKET_CATEGORIES,statuses=TICKET_STATUSES,priorities=TICKET_PRIORITIES,filters={"q":clean_q,"status":"","category":"","priority":""},notice=request.query_params.get("notice"),error=request.query_params.get("error"))


@app.get("/tickets/{reference}", response_class=HTMLResponse)
def admin_ticket_detail(reference: str, request: Request, db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    ticket=db.query(SupportTicket).options(joinedload(SupportTicket.customer),joinedload(SupportTicket.messages)).filter(SupportTicket.reference==reference).first()
    if not ticket: return RedirectResponse("/tickets?error=Ticket+not+found",status_code=303)
    if ticket.admin_unread: ticket.admin_unread=False; db.commit()
    return render(request,"ticket_detail.html",ticket=ticket,categories=TICKET_CATEGORIES,statuses=TICKET_STATUSES,priorities=TICKET_PRIORITIES,notice=request.query_params.get("notice"),error=request.query_params.get("error"))


@app.post("/tickets/{reference}/reply")
def admin_ticket_reply(reference: str, request: Request, body: str = Form(...), db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    ticket=db.query(SupportTicket).filter(SupportTicket.reference==reference).first()
    if not ticket: return RedirectResponse("/tickets?error=Ticket+not+found",status_code=303)
    try: add_admin_reply(db,ticket,body,settings.admin_username); return RedirectResponse(f"/tickets/{reference}?notice="+quote_plus("Reply sent"),status_code=303)
    except ValueError as exc: return RedirectResponse(f"/tickets/{reference}?error="+quote_plus(str(exc)),status_code=303)


@app.post("/tickets/{reference}/note")
def admin_ticket_note(reference: str, request: Request, body: str = Form(...), db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    ticket=db.query(SupportTicket).filter(SupportTicket.reference==reference).first()
    if not ticket: return RedirectResponse("/tickets?error=Ticket+not+found",status_code=303)
    try: add_internal_note(db,ticket,body,settings.admin_username); return RedirectResponse(f"/tickets/{reference}?notice="+quote_plus("Internal note added"),status_code=303)
    except ValueError as exc: return RedirectResponse(f"/tickets/{reference}?error="+quote_plus(str(exc)),status_code=303)


@app.post("/tickets/{reference}/status")
def admin_ticket_status(reference: str, request: Request, status: str = Form(...), db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    ticket=db.query(SupportTicket).filter(SupportTicket.reference==reference).first()
    if not ticket: return RedirectResponse("/tickets?error=Ticket+not+found",status_code=303)
    try: change_status(db,ticket,status,settings.admin_username); dest="/tickets/closed" if status=="closed" else f"/tickets/{reference}"; return RedirectResponse(dest+"?notice="+quote_plus("Ticket status updated"),status_code=303)
    except ValueError as exc: return RedirectResponse(f"/tickets/{reference}?error="+quote_plus(str(exc)),status_code=303)


@app.post("/tickets/{reference}/priority")
def admin_ticket_priority(reference: str, request: Request, priority: str = Form(...), db: Session = Depends(get_db)):
    gate=auth(request)
    if gate: return gate
    ticket=db.query(SupportTicket).filter(SupportTicket.reference==reference).first()
    if not ticket: return RedirectResponse("/tickets?error=Ticket+not+found",status_code=303)
    try: change_priority(db,ticket,priority,settings.admin_username); return RedirectResponse(f"/tickets/{reference}?notice="+quote_plus("Priority updated"),status_code=303)
    except ValueError as exc: return RedirectResponse(f"/tickets/{reference}?error="+quote_plus(str(exc)),status_code=303)


@app.get("/api/admin/tickets/summary")
def admin_ticket_summary(request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    active_count = db.query(func.count(SupportTicket.id)).filter(SupportTicket.status != "closed").scalar() or 0
    unread_count = db.query(func.count(SupportTicket.id)).filter(SupportTicket.status != "closed", SupportTicket.admin_unread.is_(True)).scalar() or 0
    return {"active_count": int(active_count), "unread_count": int(unread_count)}


@app.get("/api/admin/tickets/{reference}/updates")
def admin_ticket_updates(reference: str, request: Request, after: int = 0, db: Session = Depends(get_db)):
    if not logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ticket = db.query(SupportTicket).options(joinedload(SupportTicket.customer)).filter(SupportTicket.reference == reference).first()
    if not ticket:
        return JSONResponse({"error": "Ticket not found"}, status_code=404)
    messages = (
        db.query(SupportTicketMessage)
        .filter(SupportTicketMessage.ticket_id == ticket.id, SupportTicketMessage.id > max(0, after))
        .order_by(SupportTicketMessage.id.asc())
        .all()
    )
    if ticket.admin_unread:
        ticket.admin_unread = False
        db.commit()
    return {
        "ticket": _ticket_state_json(ticket),
        "messages": [_ticket_message_json(m, customer_name=ticket.customer.name) for m in messages],
        "latest_message_id": messages[-1].id if messages else after,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    due_soon = db.query(Subscription).join(Subscription.customer).filter(
        Subscription.status == "active",
        Customer.archived.is_(False),
        Subscription.current_period_end.is_not(None),
        Subscription.current_period_end >= now,
        Subscription.current_period_end <= now + timedelta(days=7),
    ).count()
    revenue = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.paid_at >= month_start, Payment.voided_at.is_(None)).scalar()

    # Forward-looking revenue forecast based purely on the current live
    # subscription distribution. Grace customers are still considered live;
    # exempt, suspended and cancelled customers are intentionally excluded.
    forecast_rows = (
        db.query(Subscription)
        .join(Subscription.customer)
        .options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package))
        .filter(
            Subscription.status.in_(["active", "grace"]),
            Customer.exempt == False,  # noqa: E712
            Customer.archived.is_(False),
        )
        .all()
    )
    forecast_by_package: dict[int, dict] = {}
    forecast_monthly = Decimal("0.00")
    forecast_yearly = Decimal("0.00")
    for sub in forecast_rows:
        tier = sub.billing_tier
        package = tier.package
        package_row = forecast_by_package.setdefault(package.id, {
            "name": package.name,
            "customers": 0,
            "monthly_customers": 0,
            "yearly_customers": 0,
            "monthly": Decimal("0.00"),
            "yearly": Decimal("0.00"),
        })
        package_row["customers"] += 1
        price = Decimal(tier.price or 0)
        count = max(int(tier.interval_count or 1), 1)
        if tier.interval_unit == "month":
            # Normalise multi-month tiers to an average monthly figure.
            amount = price / Decimal(count)
            forecast_monthly += amount
            package_row["monthly"] += amount
            package_row["monthly_customers"] += 1
        elif tier.interval_unit == "year":
            # Likewise, a two-year tier contributes half its price per year.
            amount = price / Decimal(count)
            forecast_yearly += amount
            package_row["yearly"] += amount
            package_row["yearly_customers"] += 1

    revenue_forecast = {
        "monthly": forecast_monthly,
        "yearly": forecast_yearly,
        "annualised": (forecast_monthly * Decimal("12")) + forecast_yearly,
        "packages": sorted(forecast_by_package.values(), key=lambda row: row["name"].lower()),
    }

    stats = {
        "customers": db.query(Customer).filter(Customer.archived.is_(False)).count(),
        "active": db.query(Customer).filter(Customer.archived.is_(False), Customer.status == "active").count(),
        "grace": db.query(Customer).filter(Customer.archived.is_(False), Customer.status == "grace", Customer.exempt == False).count(),  # noqa: E712
        "suspended": db.query(Customer).filter(Customer.archived.is_(False), Customer.status == "suspended", Customer.exempt == False).count(),  # noqa: E712
        "due_soon": due_soon,
        "revenue": Decimal(revenue or 0),
    }
    recent = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()
    tautulli_settings = get_tautulli_settings(db)
    tautulli_enabled = bool(tautulli_settings.integration and tautulli_settings.integration.enabled)
    tautulli_usage = dashboard_usage(db, now=now) if tautulli_enabled else None
    return render(request, "dashboard.html", stats=stats, recent=recent, revenue_forecast=revenue_forecast, tautulli_usage=tautulli_usage, tautulli_settings=tautulli_settings if tautulli_enabled else None)


@app.post("/billing/run")
def billing_run(request: Request):
    gate = auth(request)
    if gate:
        return gate
    changed = run_billing_cycle()
    return RedirectResponse(f"/?notice=Billing+check+complete%3A+{changed}+state+change%28s%29", status_code=303)


@app.get("/customers", response_class=HTMLResponse)
def customers(request: Request, error: str | None = None, notice: str | None = None, archived: int = 0, archived_match_id: int | None = None, archived_tier_id: int | None = None, archived_start_date: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    archived_mode = bool(archived)
    rows = db.query(Customer).options(
        joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier).joinedload(BillingTier.package),
        joinedload(Customer.credits),
    ).filter(Customer.archived.is_(archived_mode)).order_by(Customer.name).all()
    # Resolve the subscription shown/acted on by the customer card in Python,
    # rather than duplicating lifecycle rules in Jinja. Prefer a currently
    # assigned row, but retain the most recent historical tier as a fallback
    # so complimentary access can reactivate a former subscriber.
    activities = {row.customer_id: row for row in db.query(TautulliActivity).all()}
    for customer in rows:
        ordered = sorted(customer.subscriptions, key=lambda sub: sub.id, reverse=True)
        customer.ui_subscription = next(
            (sub for sub in ordered if sub.status in ASSIGNED_SUBSCRIPTION_STATES),
            ordered[0] if ordered else None,
        )
        customer.tautulli_activity = activities.get(customer.id)
        customer.portal_username_suggestion = customer.portal_username or _portal_username_suggestion(db, customer)
    tiers = db.query(BillingTier).options(
        joinedload(BillingTier.package).joinedload(Package.entitlements).joinedload(PackageEntitlement.integration)
    ).filter(
        BillingTier.active == True, BillingTier.package.has(active=True)  # noqa: E712
    ).order_by(BillingTier.package_id, BillingTier.price).all()
    plex_ready_tier_ids = set()
    for tier in tiers:
        if any(
            entitlement.resource_type == "library"
            and entitlement.integration.kind == "plex"
            and entitlement.integration.enabled
            for entitlement in tier.package.entitlements
        ):
            plex_ready_tier_ids.add(tier.id)
    archived_match = None
    archived_match_tier = None
    archived_match_start_date = archived_start_date or datetime.utcnow().strftime("%Y-%m-%d")
    if not archived_mode and archived_match_id:
        candidate = db.get(Customer, archived_match_id)
        if candidate and candidate.archived:
            archived_match = candidate
            if archived_tier_id:
                archived_match_tier = next((tier for tier in tiers if tier.id == archived_tier_id), None)

    return render(
        request,
        "customers.html",
        customers=rows,
        tiers=tiers,
        plex_ready_tier_ids=plex_ready_tier_ids,
        archived_mode=archived_mode,
        archived_match=archived_match,
        archived_match_tier=archived_match_tier,
        archived_match_start_date=archived_match_start_date,
        error=error,
        notice=notice,
        today=datetime.utcnow().strftime("%Y-%m-%d"),
        today_dt=datetime.utcnow(),
    )


@app.post("/customers/onboard")
def onboard_customer(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    plex_username: str = Form(""),
    billing_tier_id: int = Form(...),
    start_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate

    clean_name = name.strip()
    clean_email = email.strip() or None
    clean_plex = plex_username.strip() or clean_email
    if not clean_name or not clean_plex:
        return RedirectResponse("/customers?error=Name+and+Plex+username%2Femail+are+required", status_code=303)

    duplicate = db.query(Customer).filter(
        or_(
            func.lower(Customer.plex_username) == clean_plex.lower(),
            func.lower(Customer.email) == clean_email.lower() if clean_email else False,
        )
    ).first()
    if duplicate:
        if duplicate.archived:
            start = start_date.strip() or datetime.utcnow().strftime("%Y-%m-%d")
            return RedirectResponse(
                "/customers?"
                f"archived_match_id={duplicate.id}&archived_tier_id={billing_tier_id}&archived_start_date={quote_plus(start)}",
                status_code=303,
            )
        return RedirectResponse(f"/customers?error={quote_plus('A customer with that Plex identity or email already exists')}", status_code=303)

    tier = (
        db.query(BillingTier)
        .options(joinedload(BillingTier.package).joinedload(Package.entitlements).joinedload(PackageEntitlement.integration))
        .filter(BillingTier.id == billing_tier_id)
        .first()
    )
    if not tier or not tier.active or not tier.package.active:
        return RedirectResponse("/customers?error=Invalid+customer+or+billing+tier", status_code=303)

    plex_entitlements = [
        entitlement for entitlement in tier.package.entitlements
        if entitlement.resource_type == "library"
        and entitlement.integration.kind == "plex"
        and entitlement.integration.enabled
    ]
    if not plex_entitlements:
        return RedirectResponse("/customers?error=That+package+has+no+enabled+Plex+library+entitlements", status_code=303)

    start = _parse_date(start_date, datetime.utcnow())
    customer = Customer(
        name=clean_name,
        email=clean_email,
        plex_username=clean_plex,
        notes=notes.strip() or None,
        status="active",
    )
    db.add(customer)
    db.flush()
    subscription = Subscription(customer=customer, billing_tier=tier, status="active")
    initialize_subscription_period(subscription, start)
    db.add(subscription)
    db.add(AuditLog(
        actor=settings.admin_username,
        action="customer.onboard",
        target_type="customer",
        target_id=str(customer.id),
        detail=f"{tier.package.name} / {tier.name}; starts {start:%Y-%m-%d}; Plex identity {clean_plex}",
    ))
    db.commit()

    try:
        messages = reconcile_customer(db, customer)
    except Exception as exc:
        _notify_reconcile_failure(db, customer, exc)
        return RedirectResponse(
            f"/customers?error={quote_plus('Customer created, but Plex invitation failed: ' + str(exc))}",
            status_code=303,
        )

    invited = any("invitation" in message.lower() for message in messages)
    notice = "Customer created and Plex invitation sent" if invited else "Customer created and Plex access reconciled"
    return RedirectResponse(f"/customers?notice={quote_plus(notice)}", status_code=303)


@app.post("/customers")
def create_customer(request: Request, name: str = Form(...), email: str = Form(""), plex_username: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    c = Customer(name=name.strip(), email=email.strip() or None, plex_username=plex_username.strip() or None, notes=notes.strip() or None)
    db.add(c)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="customer.create", target_type="customer", target_id=str(c.id), detail=c.name))
    db.commit()
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/edit")
def edit_customer(
    request: Request,
    customer_id: int,
    name: str = Form(...),
    email: str = Form(""),
    plex_username: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate

    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)

    clean_name = name.strip()
    clean_email = email.strip() or None
    clean_plex = plex_username.strip() or None
    if not clean_name:
        return RedirectResponse("/customers?error=Customer+name+is+required", status_code=303)

    if clean_plex:
        duplicate = db.query(Customer).filter(
            Customer.id != customer_id,
            func.lower(Customer.plex_username) == clean_plex.lower(),
        ).first()
        if duplicate:
            return RedirectResponse(
                f"/customers?error={quote_plus('Another customer already uses that Plex username/email')}",
                status_code=303,
            )

    old_name = customer.name
    old_plex = customer.plex_username
    customer.name = clean_name
    customer.email = clean_email
    customer.plex_username = clean_plex
    customer.notes = notes.strip() or None

    # The stored numeric Plex ID belongs to the old Plex identity. If the
    # operator changes that identity, force future reconciliation to resolve
    # or invite the new account rather than accidentally targeting the old one.
    plex_changed = (old_plex or "").lower() != (clean_plex or "").lower()
    if plex_changed:
        customer.plex_user_id = None

    changes = [f"name {old_name!r} -> {clean_name!r}"] if old_name != clean_name else []
    if plex_changed:
        changes.append(f"Plex identity {old_plex or 'none'} -> {clean_plex or 'none'}")
    db.add(AuditLog(
        actor=settings.admin_username,
        action="customer.edit",
        target_type="customer",
        target_id=str(customer.id),
        detail="; ".join(changes) or "Customer metadata updated",
    ))
    db.commit()
    return RedirectResponse("/customers?notice=Customer+updated", status_code=303)


@app.post("/customers/{customer_id}/portal/enable", response_class=HTMLResponse)
def enable_customer_portal(
    request: Request,
    customer_id: int,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)
    if customer.archived or customer.status == "cancelled":
        return RedirectResponse("/customers?error=Cancelled+or+archived+customers+cannot+have+portal+access", status_code=303)
    clean_username = username.strip().lower()
    if len(clean_username) < 3 or len(clean_username) > 120 or not re.fullmatch(r"[a-z0-9._-]+", clean_username):
        return RedirectResponse("/customers?error=Portal+username+must+use+3-120+letters%2C+numbers%2C+dots%2C+dashes+or+underscores", status_code=303)
    duplicate = db.query(Customer).filter(Customer.id != customer.id, func.lower(Customer.portal_username) == clean_username).first()
    if duplicate:
        return RedirectResponse("/customers?error=That+portal+username+is+already+in+use", status_code=303)
    if len(password) < 12:
        return RedirectResponse("/customers?error=Portal+password+must+be+at+least+12+characters", status_code=303)
    customer.portal_username = clean_username
    customer.portal_password_hash = hash_portal_password(password)
    customer.portal_enabled = True
    customer.portal_enabled_at = datetime.utcnow()
    customer.portal_disabled_at = None
    customer.portal_session_version = int(customer.portal_session_version or 1) + 1
    db.add(AuditLog(actor=settings.admin_username, action="customer.portal.enable", target_type="customer", target_id=str(customer.id), detail=f"Portal enabled for {clean_username}"))
    db.commit()
    return render(request, "portal_credentials.html", customer=customer, portal_username=clean_username, portal_password=password, action="enabled")


@app.post("/customers/{customer_id}/portal/reset", response_class=HTMLResponse)
def reset_customer_portal_password(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer or not customer.portal_enabled or not customer.portal_username:
        return RedirectResponse("/customers?error=Customer+portal+is+not+enabled", status_code=303)
    password = generate_portal_password()
    customer.portal_password_hash = hash_portal_password(password)
    customer.portal_session_version = int(customer.portal_session_version or 1) + 1
    db.add(AuditLog(actor=settings.admin_username, action="customer.portal.password_reset", target_type="customer", target_id=str(customer.id), detail="Customer portal password reset; sessions revoked"))
    db.commit()
    return render(request, "portal_credentials.html", customer=customer, portal_username=customer.portal_username, portal_password=password, action="reset")


@app.post("/customers/{customer_id}/portal/disable")
def disable_customer_portal(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)
    _disable_customer_portal(customer)
    db.add(AuditLog(actor=settings.admin_username, action="customer.portal.disable", target_type="customer", target_id=str(customer.id), detail="Customer portal disabled; sessions revoked"))
    db.commit()
    return RedirectResponse("/customers?notice=Customer+portal+disabled", status_code=303)


@app.post("/customers/{customer_id}/archive")
def archive_customer(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)
    if customer.archived:
        return RedirectResponse("/customers?notice=Customer+already+archived", status_code=303)
    if customer.exempt or customer.status != "cancelled":
        return RedirectResponse(
            "/customers?error=Customer+must+be+Cancelled+before+archiving",
            status_code=303,
        )
    customer.archived = True
    customer.archived_at = datetime.utcnow()
    db.add(AuditLog(actor=settings.admin_username, action="customer.archive", target_type="customer", target_id=str(customer.id), detail=customer.name))
    db.commit()
    return RedirectResponse("/customers?notice=Customer+archived", status_code=303)


@app.post("/customers/{customer_id}/restore")
def restore_customer(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers?archived=1&error=Customer+not+found", status_code=303)
    customer.archived = False
    customer.archived_at = None
    db.add(AuditLog(actor=settings.admin_username, action="customer.restore", target_type="customer", target_id=str(customer.id), detail=customer.name))
    db.commit()
    return RedirectResponse("/customers?notice=Customer+restored", status_code=303)


@app.post("/customers/{customer_id}/restore-onboard")
def restore_onboard_customer(
    request: Request,
    customer_id: int,
    billing_tier_id: int = Form(...),
    start_date: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate

    customer = db.get(Customer, customer_id)
    if not customer or not customer.archived:
        return RedirectResponse("/customers?error=Archived+customer+not+found", status_code=303)

    tier = (
        db.query(BillingTier)
        .options(joinedload(BillingTier.package).joinedload(Package.entitlements).joinedload(PackageEntitlement.integration))
        .filter(BillingTier.id == billing_tier_id)
        .first()
    )
    if not tier or not tier.active or not tier.package.active:
        return RedirectResponse("/customers?error=Invalid+billing+tier", status_code=303)

    plex_entitlements = [
        entitlement for entitlement in tier.package.entitlements
        if entitlement.resource_type == "library"
        and entitlement.integration.kind == "plex"
        and entitlement.integration.enabled
    ]
    if not plex_entitlements:
        return RedirectResponse("/customers?error=That+package+has+no+enabled+Plex+library+entitlements", status_code=303)

    existing = db.query(Subscription).filter(
        Subscription.customer_id == customer.id,
        Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
    ).all()
    for old in existing:
        old.status = "cancelled"
        old.cancelled_at = datetime.utcnow()

    start = _parse_date(start_date, datetime.utcnow())
    subscription = Subscription(customer_id=customer.id, billing_tier_id=tier.id, status="active")
    subscription.billing_tier = tier
    initialize_subscription_period(subscription, start)

    customer.archived = False
    customer.archived_at = None
    customer.status = "active"
    customer.exempt = False
    db.add(subscription)
    db.add(AuditLog(
        actor=settings.admin_username,
        action="customer.restore_onboard",
        target_type="customer",
        target_id=str(customer.id),
        detail=f"Restored and assigned {tier.package.name} / {tier.name}; starts {start:%Y-%m-%d}",
    ))
    db.commit()

    if settings.reconcile_on_assign and customer.plex_username:
        try:
            messages = reconcile_customer(db, customer)
        except Exception as exc:
            _notify_reconcile_failure(db, customer, exc)
            return RedirectResponse(
                f"/customers?error={quote_plus('Customer restored and package assigned, but Plex reconciliation failed: ' + str(exc))}",
                status_code=303,
            )
        invited = any("invitation" in message.lower() for message in messages)
        notice = "Archived customer restored, package assigned and Plex invitation sent" if invited else "Archived customer restored, package assigned and Plex access reconciled"
    else:
        notice = "Archived customer restored and package assigned"

    return RedirectResponse(f"/customers?notice={quote_plus(notice)}", status_code=303)


@app.post("/customers/bulk")
def bulk_customers(
    request: Request,
    customer_ids: list[int] = Form(...),
    action: str = Form(...),
    status: str = Form(""),
    billing_tier_id: str = Form(""),
    start_date: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate

    ids = sorted(set(customer_ids))
    if not ids:
        return RedirectResponse("/customers?error=Select+at+least+one+customer", status_code=303)

    customers = db.query(Customer).filter(Customer.id.in_(ids), Customer.archived.is_(False)).order_by(Customer.name).all()
    if not customers:
        return RedirectResponse("/customers?error=No+active+customer+records+were+selected", status_code=303)

    selected_count = len(customers)
    skipped = len(ids) - selected_count
    reconcile_targets: list[Customer] = []
    notification_transitions: list[tuple[Customer, str]] = []

    if action == "status":
        allowed = {"active", "grace", "suspended", "cancelled", "exempt"}
        if status not in allowed:
            return RedirectResponse("/customers?error=Choose+a+valid+bulk+status", status_code=303)
        now = datetime.utcnow()
        for customer in customers:
            previous_status = customer.status
            if status == "exempt":
                customer.exempt = True
                detail = "exempt"
            else:
                customer.exempt = False
                customer.status = status
                detail = status
                current = db.query(Subscription).filter(
                    Subscription.customer_id == customer.id,
                    Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
                ).order_by(Subscription.id.desc()).first()
                if current:
                    if status == "cancelled":
                        current.status = "cancelled"
                        current.cancelled_at = now
                    elif status in ASSIGNED_SUBSCRIPTION_STATES:
                        current.status = status
                if status == "cancelled":
                    _disable_customer_portal(customer, now)
            db.add(AuditLog(actor=settings.admin_username, action="customer.status", target_type="customer", target_id=str(customer.id), detail=f"{detail} (bulk)"))
            if not customer.exempt:
                if customer.status == "grace" and previous_status != "grace":
                    notification_transitions.append((customer, "grace"))
                elif customer.status == "suspended" and previous_status != "suspended":
                    notification_transitions.append((customer, "suspended"))
                elif customer.status == "active" and previous_status == "suspended":
                    notification_transitions.append((customer, "active"))
            if settings.reconcile_on_assign and customer.plex_username:
                reconcile_targets.append(customer)
        db.commit()

        for customer, transition in notification_transitions:
            if transition == "grace":
                notify_event(db, event="customer.entered_grace", title="Customer entered grace", message=f"{customer.name} has entered their billing grace period.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})
            elif transition == "suspended":
                notify_event(db, event="customer.suspended", title="Customer suspended", message=f"{customer.name} has been suspended.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})
            elif transition == "active":
                notify_event(db, event="customer.reactivated", title="Customer reactivated", message=f"{customer.name} is active again.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})

        queued_customers = 0
        for customer in reconcile_targets:
            count = enqueue_reconciliation(db, customer)
            if count:
                queued_customers += 1
        db.commit()
        suffix = f"; queued Plex reconciliation for {queued_customers} customer(s)" if queued_customers else ""
        return RedirectResponse(f"/customers?notice={quote_plus(f'Updated {selected_count} customer(s) to {status}{suffix}')}" , status_code=303)

    if action == "package":
        if not billing_tier_id.strip():
            return RedirectResponse("/customers?error=Choose+a+billing+tier+for+the+bulk+package+change", status_code=303)
        try:
            tier_id = int(billing_tier_id)
        except ValueError:
            return RedirectResponse("/customers?error=Invalid+bulk+billing+tier", status_code=303)
        tier = db.query(BillingTier).options(joinedload(BillingTier.package)).filter(BillingTier.id == tier_id).first()
        if not tier or not tier.active or not tier.package.active:
            return RedirectResponse("/customers?error=Invalid+bulk+billing+tier", status_code=303)
        start = _parse_date(start_date, datetime.utcnow())
        now = datetime.utcnow()
        for customer in customers:
            existing = db.query(Subscription).filter(
                Subscription.customer_id == customer.id,
                Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
            ).all()
            for old in existing:
                old.status = "cancelled"
                old.cancelled_at = now
            sub = Subscription(customer_id=customer.id, billing_tier_id=tier.id, status="active")
            sub.billing_tier = tier
            initialize_subscription_period(sub, start)
            customer.status = "active"
            db.add(sub)
            db.add(AuditLog(
                actor=settings.admin_username,
                action="subscription.assign",
                target_type="customer",
                target_id=str(customer.id),
                detail=f"{tier.package.name} / {tier.name}; starts {start:%Y-%m-%d}; paid through {sub.current_period_end:%Y-%m-%d} (bulk)",
            ))
            if settings.reconcile_on_assign and customer.plex_username:
                reconcile_targets.append(customer)
        db.commit()
        queued_customers = 0
        for customer in reconcile_targets:
            if enqueue_reconciliation(db, customer):
                queued_customers += 1
        db.commit()
        suffix = f"; queued Plex reconciliation for {queued_customers} customer(s)" if queued_customers else ""
        return RedirectResponse(f"/customers?notice={quote_plus(f'Changed package for {selected_count} customer(s){suffix}')}" , status_code=303)

    if action == "reconcile":
        queued = 0
        for customer in customers:
            if customer.exempt or not customer.plex_username:
                skipped += 1
                continue
            count = enqueue_reconciliation(db, customer)
            if not count:
                skipped += 1
                continue
            queued += 1
            db.add(AuditLog(
                actor=settings.admin_username,
                action="reconcile.queued",
                target_type="customer",
                target_id=str(customer.id),
                detail="Queued manual Plex reconciliation (bulk)",
            ))
        db.commit()
        detail = f"Queued Plex reconciliation for {queued} customer(s)"
        if skipped:
            detail += f"; {skipped} skipped"
        return RedirectResponse(f"/customers?notice={quote_plus(detail)}", status_code=303)

    if action == "archive":
        archived = 0
        ineligible = skipped
        now = datetime.utcnow()
        for customer in customers:
            if customer.exempt or customer.status != "cancelled":
                ineligible += 1
                continue
            customer.archived = True
            customer.archived_at = now
            db.add(AuditLog(actor=settings.admin_username, action="customer.archive", target_type="customer", target_id=str(customer.id), detail=f"{customer.name} (bulk)"))
            archived += 1
        db.commit()
        detail = f"Archived {archived} customer(s)"
        if ineligible:
            detail += f"; {ineligible} skipped because they were not eligible"
        return RedirectResponse(f"/customers?notice={quote_plus(detail)}", status_code=303)

    return RedirectResponse("/customers?error=Choose+a+valid+bulk+action", status_code=303)


@app.post("/customers/{customer_id}/status")
def customer_status(request: Request, customer_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    c = db.get(Customer, customer_id)
    if not c:
        return RedirectResponse("/customers", status_code=303)
    previous_status = c.status
    allowed = {"active", "grace", "suspended", "cancelled", "exempt"}
    if status not in allowed:
        return RedirectResponse("/customers", status_code=303)

    if status == "exempt":
        c.exempt = True
        detail = "exempt"
    else:
        c.exempt = False
        c.status = status
        detail = status
        current = db.query(Subscription).filter(
            Subscription.customer_id == c.id,
            Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
        ).order_by(Subscription.id.desc()).first()
        if current:
            if status == "cancelled":
                current.status = "cancelled"
                current.cancelled_at = datetime.utcnow()
            elif status in ASSIGNED_SUBSCRIPTION_STATES:
                current.status = status
        if status == "cancelled":
            _disable_customer_portal(c)

    db.add(AuditLog(actor=settings.admin_username, action="customer.status", target_type="customer", target_id=str(c.id), detail=detail))
    db.commit()
    if not c.exempt:
        if c.status == "grace" and previous_status != "grace":
            notify_event(db, event="customer.entered_grace", title="Customer entered grace", message=f"{c.name} has entered their billing grace period.", target_type="customer", target_id=str(c.id), data={"customer": c.name})
        elif c.status == "suspended" and previous_status != "suspended":
            notify_event(db, event="customer.suspended", title="Customer suspended", message=f"{c.name} has been suspended.", target_type="customer", target_id=str(c.id), data={"customer": c.name})
        elif c.status == "active" and previous_status == "suspended":
            notify_event(db, event="customer.reactivated", title="Customer reactivated", message=f"{c.name} is active again.", target_type="customer", target_id=str(c.id), data={"customer": c.name})
    if settings.reconcile_on_assign and c.plex_username:
        try:
            reconcile_customer(db, c)
        except Exception as exc:
            _notify_reconcile_failure(db, c, exc)
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/subscribe")
def subscribe(
    request: Request,
    customer_id: int,
    billing_tier_id: int = Form(...),
    start_date: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    c = db.get(Customer, customer_id)
    tier = db.query(BillingTier).options(joinedload(BillingTier.package)).filter(BillingTier.id == billing_tier_id).first()
    if not c or not tier or not tier.active or not tier.package.active:
        return RedirectResponse("/customers?error=Invalid+customer+or+billing+tier", status_code=303)

    existing = db.query(Subscription).filter(
        Subscription.customer_id == customer_id,
        Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
    ).all()
    for old in existing:
        old.status = "cancelled"
        old.cancelled_at = datetime.utcnow()

    start = _parse_date(start_date, datetime.utcnow())
    sub = Subscription(customer_id=customer_id, billing_tier_id=billing_tier_id, status="active")
    sub.billing_tier = tier
    initialize_subscription_period(sub, start)
    c.status = "active"
    db.add(sub)
    db.add(AuditLog(
        actor=settings.admin_username,
        action="subscription.assign",
        target_type="customer",
        target_id=str(c.id),
        detail=f"{tier.package.name} / {tier.name}; starts {start:%Y-%m-%d}; paid through {sub.current_period_end:%Y-%m-%d}",
    ))
    db.commit()
    if settings.reconcile_on_assign and c.plex_username:
        try:
            reconcile_customer(db, c)
        except Exception as exc:
            _notify_reconcile_failure(db, c, exc)
    return RedirectResponse("/customers?notice=Subscription+assigned", status_code=303)


@app.post("/subscriptions/{subscription_id}/billing-dates")
def edit_subscription_dates(
    request: Request,
    subscription_id: int,
    start_date: str = Form(...),
    period_end: str = Form(""),
    manual_access_end: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    sub = db.query(Subscription).options(joinedload(Subscription.billing_tier)).filter(Subscription.id == subscription_id).first()
    if not sub:
        return RedirectResponse("/customers?error=Subscription+not+found", status_code=303)
    start = _parse_date(start_date)
    end = _parse_date(period_end)
    override_end = _parse_date(manual_access_end)
    if end and end <= start:
        return RedirectResponse("/customers?error=Paid-through+date+must+be+after+the+period+start", status_code=303)
    initialize_subscription_period(sub, start, end)
    sub.manual_access_end = override_end
    desired = desired_billing_status(sub, datetime.utcnow()) or "active"
    sub.status = desired
    if not sub.customer.exempt:
        sub.customer.status = desired
    db.add(AuditLog(
        actor=settings.admin_username,
        action="subscription.dates",
        target_type="subscription",
        target_id=str(sub.id),
        detail=(f"starts {start:%Y-%m-%d}; paid through {sub.current_period_end:%Y-%m-%d}; grace until {sub.grace_until:%Y-%m-%d}; manual access until {sub.manual_access_end:%Y-%m-%d}" if sub.manual_access_end else f"starts {start:%Y-%m-%d}; paid through {sub.current_period_end:%Y-%m-%d}; grace until {sub.grace_until:%Y-%m-%d}; manual access override cleared"),
    ))
    db.commit()
    if settings.reconcile_on_assign and sub.customer.plex_username and not sub.customer.exempt:
        try:
            reconcile_customer(db, sub.customer)
        except Exception as exc:
            _notify_reconcile_failure(db, sub.customer, exc)
    return RedirectResponse("/customers?notice=Billing+dates+updated", status_code=303)


@app.post("/customers/{customer_id}/credits")
def grant_subscription_credit(
    request: Request,
    customer_id: int,
    billing_periods: int = Form(...),
    granted_date: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)
    granted_at = _parse_date(granted_date, datetime.utcnow())
    previous_status = customer.status
    try:
        credit = apply_subscription_credit(
            db,
            customer=customer,
            periods=billing_periods,
            granted_at=granted_at,
            reason=reason,
            granted_by=settings.admin_username,
        )
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/customers?error={quote_plus(str(exc))}", status_code=303)

    tier = credit.subscription.billing_tier
    db.add(AuditLog(
        actor=settings.admin_username,
        action="subscription.credit",
        target_type="subscription_credit",
        target_id=str(credit.id),
        detail=(
            f"{credit.billing_periods} complimentary {tier.name} period(s); "
            f"coverage {credit.coverage_start:%Y-%m-%d} -> {credit.coverage_end:%Y-%m-%d}; "
            f"reason: {credit.reason or 'not specified'}"
        ),
    ))
    db.commit()
    if previous_status == "suspended" and customer.status == "active":
        notify_event(db, event="customer.reactivated", title="Customer reactivated", message=f"{customer.name} is active again after complimentary access was granted.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})


    if settings.reconcile_on_assign and customer.plex_username and not customer.exempt:
        try:
            reconcile_customer(db, customer)
        except Exception as exc:
            _notify_reconcile_failure(db, customer, exc)
    return RedirectResponse("/customers?notice=Complimentary+access+granted", status_code=303)


@app.post("/customers/{customer_id}/reconcile")
def reconcile(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    c = db.get(Customer, customer_id)
    try:
        messages = reconcile_customer(db, c)
        db.add(AuditLog(actor=settings.admin_username, action="reconcile.manual", target_type="customer", target_id=str(c.id), detail="; ".join(messages)))
    except Exception as exc:
        db.add(AuditLog(actor=settings.admin_username, action="reconcile.error", target_type="customer", target_id=str(c.id), detail=str(exc)))
        db.commit()
        notify_event(db, event="plex.reconcile_failed", title="Plex reconciliation failed", message=f"{c.name}: {exc}", target_type="customer", target_id=str(c.id), data={"customer": c.name})
        return RedirectResponse("/customers", status_code=303)
    db.commit()
    return RedirectResponse("/customers", status_code=303)


@app.get("/packages", response_class=HTMLResponse)
def packages(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    rows = db.query(Package).options(
        joinedload(Package.billing_tiers).joinedload(BillingTier.subscriptions),
        joinedload(Package.entitlements).joinedload(PackageEntitlement.integration),
    ).filter(Package.active == True).order_by(Package.name).all()  # noqa: E712
    integrations = db.query(Integration).filter(Integration.enabled == True).all()  # noqa: E712
    selected = {}
    for package in rows:
        for entitlement in package.entitlements:
            selected.setdefault(f"{package.id}:{entitlement.integration_id}", []).append(entitlement.resource_id)
    return render(request, "packages.html", packages=rows, integrations=integrations, selected=selected, error=error, notice=notice)


@app.post("/packages")
def create_package(request: Request, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    p = Package(name=name.strip(), description=description.strip() or None)
    db.add(p)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="package.create", target_type="package", target_id=str(p.id), detail=p.name))
    db.commit()
    return RedirectResponse("/packages", status_code=303)


@app.post("/packages/{package_id}/edit")
def edit_package(request: Request, package_id: int, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    p = db.get(Package, package_id)
    if not p:
        return RedirectResponse("/packages?error=Package+not+found", status_code=303)
    clean_name = name.strip()
    if not clean_name:
        return RedirectResponse("/packages?error=Package+name+cannot+be+blank", status_code=303)
    duplicate = db.query(Package).filter(Package.name == clean_name, Package.id != package_id).first()
    if duplicate:
        return RedirectResponse("/packages?error=A+package+with+that+name+already+exists", status_code=303)
    old_name = p.name
    p.name = clean_name
    p.description = description.strip() or None
    db.add(AuditLog(actor=settings.admin_username, action="package.update", target_type="package", target_id=str(p.id), detail=f"{old_name} -> {p.name}"))
    db.commit()
    return RedirectResponse("/packages?notice=Package+updated", status_code=303)


@app.post("/packages/{package_id}/delete")
def delete_package(request: Request, package_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    p = db.query(Package).options(joinedload(Package.billing_tiers).joinedload(BillingTier.subscriptions)).filter(Package.id == package_id).first()
    if not p:
        return RedirectResponse("/packages?error=Package+not+found", status_code=303)
    assigned = sum(t.current_subscription_count for t in p.billing_tiers)
    if assigned:
        return RedirectResponse(f"/packages?error=Cannot+delete+package%3A+it+is+used+by+{assigned}+assigned+subscription%28s%29", status_code=303)
    name = p.name
    historical = sum(len(t.subscriptions) for t in p.billing_tiers)
    if historical:
        p.active = False
        for tier in p.billing_tiers:
            tier.active = False
        action = "package.archive"
        notice = "Package+archived%3B+historical+subscriptions+preserved"
    else:
        db.delete(p)
        action = "package.delete"
        notice = "Package+deleted"
    db.add(AuditLog(actor=settings.admin_username, action=action, target_type="package", target_id=str(package_id), detail=name))
    db.commit()
    return RedirectResponse(f"/packages?notice={notice}", status_code=303)


@app.post("/packages/{package_id}/tiers")
def add_tier(
    request: Request,
    package_id: int,
    name: str = Form(...),
    price: Decimal = Form(...),
    interval_unit: str = Form(...),
    interval_count: int = Form(1),
    grace_period_days: int = Form(3),
    stream_limit: int = Form(1),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    if interval_unit not in {"week", "month", "year"} or interval_count < 1 or grace_period_days < 0 or stream_limit < 0 or price < 0:
        return RedirectResponse("/packages?error=Invalid+billing+tier+values", status_code=303)
    t = BillingTier(package_id=package_id, name=name.strip(), price=price, interval_unit=interval_unit, interval_count=interval_count, grace_period_days=grace_period_days, stream_limit=stream_limit)
    db.add(t)
    db.commit()
    return RedirectResponse("/packages", status_code=303)


@app.post("/packages/{package_id}/tiers/{tier_id}/edit")
def edit_tier(
    request: Request,
    package_id: int,
    tier_id: int,
    name: str = Form(...),
    price: Decimal = Form(...),
    interval_unit: str = Form(...),
    interval_count: int = Form(1),
    grace_period_days: int = Form(3),
    stream_limit: int = Form(1),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    t = db.query(BillingTier).filter(BillingTier.id == tier_id, BillingTier.package_id == package_id).first()
    if not t:
        return RedirectResponse("/packages?error=Billing+tier+not+found", status_code=303)
    if interval_unit not in {"week", "month", "year"} or interval_count < 1 or grace_period_days < 0 or stream_limit < 0 or price < 0:
        return RedirectResponse("/packages?error=Invalid+billing+tier+values", status_code=303)
    t.name = name.strip()
    t.price = price
    t.interval_unit = interval_unit
    t.interval_count = interval_count
    t.grace_period_days = grace_period_days
    t.stream_limit = stream_limit
    db.add(AuditLog(actor=settings.admin_username, action="billing_tier.update", target_type="billing_tier", target_id=str(t.id), detail=f"{t.name}: £{price} / {interval_count} {interval_unit}; {grace_period_days}d grace; stream limit {stream_limit}"))
    db.commit()
    return RedirectResponse("/packages?notice=Billing+tier+updated", status_code=303)


@app.post("/packages/{package_id}/tiers/{tier_id}/delete")
def delete_tier(request: Request, package_id: int, tier_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    t = db.query(BillingTier).options(joinedload(BillingTier.subscriptions)).filter(BillingTier.id == tier_id, BillingTier.package_id == package_id).first()
    if not t:
        return RedirectResponse("/packages?error=Billing+tier+not+found", status_code=303)
    if t.current_subscription_count:
        return RedirectResponse(f"/packages?error=Cannot+delete+billing+tier%3A+it+is+used+by+{t.current_subscription_count}+assigned+subscription%28s%29", status_code=303)
    name = t.name
    if t.subscriptions:
        t.active = False
        action = "billing_tier.archive"
        notice = "Billing+tier+archived%3B+historical+subscriptions+preserved"
    else:
        db.delete(t)
        action = "billing_tier.delete"
        notice = "Billing+tier+deleted"
    db.add(AuditLog(actor=settings.admin_username, action=action, target_type="billing_tier", target_id=str(tier_id), detail=name))
    db.commit()
    return RedirectResponse(f"/packages?notice={notice}", status_code=303)


@app.post("/packages/{package_id}/entitlements")
def set_entitlements(request: Request, package_id: int, integration_id: int = Form(...), library_ids: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    integration = db.get(Integration, integration_id)
    if not integration or integration.kind != "plex":
        return RedirectResponse("/packages", status_code=303)
    client = PlexIntegration(integration.base_url, integration.secret)
    libs = {x["id"]: x for x in client.libraries()}
    db.query(PackageEntitlement).filter(PackageEntitlement.package_id == package_id, PackageEntitlement.integration_id == integration_id).delete()
    for lid in library_ids:
        if lid in libs:
            db.add(PackageEntitlement(package_id=package_id, integration_id=integration_id, resource_type="library", resource_id=lid, resource_name=libs[lid]["name"]))
    db.commit()
    return RedirectResponse("/packages", status_code=303)


NEWS_BANNER_SEVERITIES = ("info", "advisory", "warning", "critical")


def _parse_utc_local(value: str) -> datetime:
    parsed = datetime.fromisoformat((value or "").strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _news_banner_conflict(db: Session, starts_at: datetime, ends_at: datetime, exclude_id: int | None = None) -> NewsBanner | None:
    query = db.query(NewsBanner).filter(
        NewsBanner.cancelled_at.is_(None),
        NewsBanner.starts_at < ends_at,
        NewsBanner.ends_at > starts_at,
    )
    if exclude_id is not None:
        query = query.filter(NewsBanner.id != exclude_id)
    return query.order_by(NewsBanner.starts_at.asc()).first()


@app.get("/news", response_class=HTMLResponse)
def news_banners(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    now = datetime.utcnow()
    active = db.query(NewsBanner).filter(NewsBanner.cancelled_at.is_(None), NewsBanner.starts_at <= now, NewsBanner.ends_at > now).order_by(NewsBanner.starts_at.desc()).first()
    queued = db.query(NewsBanner).filter(NewsBanner.cancelled_at.is_(None), NewsBanner.starts_at > now).order_by(NewsBanner.starts_at.asc()).all()
    recent = db.query(NewsBanner).filter(or_(NewsBanner.cancelled_at.is_not(None), NewsBanner.ends_at <= now)).order_by(NewsBanner.ends_at.desc()).limit(25).all()
    return render(request, "news.html", active_banner=active, queued_banners=queued, recent_banners=recent, severities=NEWS_BANNER_SEVERITIES, error=error, notice=notice)


@app.post("/news")
def create_news_banner(
    request: Request, title: str = Form(...), body: str = Form(...), severity: str = Form("info"),
    starts_at_utc: str = Form(...), ends_at_utc: str = Form(...), db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    clean_title = title.strip()
    clean_body = body.strip()
    clean_severity = severity.strip().lower()
    if not clean_title or not clean_body:
        return RedirectResponse("/news?error=Title+and+body+are+required", status_code=303)
    if len(clean_title) > 160:
        return RedirectResponse("/news?error=Title+must+be+160+characters+or+fewer", status_code=303)
    if clean_severity not in NEWS_BANNER_SEVERITIES:
        return RedirectResponse("/news?error=Invalid+banner+severity", status_code=303)
    try:
        starts_at = datetime.fromisoformat((starts_at_utc or "").strip())
        ends_at = datetime.fromisoformat((ends_at_utc or "").strip())
    except ValueError:
        return RedirectResponse("/news?error=Invalid+start+or+end+time", status_code=303)
    if ends_at <= starts_at:
        return RedirectResponse("/news?error=End+time+must+be+after+start+time", status_code=303)
    conflict = _news_banner_conflict(db, starts_at, ends_at)
    if conflict:
        return RedirectResponse(f"/news?error={quote_plus('Schedule conflicts with '+conflict.title)}", status_code=303)
    row = NewsBanner(title=clean_title, body=clean_body, severity=clean_severity, starts_at=starts_at, ends_at=ends_at, created_by=settings.admin_username)
    db.add(row); db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="news_banner.create", target_type="news_banner", target_id=str(row.id), detail=f"{row.title}; {row.starts_at.isoformat()}Z -> {row.ends_at.isoformat()}Z; severity={row.severity}"))
    db.commit()
    return RedirectResponse("/news?notice=Banner+scheduled", status_code=303)


@app.post("/news/{banner_id}/cancel")
def cancel_news_banner(request: Request, banner_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    row = db.get(NewsBanner, banner_id)
    if not row:
        return RedirectResponse("/news?error=Banner+not+found", status_code=303)
    if row.cancelled_at is None:
        row.cancelled_at = datetime.utcnow()
        db.add(AuditLog(actor=settings.admin_username, action="news_banner.cancel", target_type="news_banner", target_id=str(row.id), detail=row.title))
        db.commit()
    return RedirectResponse("/news?notice=Banner+cancelled", status_code=303)


@app.get("/portal/news", response_class=HTMLResponse)
def portal_news(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return RedirectResponse("/portal/login", status_code=303)
    now = datetime.utcnow()
    active = (db.query(NewsBanner).filter(
        NewsBanner.cancelled_at.is_(None),
        NewsBanner.starts_at <= now,
        NewsBanner.ends_at > now,
    ).order_by(NewsBanner.starts_at.desc()).first())
    upcoming = (db.query(NewsBanner).filter(
        NewsBanner.cancelled_at.is_(None),
        NewsBanner.starts_at > now,
    ).order_by(NewsBanner.starts_at.asc()).all())
    return render(request, "portal_news.html", customer=customer, current_banner=active, upcoming_banners=upcoming)


@app.get("/portal/api/tickets/summary")
def portal_ticket_summary(request: Request, db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    unread_count = (db.query(func.count(SupportTicket.id)).filter(
        SupportTicket.customer_id == customer.id,
        SupportTicket.customer_unread.is_(True),
    ).scalar() or 0)
    return {"unread_count": int(unread_count)}


@app.get("/portal/api/news-banner")
def portal_news_banner_api(request: Request, scope: str = "global", db: Session = Depends(get_db)):
    customer = _portal_customer(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = datetime.utcnow()
    query = db.query(NewsBanner).filter(
        NewsBanner.cancelled_at.is_(None),
        NewsBanner.starts_at <= now,
        NewsBanner.ends_at > now,
    )
    # Lower-severity announcements live on Account; only Critical follows the
    # customer throughout the rest of the portal.
    if scope != "account":
        query = query.filter(NewsBanner.severity == "critical")
    row = query.order_by(NewsBanner.starts_at.desc()).first()
    if not row:
        return {"banner": None}
    return {"banner": {"id": row.id, "title": row.title, "body": row.body, "severity": row.severity, "ends_at": row.ends_at.isoformat() + "Z"}}


@app.get("/integrations", response_class=HTMLResponse)
def integrations(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    rows = db.query(Integration).filter(Integration.kind == "plex").order_by(Integration.name).all()
    enriched = []
    for integration in rows:
        libraries = []
        plex_error = None
        if integration.kind == "plex" and integration.enabled:
            try:
                libraries = PlexIntegration(integration.base_url, integration.secret).libraries()
            except Exception as exc:
                plex_error = str(exc)
        enriched.append((integration, libraries, plex_error))
    notification_endpoints = db.query(NotificationEndpoint).order_by(NotificationEndpoint.name).all()
    for endpoint in notification_endpoints:
        if endpoint.kind == "home_assistant":
            endpoint.ui_url = endpoint.url
        else:
            parts = urlsplit(endpoint.url)
            endpoint.ui_url = f"{parts.scheme}://{parts.netloc}/…" if parts.scheme and parts.netloc else "Configured webhook"
    notification_deliveries = db.query(NotificationDelivery).options(joinedload(NotificationDelivery.endpoint)).order_by(NotificationDelivery.created_at.desc()).limit(50).all()
    admin_push_devices = db.query(PushSubscription).filter(PushSubscription.owner_type == "admin", PushSubscription.enabled.is_(True)).count()
    notification_event_count = db.query(NotificationEvent).count()
    scheduled_broadcasts = db.query(ScheduledCustomerBroadcast).filter(ScheduledCustomerBroadcast.status == "scheduled").order_by(ScheduledCustomerBroadcast.scheduled_for.asc()).limit(10).all()
    return render(
        request,
        "integrations.html",
        integrations=enriched,
        notification_endpoints=notification_endpoints,
        notification_deliveries=notification_deliveries,
        notification_events=EVENT_DEFINITIONS,
        notification_default_due_days=max(0, int(settings.notification_due_soon_days)),
        admin_push_devices=admin_push_devices, notification_event_count=notification_event_count,
        admin_push_status=admin_push_status(db),
        critical_broadcast_audience=critical_broadcast_audience(db),
        scheduled_broadcasts=scheduled_broadcasts,
        requests_platform=db.get(RequestsPlatformSettings, 1),
        tautulli_settings=get_tautulli_settings(db),
        tautulli_backfill=watch_history_backfill_status(db),
        error=error,
        notice=notice,
    )


@app.post("/integrations/plex")
def add_plex(request: Request, name: str = Form(...), base_url: str = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    client = PlexIntegration(base_url, token)
    info = client.test()
    integration = Integration(kind="plex", name=name.strip(), base_url=base_url.strip(), secret=token.strip(), machine_identifier=info["machine_identifier"])
    db.add(integration)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="integration.create", target_type="integration", target_id=str(integration.id), detail=f"Plex: {info['server_name']}"))
    db.commit()
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/{integration_id}/import-users")
def import_plex_users(request: Request, integration_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    integration = db.get(Integration, integration_id)
    client = PlexIntegration(integration.base_url, integration.secret)
    created = 0
    for user in client.users():
        identifier = user["username"] or user["email"]
        if not identifier:
            continue
        existing = db.query(Customer).filter(Customer.plex_username == identifier).first()
        if existing:
            continue
        db.add(Customer(name=user["username"] or user["email"], email=user["email"], plex_username=identifier, plex_user_id=user["id"] or None))
        created += 1
    db.add(AuditLog(actor=settings.admin_username, action="plex.import_users", target_type="integration", target_id=str(integration.id), detail=f"Imported {created} users"))
    db.commit()
    return RedirectResponse("/customers", status_code=303)


@app.post("/integrations/requests-platform")
def save_requests_platform(
    request: Request,
    enabled: str | None = Form(None),
    name: str = Form("Seerr"),
    base_url: str = Form(""),
    button_label: str = Form("Request Content"),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate

    clean_name = name.strip() or "Seerr"
    clean_label = button_label.strip() or "Request Content"
    clean_url = base_url.strip().rstrip("/")
    is_enabled = enabled is not None

    if len(clean_name) > 120 or len(clean_label) > 80:
        return RedirectResponse("/integrations?error=Requests+platform+name+or+button+label+is+too+long", status_code=303)
    if is_enabled and not clean_url:
        return RedirectResponse("/integrations?error=Requests+platform+URL+is+required+when+enabled", status_code=303)
    if clean_url:
        parsed = urlsplit(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return RedirectResponse("/integrations?error=Requests+platform+URL+must+be+a+valid+HTTP+or+HTTPS+URL", status_code=303)

    row = db.get(RequestsPlatformSettings, 1)
    if row is None:
        row = RequestsPlatformSettings(id=1)
        db.add(row)
    row.enabled = is_enabled
    row.name = clean_name
    row.base_url = clean_url or None
    row.button_label = clean_label
    row.updated_at = datetime.utcnow()
    db.add(AuditLog(
        actor=settings.admin_username,
        action="requests_platform.update",
        target_type="requests_platform",
        target_id="1",
        detail=f"{clean_name}: {'enabled' if is_enabled else 'disabled'}",
    ))
    db.commit()
    return RedirectResponse("/integrations?notice=Requests+platform+settings+saved", status_code=303)


@app.post("/integrations/tautulli")
def save_tautulli(
    request: Request,
    base_url: str = Form(...),
    api_key: str = Form(""),
    enabled: str | None = Form(None),
    sync_interval_minutes: int = Form(30),
    live_refresh_seconds: int = Form(10),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    if not 5 <= sync_interval_minutes <= 1440:
        return RedirectResponse("/integrations?error=Tautulli+sync+interval+must+be+between+5+and+1440+minutes", status_code=303)
    if live_refresh_seconds not in {10, 15, 30, 60}:
        return RedirectResponse("/integrations?error=Invalid+live+refresh+interval", status_code=303)
    row = get_tautulli_settings(db)
    integration = row.integration
    clean_url = base_url.strip().rstrip("/")
    clean_key = api_key.strip()
    if integration is None:
        if not clean_key:
            return RedirectResponse("/integrations?error=Tautulli+API+key+is+required", status_code=303)
        integration = Integration(kind="tautulli", name="Tautulli", enabled=True, base_url=clean_url, secret=clean_key)
        db.add(integration)
        db.flush()
        row.integration_id = integration.id
    else:
        integration.base_url = clean_url
        if clean_key:
            integration.secret = clean_key
    integration.enabled = enabled == "on"
    row.sync_interval_minutes = sync_interval_minutes
    row.live_refresh_seconds = live_refresh_seconds
    row.updated_at = datetime.utcnow()
    db.add(AuditLog(actor=settings.admin_username, action="tautulli.settings.updated", target_type="integration", target_id=str(integration.id), detail=f"enabled={integration.enabled}; sync={sync_interval_minutes}m; live={live_refresh_seconds}s"))
    db.commit()
    return RedirectResponse("/integrations?notice=Tautulli+settings+saved", status_code=303)


@app.post("/integrations/tautulli/test")
def test_tautulli(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    row = get_tautulli_settings(db)
    if not row.integration:
        return RedirectResponse("/integrations?error=Configure+Tautulli+first", status_code=303)
    try:
        info = TautulliIntegration(row.integration.base_url or "", row.integration.secret or "").test()
        return RedirectResponse(f"/integrations?notice={quote_plus('Tautulli connected: v' + str(info['version']) + ', ' + str(info['user_count']) + ' users visible')}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/integrations?error={quote_plus('Tautulli test failed: ' + str(exc))}", status_code=303)


@app.post("/integrations/tautulli/sync")
def sync_tautulli_now(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    try:
        result = sync_tautulli(db)
        return RedirectResponse(f"/integrations?notice={quote_plus(f'Tautulli sync complete: {result.matched} matched, {result.unmatched} unmatched')}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/integrations?error={quote_plus('Tautulli sync failed: ' + str(exc))}", status_code=303)


@app.get("/api/tautulli/live")
def tautulli_live(request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    row = get_tautulli_settings(db)
    if not row.integration or not row.integration.enabled:
        return {"enabled": False, "sessions": [], "refresh_seconds": row.live_refresh_seconds}
    live = get_live_activity(db, max_age_seconds=row.live_refresh_seconds)
    sampled = live.get("sampled_at")
    return {
        "enabled": True,
        "sampled_at": sampled.isoformat() + "Z" if sampled else None,
        "sessions": live.get("sessions", []),
        "error": live.get("error"),
        "refresh_seconds": row.live_refresh_seconds,
    }


@app.post("/integrations/tautulli/history/force-resync")
def force_resync_tautulli_history(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    row = get_tautulli_settings(db)
    if not row.integration or not row.integration.enabled:
        return RedirectResponse("/integrations?error=Enable+and+configure+Tautulli+before+forcing+a+history+re-sync", status_code=303)
    try:
        # Background DB workers use this same lock, so a page cannot be committed
        # while the cache/checkpoints are being cleared. The next backfill worker
        # cycle will reseed per-library checkpoints and begin the rebuild.
        with DB_WORK_LOCK:
            result = force_full_watch_history_resync(db)
            db.add(AuditLog(
                actor=settings.admin_username,
                action="tautulli.history.force_resync",
                target_type="integration",
                target_id=str(row.integration_id),
                detail=(
                    f"Cleared {result['deleted_history_rows']} cached history rows and "
                    f"{result['deleted_checkpoints']} backfill checkpoints; "
                    f"reset {result['customers_reset']} customer history states"
                ),
            ))
            db.commit()
        notice = (
            f"Full watch-history re-sync started. Cleared {result['deleted_history_rows']} cached rows; "
            "the asynchronous backfill will repopulate them from Tautulli."
        )
        return RedirectResponse(f"/integrations?notice={quote_plus(notice)}", status_code=303)
    except Exception as exc:
        db.rollback()
        logger.exception("Forced Tautulli watch-history re-sync failed")
        return RedirectResponse(
            f"/integrations?error={quote_plus('Could not start full watch-history re-sync: ' + str(exc))}",
            status_code=303,
        )


@app.get("/api/tautulli/backfill")
def tautulli_backfill_status_api(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return watch_history_backfill_status(db)



@app.post("/notifications/broadcast/critical")
def critical_customer_broadcast(
    request: Request,
    title: str = Form(...),
    message: str = Form(...),
    destination: str = Form("/portal"),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    try:
        event_row, deliveries = send_critical_customer_broadcast(
            db, title=title, message=message, url=destination
        )
    except ValueError as exc:
        return RedirectResponse(f"/integrations?error={quote_plus(str(exc))}#critical-customer-broadcast", status_code=303)

    successful = sum(1 for delivery in deliveries if delivery.success)
    failed = len(deliveries) - successful
    customer_ids = {delivery.recipient_id for delivery in deliveries if delivery.recipient_type == "customer" and delivery.recipient_id}
    db.add(AuditLog(
        actor=settings.admin_username,
        action="notification.critical_broadcast",
        target_type="customer_broadcast",
        target_id=str(event_row.id),
        detail=f"{event_row.title}; customers={len(customer_ids)} devices={len(deliveries)} delivered={successful} failed={failed}",
    ))
    db.commit()
    notice = f"Critical broadcast sent to {successful} device{'s' if successful != 1 else ''}"
    if failed:
        notice += f" ({failed} failed)"
    return RedirectResponse(f"/integrations?notice={quote_plus(notice)}#critical-customer-broadcast", status_code=303)

@app.post("/notifications/broadcast/critical/schedule")
def schedule_critical_broadcast_route(
    request: Request, title: str = Form(...), message: str = Form(...), destination: str = Form("/portal"), scheduled_for_utc: str = Form(...), db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    try:
        scheduled_for = datetime.fromisoformat((scheduled_for_utc or "").strip())
        row = schedule_critical_customer_broadcast(db, title=title, message=message, url=destination, scheduled_for=scheduled_for, created_by=settings.admin_username)
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/integrations?error={quote_plus(str(exc))}#critical-customer-broadcast", status_code=303)
    db.add(AuditLog(actor=settings.admin_username, action="notification.critical_broadcast.scheduled", target_type="scheduled_customer_broadcast", target_id=str(row.id), detail=f"{row.title}; scheduled_for={row.scheduled_for.isoformat()}Z")); db.commit()
    return RedirectResponse(f"/integrations?notice={quote_plus('Critical broadcast scheduled')}#critical-customer-broadcast", status_code=303)


@app.post("/notifications/broadcast/scheduled/{broadcast_id}/cancel")
def cancel_critical_broadcast_route(request: Request, broadcast_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    try:
        row = cancel_scheduled_broadcast(db, broadcast_id)
    except ValueError as exc:
        return RedirectResponse(f"/integrations?error={quote_plus(str(exc))}#critical-customer-broadcast", status_code=303)
    db.add(AuditLog(actor=settings.admin_username, action="notification.critical_broadcast.cancelled", target_type="scheduled_customer_broadcast", target_id=str(row.id), detail=row.title)); db.commit()
    return RedirectResponse("/integrations?notice=Scheduled+broadcast+cancelled#critical-customer-broadcast", status_code=303)


@app.get("/notifications/history", response_class=HTMLResponse)
def notification_history(request: Request, page: int = 1, per_page: int = 25, event: str = "", channel: str = "", result: str = "", db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    page = max(1, int(page or 1)); per_page = int(per_page or 25); per_page = per_page if per_page in {10, 25, 50, 100} else 25
    q = db.query(NotificationDelivery).options(joinedload(NotificationDelivery.endpoint))
    if event:
        q = q.filter(NotificationDelivery.event == event)
    if channel:
        q = q.filter(NotificationDelivery.channel == channel)
    if result == "sent":
        q = q.filter(NotificationDelivery.success.is_(True))
    elif result == "retrying":
        q = q.filter(NotificationDelivery.success.is_(False), NotificationDelivery.next_attempt_at.is_not(None), NotificationDelivery.final_failure.is_(False))
    elif result == "failed":
        q = q.filter(NotificationDelivery.success.is_(False)).filter(or_(NotificationDelivery.final_failure.is_(True), NotificationDelivery.next_attempt_at.is_(None)))
    total = q.count(); pages = max(1, (total + per_page - 1) // per_page); page = min(page, pages)
    deliveries = q.order_by(NotificationDelivery.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    channels = [r[0] for r in db.query(NotificationDelivery.channel).distinct().order_by(NotificationDelivery.channel).all() if r[0]]
    schedules = db.query(ScheduledCustomerBroadcast).order_by(ScheduledCustomerBroadcast.created_at.desc()).limit(50).all()
    return render(request, "notification_history.html", title="Notification History", deliveries=deliveries, total=total, page=page, pages=pages, per_page=per_page, event_filter=event, channel_filter=channel, result_filter=result, notification_events=EVENT_DEFINITIONS, channels=channels, scheduled_broadcasts=schedules)


@app.post("/notifications")
def add_notification_endpoint(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(""),
    target: str = Form(""),
    events: list[str] = Form(default=[]),
    min_severity: str = Form("info"),
    due_reminder_days: str = Form("3"),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    if kind not in {"home_assistant", "discord", "webhook"}:
        return RedirectResponse("/integrations?error=Unsupported+notification+type", status_code=303)
    if min_severity not in {"info", "warning", "critical"}:
        return RedirectResponse("/integrations?error=Invalid+notification+severity", status_code=303)
    try:
        clean_due_days = format_due_reminder_days(due_reminder_days, settings.notification_due_soon_days)
    except ValueError as exc:
        return RedirectResponse(f"/integrations?error={quote_plus(str(exc))}", status_code=303)
    clean_name = name.strip()
    clean_url = url.strip().rstrip("/")
    if not clean_name or not clean_url:
        return RedirectResponse("/integrations?error=Name+and+URL+are+required", status_code=303)
    if kind == "home_assistant" and not secret.strip():
        return RedirectResponse("/integrations?error=Home+Assistant+requires+a+long-lived+access+token", status_code=303)
    if db.query(NotificationEndpoint).filter(func.lower(NotificationEndpoint.name) == clean_name.lower()).first():
        return RedirectResponse("/integrations?error=A+notification+integration+with+that+name+already+exists", status_code=303)
    selected = [event for event in events if event in EVENT_DEFINITIONS]
    endpoint = NotificationEndpoint(
        kind=kind, name=clean_name, url=clean_url, secret=secret.strip() or None,
        target=target.strip() or None, events=",".join(selected), min_severity=min_severity,
        due_reminder_days=clean_due_days, enabled=True,
    )
    db.add(endpoint)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="notification.create", target_type="notification_endpoint", target_id=str(endpoint.id), detail=f"{kind}: {clean_name}"))
    db.commit()
    return RedirectResponse("/integrations?notice=Notification+integration+added", status_code=303)


@app.post("/notifications/{endpoint_id}/edit")
def edit_notification_endpoint(
    request: Request,
    endpoint_id: int,
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(""),
    target: str = Form(""),
    events: list[str] = Form(default=[]),
    min_severity: str = Form("info"),
    due_reminder_days: str = Form("3"),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    endpoint = db.get(NotificationEndpoint, endpoint_id)
    if not endpoint:
        return RedirectResponse("/integrations?error=Notification+integration+not+found", status_code=303)
    if min_severity not in {"info", "warning", "critical"}:
        return RedirectResponse("/integrations?error=Invalid+notification+severity", status_code=303)
    try:
        clean_due_days = format_due_reminder_days(due_reminder_days, settings.notification_due_soon_days)
    except ValueError as exc:
        return RedirectResponse(f"/integrations?error={quote_plus(str(exc))}", status_code=303)
    clean_name = name.strip()
    clean_url = url.strip().rstrip("/")
    if not clean_name or not clean_url:
        return RedirectResponse("/integrations?error=Name+and+URL+are+required", status_code=303)
    duplicate = db.query(NotificationEndpoint).filter(func.lower(NotificationEndpoint.name) == clean_name.lower(), NotificationEndpoint.id != endpoint.id).first()
    if duplicate:
        return RedirectResponse("/integrations?error=A+notification+integration+with+that+name+already+exists", status_code=303)
    selected = [event for event in events if event in EVENT_DEFINITIONS]
    endpoint.name = clean_name
    endpoint.url = clean_url
    if endpoint.kind == "home_assistant" and secret.strip():
        endpoint.secret = secret.strip()
    endpoint.target = target.strip() or None
    endpoint.events = ",".join(selected)
    endpoint.min_severity = min_severity
    endpoint.due_reminder_days = clean_due_days
    db.add(AuditLog(actor=settings.admin_username, action="notification.update", target_type="notification_endpoint", target_id=str(endpoint.id), detail=endpoint.name))
    db.commit()
    return RedirectResponse("/integrations?notice=Notification+integration+updated", status_code=303)


@app.post("/notifications/{endpoint_id}/test")
def test_notification_endpoint(request: Request, endpoint_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    endpoint = db.get(NotificationEndpoint, endpoint_id)
    if not endpoint:
        return RedirectResponse("/integrations?error=Notification+integration+not+found", status_code=303)
    delivery = send_test(db, endpoint)
    if delivery.success:
        return RedirectResponse("/integrations?notice=Test+notification+sent", status_code=303)
    return RedirectResponse(f"/integrations?error={quote_plus('Test failed: ' + (delivery.detail or 'unknown error'))}", status_code=303)


@app.post("/notifications/{endpoint_id}/toggle")
def toggle_notification_endpoint(request: Request, endpoint_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    endpoint = db.get(NotificationEndpoint, endpoint_id)
    if not endpoint:
        return RedirectResponse("/integrations?error=Notification+integration+not+found", status_code=303)
    endpoint.enabled = not endpoint.enabled
    db.add(AuditLog(actor=settings.admin_username, action="notification.toggle", target_type="notification_endpoint", target_id=str(endpoint.id), detail=f"{endpoint.name}: {'enabled' if endpoint.enabled else 'disabled'}"))
    db.commit()
    return RedirectResponse("/integrations?notice=Notification+integration+updated", status_code=303)


@app.post("/notifications/{endpoint_id}/delete")
def delete_notification_endpoint(request: Request, endpoint_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    endpoint = db.get(NotificationEndpoint, endpoint_id)
    if not endpoint:
        return RedirectResponse("/integrations?error=Notification+integration+not+found", status_code=303)
    name = endpoint.name
    db.delete(endpoint)
    db.add(AuditLog(actor=settings.admin_username, action="notification.delete", target_type="notification_endpoint", target_id=str(endpoint_id), detail=name))
    db.commit()
    return RedirectResponse("/integrations?notice=Notification+integration+deleted", status_code=303)


@app.get("/payments", response_class=HTMLResponse)
def payments(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    rows = db.query(Payment).options(joinedload(Payment.customer), joinedload(Payment.subscription).joinedload(Subscription.billing_tier)).order_by(Payment.paid_at.desc(), Payment.id.desc()).limit(250).all()
    customers = db.query(Customer).options(joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).filter(Customer.archived.is_(False)).order_by(Customer.name).all()
    payment_sources = db.query(PaymentSource).order_by(PaymentSource.active.desc(), PaymentSource.name.asc()).all()
    return render(request, "payments.html", payments=rows, customers=customers, payment_sources=payment_sources, error=error, notice=notice, today=datetime.utcnow().strftime("%Y-%m-%d"))


@app.post("/payments/sources")
def add_payment_source(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    clean = name.strip()
    if not clean:
        return RedirectResponse("/payments?error=Payment+source+name+cannot+be+blank", status_code=303)
    existing = db.query(PaymentSource).filter(func.lower(PaymentSource.name) == clean.lower()).first()
    if existing:
        if not existing.active:
            existing.active = True
            db.add(AuditLog(actor=settings.admin_username, action="payment_source.restore", target_type="payment_source", target_id=str(existing.id), detail=existing.name))
            db.commit()
            return RedirectResponse("/payments?notice=Payment+source+restored", status_code=303)
        return RedirectResponse("/payments?error=Payment+source+already+exists", status_code=303)
    source = PaymentSource(name=clean)
    db.add(source)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="payment_source.create", target_type="payment_source", target_id=str(source.id), detail=source.name))
    db.commit()
    return RedirectResponse("/payments?notice=Payment+source+added", status_code=303)


@app.post("/payments/sources/{source_id}/rename")
def rename_payment_source(request: Request, source_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    source = db.get(PaymentSource, source_id)
    if not source:
        return RedirectResponse("/payments?error=Payment+source+not+found", status_code=303)
    clean = name.strip()
    if not clean:
        return RedirectResponse("/payments?error=Payment+source+name+cannot+be+blank", status_code=303)
    duplicate = db.query(PaymentSource).filter(func.lower(PaymentSource.name) == clean.lower(), PaymentSource.id != source.id).first()
    if duplicate:
        return RedirectResponse("/payments?error=Another+payment+source+already+uses+that+name", status_code=303)
    old = source.name
    source.name = clean
    db.add(AuditLog(actor=settings.admin_username, action="payment_source.rename", target_type="payment_source", target_id=str(source.id), detail=f"{old} -> {clean}"))
    db.commit()
    return RedirectResponse("/payments?notice=Payment+source+renamed", status_code=303)


@app.post("/payments/sources/{source_id}/toggle")
def toggle_payment_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    source = db.get(PaymentSource, source_id)
    if not source:
        return RedirectResponse("/payments?error=Payment+source+not+found", status_code=303)
    source.active = not source.active
    db.add(AuditLog(actor=settings.admin_username, action="payment_source.toggle", target_type="payment_source", target_id=str(source.id), detail=f"{source.name}: {'active' if source.active else 'archived'}"))
    db.commit()
    return RedirectResponse("/payments?notice=Payment+source+updated", status_code=303)


@app.post("/payments")
def add_payment(
    request: Request,
    customer_id: int = Form(...),
    amount: Decimal = Form(...),
    paid_at: str = Form(...),
    source: str = Form("manual"),
    external_reference: str = Form(""),
    note: str = Form(""),
    apply_to_subscription: bool = Form(False),
    billing_periods: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/payments?error=Customer+not+found", status_code=303)
    if customer.archived:
        return RedirectResponse("/payments?error=Archived+customers+must+be+restored+before+recording+new+payments", status_code=303)
    selected_source = db.query(PaymentSource).filter(PaymentSource.name == source, PaymentSource.active.is_(True)).first()
    if not selected_source:
        return RedirectResponse("/payments?error=Payment+source+is+not+available", status_code=303)
    paid = _parse_date(paid_at)
    previous_status = customer.status
    try:
        periods_override = int(billing_periods) if billing_periods.strip() else None
        payment = apply_payment(
            db,
            customer=customer,
            amount=amount,
            paid_at=paid,
            source=source,
            external_reference=external_reference.strip() or None,
            note=note.strip() or None,
            apply_to_subscription=apply_to_subscription,
            billing_periods=periods_override,
        )
    except (ValueError, TypeError) as exc:
        db.rollback()
        return RedirectResponse(f"/payments?error={quote_plus(str(exc))}", status_code=303)
    detail = f"£{amount} via {source} on {paid:%Y-%m-%d}"
    if payment.subscription_id:
        detail += f"; {payment.billing_periods or 1} billing period(s); coverage {payment.coverage_start:%Y-%m-%d} -> {payment.coverage_end:%Y-%m-%d}"
    db.add(AuditLog(actor=settings.admin_username, action="payment.record", target_type="payment", target_id=str(payment.id), detail=detail))
    db.commit()
    notify_event(
        db, event="payment.received", title="Payment received",
        message=f"{customer.name}: £{amount:.2f} via {source}.",
        target_type="payment", target_id=str(payment.id),
        data={"customer": customer.name, "amount": f"{amount:.2f}", "source": source},
    )
    if previous_status == "suspended" and customer.status == "active":
        notify_event(db, event="customer.reactivated", title="Customer reactivated", message=f"{customer.name} is active again after payment.", target_type="customer", target_id=str(customer.id), data={"customer": customer.name})

    if payment.subscription_id and settings.reconcile_on_assign and customer.plex_username and not customer.exempt:
        try:
            reconcile_customer(db, customer)
        except Exception as exc:
            _notify_reconcile_failure(db, customer, exc)

    if apply_to_subscription and not payment.subscription_id:
        return RedirectResponse("/payments?notice=Payment+recorded+as+ledger+only%3B+customer+has+no+assigned+subscription", status_code=303)
    return RedirectResponse("/payments?notice=Payment+recorded", status_code=303)


@app.post("/payments/{payment_id}/edit")
def edit_payment(
    request: Request,
    payment_id: int,
    amount: Decimal = Form(...),
    paid_at: str = Form(...),
    source: str = Form(...),
    external_reference: str = Form(""),
    note: str = Form(""),
    billing_periods: str = Form(""),
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    payment = db.query(Payment).options(joinedload(Payment.subscription).joinedload(Subscription.billing_tier)).filter(Payment.id == payment_id).first()
    if not payment or payment.voided_at:
        return RedirectResponse("/payments?error=Payment+not+found", status_code=303)
    selected_source = db.query(PaymentSource).filter(PaymentSource.name == source).first()
    if not selected_source:
        return RedirectResponse("/payments?error=Payment+source+not+found", status_code=303)
    try:
        new_periods = int(billing_periods) if billing_periods.strip() else payment.billing_periods
        if new_periods is not None and new_periods < 1:
            raise ValueError("Billing periods must be at least 1")
        coverage_change = bool(payment.subscription_id and payment.coverage_end and new_periods != payment.billing_periods)
        if coverage_change and not payment_is_latest_coverage_event(db, payment):
            raise ValueError("Coverage periods can only be changed on the latest applied payment for this subscription")
        old = f"£{payment.amount} {payment.source} {payment.paid_at:%Y-%m-%d}; periods={payment.billing_periods or '-'}"
        payment.amount = amount
        payment.paid_at = _parse_date(paid_at)
        payment.source = source
        payment.external_reference = external_reference.strip() or None
        payment.note = note.strip() or None
        payment.billing_periods = new_periods
        if coverage_change:
            recalculate_after_latest_payment_change(db, payment)
        db.add(AuditLog(actor=settings.admin_username, action="payment.edit", target_type="payment", target_id=str(payment.id), detail=f"{old} -> £{amount} {source} {payment.paid_at:%Y-%m-%d}; periods={payment.billing_periods or '-'}"))
        db.commit()
    except (ValueError, TypeError) as exc:
        db.rollback()
        return RedirectResponse(f"/payments?error={quote_plus(str(exc))}", status_code=303)
    if coverage_change:
        process_billing(db, customer_id=payment.customer_id)
    if coverage_change and payment.customer.plex_username and not payment.customer.exempt:
        try:
            reconcile_customer(db, payment.customer)
        except Exception as exc:
            _notify_reconcile_failure(db, payment.customer, exc)
    return RedirectResponse("/payments?notice=Payment+updated", status_code=303)


@app.post("/payments/{payment_id}/delete")
def delete_payment(request: Request, payment_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    payment = db.query(Payment).options(joinedload(Payment.subscription).joinedload(Subscription.billing_tier), joinedload(Payment.customer)).filter(Payment.id == payment_id).first()
    if not payment or payment.voided_at:
        return RedirectResponse("/payments?error=Payment+not+found", status_code=303)
    if payment.subscription_id and payment.coverage_end and not payment_is_latest_coverage_event(db, payment):
        return RedirectResponse("/payments?error=Applied+payments+can+only+be+deleted+when+they+are+the+latest+coverage+event", status_code=303)
    if payment.subscription_id and payment.coverage_end:
        try:
            rollback_voided_latest_payment(db, payment)
        except ValueError as exc:
            db.rollback()
            return RedirectResponse(f"/payments?error={quote_plus(str(exc))}", status_code=303)
    payment.voided_at = datetime.utcnow()
    payment.voided_by = settings.admin_username
    db.add(AuditLog(actor=settings.admin_username, action="payment.delete", target_type="payment", target_id=str(payment.id), detail=f"Voided £{payment.amount} via {payment.source} received {payment.paid_at:%Y-%m-%d}"))
    db.commit()
    if payment.subscription_id:
        process_billing(db, customer_id=payment.customer_id)
    if payment.subscription_id and payment.customer.plex_username and not payment.customer.exempt:
        try:
            reconcile_customer(db, payment.customer)
        except Exception as exc:
            _notify_reconcile_failure(db, payment.customer, exc)
    return RedirectResponse("/payments?notice=Payment+deleted", status_code=303)



@app.get("/stream-limits", response_class=HTMLResponse)
def stream_limits_page(request: Request, customer_id: str = "", result: str = "all", date_from: str = "", date_to: str = "", db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate

    selected_customer_id = None
    if customer_id:
        try:
            selected_customer_id = int(customer_id)
        except (TypeError, ValueError):
            return RedirectResponse("/stream-limits", status_code=303)

    query = db.query(StreamLimitEvent).options(joinedload(StreamLimitEvent.customer), joinedload(StreamLimitEvent.billing_tier))
    if selected_customer_id is not None:
        query = query.filter(StreamLimitEvent.customer_id == selected_customer_id)
    if result == "success":
        query = query.filter(StreamLimitEvent.success.is_(True))
    elif result == "failed":
        query = query.filter(StreamLimitEvent.success.is_(False))
    try:
        if date_from:
            query = query.filter(StreamLimitEvent.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        if date_to:
            query = query.filter(StreamLimitEvent.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        return RedirectResponse("/stream-limits", status_code=303)
    rows = query.order_by(StreamLimitEvent.created_at.desc()).limit(500).all()
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_rows = db.query(StreamLimitEvent).filter(StreamLimitEvent.created_at >= month_start).all()
    counts: dict[int, int] = {}
    for row in month_rows:
        counts[row.customer_id] = counts.get(row.customer_id, 0) + 1
    top_customer = db.get(Customer, max(counts, key=counts.get)) if counts else None
    stats = {
        "month": len(month_rows),
        "unique": len(counts),
        "failed": sum(1 for row in month_rows if not row.success),
        "top_customer": top_customer,
        "top_count": counts.get(top_customer.id, 0) if top_customer else 0,
    }
    customers = db.query(Customer).filter(Customer.archived.is_(False)).order_by(Customer.name).all()
    return render(request, "stream_limits.html", rows=rows, stats=stats, customers=customers, selected_customer_id=selected_customer_id, selected_result=result, date_from=date_from, date_to=date_to)

@app.get("/customers/{customer_id}/history", response_class=HTMLResponse)
def customer_history(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    customer = db.query(Customer).options(
        joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier).joinedload(BillingTier.package),
        joinedload(Customer.payments),
        joinedload(Customer.credits).joinedload(SubscriptionCredit.subscription).joinedload(Subscription.billing_tier),
    ).filter(Customer.id == customer_id).first()
    if not customer:
        return RedirectResponse("/customers?error=Customer+not+found", status_code=303)
    events = []
    for payment in customer.payments:
        events.append({"when": payment.paid_at, "kind": "Payment" if not payment.voided_at else "Payment (deleted)", "detail": f"£{payment.amount:.2f} via {payment.source}" + (f" · {payment.billing_periods} period(s) · {payment.coverage_start:%Y-%m-%d} → {payment.coverage_end:%Y-%m-%d}" if payment.coverage_end else " · ledger only") + (f" · {payment.note}" if payment.note else ""), "voided": bool(payment.voided_at)})
    for credit in customer.credits:
        events.append({"when": credit.granted_at, "kind": "Complimentary access", "detail": f"+{credit.billing_periods} period(s) · {credit.coverage_start:%Y-%m-%d} → {credit.coverage_end:%Y-%m-%d}" + (f" · {credit.reason}" if credit.reason else ""), "voided": False})
    for sub in customer.subscriptions:
        events.append({"when": sub.started_at, "kind": "Subscription", "detail": f"{sub.billing_tier.package.name} / {sub.billing_tier.name} · {sub.status}", "voided": False})
    subscription_ids = [str(sub.id) for sub in customer.subscriptions]
    payment_ids = [str(payment.id) for payment in customer.payments]
    log_filters = [
        (AuditLog.target_type == "customer") & (AuditLog.target_id == str(customer.id)),
    ]
    if subscription_ids:
        log_filters.append((AuditLog.target_type == "subscription") & AuditLog.target_id.in_(subscription_ids))
    if payment_ids:
        log_filters.append((AuditLog.target_type == "payment") & AuditLog.target_id.in_(payment_ids))
    logs = db.query(AuditLog).filter(or_(*log_filters)).all()
    for log in logs:
        events.append({"when": log.created_at, "kind": log.action, "detail": log.detail or "", "voided": False})
    events.sort(key=lambda item: item["when"], reverse=True)
    tautulli_activity = db.query(TautulliActivity).filter(TautulliActivity.customer_id == customer.id).first()
    stream_limit_events = (db.query(StreamLimitEvent).options(joinedload(StreamLimitEvent.billing_tier)).filter(StreamLimitEvent.customer_id == customer.id).order_by(StreamLimitEvent.created_at.desc()).limit(100).all())
    return render(request, "customer_history.html", customer=customer, events=events, tautulli_activity=tautulli_activity, stream_limit_events=stream_limit_events)


@app.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request, error: str | None = None, notice: str | None = None):
    gate = auth(request)
    if gate:
        return gate
    storage_error = None
    try:
        backups = list_backups(settings.backup_dir)
    except BackupStorageError as exc:
        backups = []
        storage_error = str(exc)
        error = error or storage_error
    latest = backups[0] if backups else None
    db = SessionLocal()
    try:
        policy = get_backup_policy(db, settings)
    finally:
        db.close()
    return render(
        request,
        "backups.html",
        error=error,
        notice=notice,
        backups=backups,
        latest=latest,
        backup_dir=settings.backup_dir,
        backup_enabled=policy.enabled,
        schedule_hour=policy.schedule_hour,
        check_interval_minutes=policy.check_interval_minutes,
        retention_daily=policy.retention_daily,
        retention_weekly=policy.retention_weekly,
        retention_monthly=policy.retention_monthly,
        storage_error=storage_error,
    )


@app.post("/backups/settings")
def update_backup_settings(
    request: Request,
    enabled: str | None = Form(None),
    schedule_hour: int = Form(...),
    check_interval_minutes: int = Form(...),
    retention_daily: int = Form(...),
    retention_weekly: int = Form(...),
    retention_monthly: int = Form(...),
):
    gate = auth(request)
    if gate:
        return gate
    if not 0 <= schedule_hour <= 23:
        return RedirectResponse("/backups?error=Schedule+hour+must+be+between+0+and+23", status_code=303)
    if not 1 <= check_interval_minutes <= 60:
        return RedirectResponse("/backups?error=Check+interval+must+be+between+1+and+60+minutes", status_code=303)
    if not 1 <= retention_daily <= 365:
        return RedirectResponse("/backups?error=Daily+retention+must+be+between+1+and+365", status_code=303)
    if not 0 <= retention_weekly <= 104 or not 0 <= retention_monthly <= 120:
        return RedirectResponse("/backups?error=Weekly+or+monthly+retention+is+outside+the+allowed+range", status_code=303)

    db = SessionLocal()
    try:
        row = db.get(BackupSettings, 1)
        if row is None:
            row = BackupSettings(id=1)
            db.add(row)
        row.enabled = enabled == "on"
        row.schedule_hour = schedule_hour
        row.check_interval_minutes = check_interval_minutes
        row.retention_daily = retention_daily
        row.retention_weekly = retention_weekly
        row.retention_monthly = retention_monthly
        row.updated_at = datetime.utcnow()
        db.add(AuditLog(
            actor=settings.admin_username,
            action="backup.settings.updated",
            target_type="backup_settings",
            target_id="1",
            detail=f"enabled={row.enabled}; hour={schedule_hour}; check={check_interval_minutes}m; retention={retention_daily}/{retention_weekly}/{retention_monthly}",
        ))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/backups?notice=Backup+automation+settings+saved", status_code=303)


@app.post("/backups/run")
def create_manual_backup(request: Request):
    gate = auth(request)
    if gate:
        return gate
    db = SessionLocal()
    try:
        try:
            backup = create_backup(settings.database_url, settings.backup_dir, automatic=False)
            db.add(AuditLog(actor=settings.admin_username, action="backup.manual", target_type="backup", target_id=backup.name, detail=f"{backup.size} bytes"))
            db.commit()
            notify_event(db, event="backup.created", title="Database backup created", message=f"{backup.name} was created successfully.", target_type="backup", target_id=backup.name, data={"filename": backup.name, "size": backup.size, "automatic": False})
            return RedirectResponse("/backups?notice=Backup+created+successfully", status_code=303)
        except Exception as exc:
            db.rollback()
            notify_event(db, event="backup.failed", title="Database backup failed", message="Share Manager could not create a manual database backup.", severity="critical", target_type="backup", data={"error_type": type(exc).__name__})
            return RedirectResponse(f"/backups?error={quote_plus('Backup failed: ' + str(exc))}", status_code=303)
    finally:
        db.close()


@app.get("/backups/database")
def download_database_backup(request: Request):
    """Backwards-compatible one-click download: create a temp dump and return it."""
    gate = auth(request)
    if gate:
        return gate
    url = make_url(settings.database_url)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fd, path = tempfile.mkstemp(prefix="share-manager-", suffix=".dump")
    os.close(fd)
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--host", url.host or "localhost", "--port", str(url.port or 5432), "--username", url.username or "postgres", "--file", path, url.database or "postgres"]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        return RedirectResponse(f"/backups?error={quote_plus('Backup failed: ' + (exc.stderr or str(exc)))}", status_code=303)
    return FileResponse(path, filename=f"share-manager-{stamp}.dump", media_type="application/octet-stream", background=BackgroundTask(lambda: os.path.exists(path) and os.unlink(path)))


@app.get("/backups/files/{filename}")
def download_stored_backup(request: Request, filename: str):
    gate = auth(request)
    if gate:
        return gate
    try:
        path = safe_backup_path(settings.backup_dir, filename)
    except ValueError:
        return RedirectResponse("/backups?error=Backup+not+found", status_code=303)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/backups/files/{filename}/restore")
def restore_stored_backup(request: Request, filename: str, confirmation: str = Form("")):
    gate = auth(request)
    if gate:
        return gate
    if confirmation.strip().upper() != "RESTORE":
        return RedirectResponse("/backups?error=Type+RESTORE+to+confirm", status_code=303)
    if RESTORE_IN_PROGRESS.is_set():
        return RedirectResponse("/backups?error=Another+restore+is+already+in+progress", status_code=303)
    RESTORE_IN_PROGRESS.set()
    DB_WORK_LOCK.acquire()
    try:
        path = safe_backup_path(settings.backup_dir, filename)
        ok, detail = validate_backup(settings.database_url, path)
        if not ok:
            return RedirectResponse(f"/backups?error={quote_plus('Restore validation failed: ' + detail)}", status_code=303)
        safety = create_backup(settings.database_url, settings.backup_dir, automatic=False)
        engine.dispose()
        restore_backup(settings.database_url, path)
        engine.dispose()
        schema_ok, schema_detail = validate_application_schema(settings.database_url)
        if not schema_ok:
            raise RuntimeError(schema_detail)
        db = SessionLocal()
        try:
            db.add(AuditLog(actor=settings.admin_username, action="backup.restore", target_type="backup", target_id=filename, detail=f"Safety backup: {safety.name}"))
            db.commit()
            notify_event(db, event="backup.restored", title="Database restore completed", message=f"Share Manager restored {filename}. Safety backup: {safety.name}.", severity="warning", target_type="backup", target_id=filename, data={"filename": filename, "safety_backup": safety.name})
        finally:
            db.close()
        return RedirectResponse(f"/backups?notice={quote_plus('Restore completed. Safety backup: ' + safety.name + '. Restart the app container now.')}", status_code=303)
    except Exception as exc:
        engine.dispose()
        return RedirectResponse(f"/backups?error={quote_plus('Restore failed: ' + str(exc))}", status_code=303)
    finally:
        DB_WORK_LOCK.release()
        RESTORE_IN_PROGRESS.clear()


@app.post("/backups/upload-restore")
async def restore_uploaded_backup(request: Request, backup_file: UploadFile = File(...), confirmation: str = Form("")):
    gate = auth(request)
    if gate:
        return gate
    if confirmation.strip().upper() != "RESTORE":
        return RedirectResponse("/backups?error=Type+RESTORE+to+confirm", status_code=303)
    suffix = Path(backup_file.filename or "").suffix.lower()
    if suffix != ".dump":
        return RedirectResponse("/backups?error=Upload+a+PostgreSQL+.dump+backup", status_code=303)
    if RESTORE_IN_PROGRESS.is_set():
        return RedirectResponse("/backups?error=Another+restore+is+already+in+progress", status_code=303)
    RESTORE_IN_PROGRESS.set()
    DB_WORK_LOCK.acquire()
    fd, temp_name = tempfile.mkstemp(prefix="share-manager-restore-", suffix=suffix)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("wb") as handle:
            while chunk := await backup_file.read(1024 * 1024):
                handle.write(chunk)
        ok, detail = validate_backup(settings.database_url, temp_path)
        if not ok:
            return RedirectResponse(f"/backups?error={quote_plus('Restore validation failed: ' + detail)}", status_code=303)
        safety = create_backup(settings.database_url, settings.backup_dir, automatic=False)
        engine.dispose()
        restore_backup(settings.database_url, temp_path)
        engine.dispose()
        schema_ok, schema_detail = validate_application_schema(settings.database_url)
        if not schema_ok:
            raise RuntimeError(schema_detail)
        db = SessionLocal()
        try:
            db.add(AuditLog(actor=settings.admin_username, action="backup.restore.upload", target_type="backup", target_id=backup_file.filename, detail=f"Safety backup: {safety.name}"))
            db.commit()
            notify_event(db, event="backup.restored", title="Database restore completed", message=f"Share Manager restored uploaded backup {backup_file.filename}. Safety backup: {safety.name}.", severity="warning", target_type="backup", target_id=backup_file.filename, data={"filename": backup_file.filename, "safety_backup": safety.name})
        finally:
            db.close()
        return RedirectResponse(f"/backups?notice={quote_plus('Restore completed. Safety backup: ' + safety.name + '. Restart the app container now.')}", status_code=303)
    except Exception as exc:
        engine.dispose()
        return RedirectResponse(f"/backups?error={quote_plus('Restore failed: ' + str(exc))}", status_code=303)
    finally:
        temp_path.unlink(missing_ok=True)
        DB_WORK_LOCK.release()
        RESTORE_IN_PROGRESS.clear()


@app.get("/api/integrations/{integration_id}/libraries")
def api_libraries(request: Request, integration_id: int, db: Session = Depends(get_db)):
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    integration = db.get(Integration, integration_id)
    return PlexIntegration(integration.base_url, integration.secret).libraries()
