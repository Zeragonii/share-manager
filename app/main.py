import asyncio
import logging
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from urllib.parse import quote_plus, urlsplit

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
    TautulliActivity,
    TautulliSettings,
)
from .integrations.plex import PlexIntegration
from .integrations.tautulli import TautulliIntegration, TautulliError
from .security import logged_in, make_session, valid_credentials
from .services.billing import apply_payment, apply_subscription_credit, desired_billing_status, initialize_subscription_period, process_billing
from .services.reconcile import reconcile_customer, retry_pending_reconciliations
from .services.payment_maintenance import payment_is_latest_coverage_event, recalculate_after_latest_payment_change, rollback_voided_latest_payment
from .services.notifications import EVENT_DEFINITIONS, format_due_reminder_days, notify_due_reminders, notify_event, send_test
from .services.backups import create_backup, list_backups, apply_retention, scheduled_backup_due, safe_backup_path, validate_backup, restore_backup, get_backup_policy, validate_application_schema, BackupStorageError
from .services.tautulli import get_tautulli_settings, sync_tautulli, sync_due, get_live_activity, dashboard_usage
from .version import APP_VERSION



logger = logging.getLogger("share-manager")

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

    async def tautulli_loop():
        await asyncio.sleep(15)
        while True:
            try:
                await asyncio.to_thread(run_tautulli_cycle)
            except Exception:
                logger.exception("Tautulli worker cycle failed")
            await asyncio.sleep(60)

    billing_task = asyncio.create_task(billing_loop())
    backup_task = asyncio.create_task(backup_loop())
    tautulli_task = asyncio.create_task(tautulli_loop())
    try:
        yield
    finally:
        billing_task.cancel()
        backup_task.cancel()
        tautulli_task.cancel()
        with suppress(asyncio.CancelledError):
            await billing_task
        with suppress(asyncio.CancelledError):
            await backup_task
        with suppress(asyncio.CancelledError):
            await tautulli_task


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
    return templates.TemplateResponse(request=request, name=name, context={"request": request, "app_version": APP_VERSION, **ctx})


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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    due_soon = db.query(Subscription).filter(
        Subscription.status == "active",
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
        "customers": db.query(Customer).count(),
        "active": db.query(Customer).filter(Customer.status == "active").count(),
        "grace": db.query(Customer).filter(Customer.status == "grace", Customer.exempt == False).count(),  # noqa: E712
        "suspended": db.query(Customer).filter(Customer.status == "suspended", Customer.exempt == False).count(),  # noqa: E712
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
def customers(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    rows = db.query(Customer).options(
        joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier).joinedload(BillingTier.package),
        joinedload(Customer.credits),
    ).order_by(Customer.name).all()
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
    return render(request, "customers.html", customers=rows, tiers=tiers, plex_ready_tier_ids=plex_ready_tier_ids, error=error, notice=notice, today=datetime.utcnow().strftime("%Y-%m-%d"), today_dt=datetime.utcnow())


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
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    if interval_unit not in {"week", "month", "year"} or interval_count < 1 or grace_period_days < 0 or price < 0:
        return RedirectResponse("/packages?error=Invalid+billing+tier+values", status_code=303)
    t = BillingTier(package_id=package_id, name=name.strip(), price=price, interval_unit=interval_unit, interval_count=interval_count, grace_period_days=grace_period_days)
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
    db: Session = Depends(get_db),
):
    gate = auth(request)
    if gate:
        return gate
    t = db.query(BillingTier).filter(BillingTier.id == tier_id, BillingTier.package_id == package_id).first()
    if not t:
        return RedirectResponse("/packages?error=Billing+tier+not+found", status_code=303)
    if interval_unit not in {"week", "month", "year"} or interval_count < 1 or grace_period_days < 0 or price < 0:
        return RedirectResponse("/packages?error=Invalid+billing+tier+values", status_code=303)
    t.name = name.strip()
    t.price = price
    t.interval_unit = interval_unit
    t.interval_count = interval_count
    t.grace_period_days = grace_period_days
    db.add(AuditLog(actor=settings.admin_username, action="billing_tier.update", target_type="billing_tier", target_id=str(t.id), detail=f"{t.name}: £{price} / {interval_count} {interval_unit}; {grace_period_days}d grace"))
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
    notification_deliveries = db.query(NotificationDelivery).options(joinedload(NotificationDelivery.endpoint)).order_by(NotificationDelivery.created_at.desc()).limit(30).all()
    return render(
        request,
        "integrations.html",
        integrations=enriched,
        notification_endpoints=notification_endpoints,
        notification_deliveries=notification_deliveries,
        notification_events=EVENT_DEFINITIONS,
        notification_default_due_days=max(0, int(settings.notification_due_soon_days)),
        tautulli_settings=get_tautulli_settings(db),
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
    customers = db.query(Customer).options(joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier).joinedload(BillingTier.package)).order_by(Customer.name).all()
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
    return render(request, "customer_history.html", customer=customer, events=events, tautulli_activity=tautulli_activity)


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
    if url.get_backend_name() == "sqlite":
        db_path = url.database
        if not db_path or not os.path.exists(db_path):
            return RedirectResponse("/backups?error=SQLite+database+file+not+found", status_code=303)
        return FileResponse(db_path, filename=f"share-manager-{stamp}.sqlite", media_type="application/octet-stream")
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
    if RESTORE_IN_PROGRESS.is_set():
        return RedirectResponse("/backups?error=Another+restore+is+already+in+progress", status_code=303)
    RESTORE_IN_PROGRESS.set()
    DB_WORK_LOCK.acquire()
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
    if suffix not in {".dump", ".sqlite"}:
        return RedirectResponse("/backups?error=Upload+a+.dump+or+.sqlite+backup", status_code=303)
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
