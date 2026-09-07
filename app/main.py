import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .db import get_db, SessionLocal
from .models import (
    ACCESS_SUBSCRIPTION_STATES,
    ASSIGNED_SUBSCRIPTION_STATES,
    AuditLog,
    BillingTier,
    Customer,
    Integration,
    Package,
    PackageEntitlement,
    Payment,
    PaymentSource,
    Subscription,
)
from .integrations.plex import PlexIntegration
from .security import logged_in, make_session, valid_credentials
from .services.billing import apply_payment, initialize_subscription_period, process_billing
from .services.reconcile import reconcile_customer
from .version import APP_VERSION


def _parse_date(value: str | None, fallback: datetime | None = None) -> datetime | None:
    if not value:
        return fallback
    return datetime.strptime(value, "%Y-%m-%d")


def run_billing_cycle() -> int:
    """Run automatic expiry/grace transitions and reconcile changed customers."""
    db = SessionLocal()
    try:
        changed = process_billing(db)
        for customer in changed:
            if not customer.plex_username or customer.exempt:
                continue
            try:
                reconcile_customer(db, customer)
            except Exception as exc:
                db.add(AuditLog(action="billing.reconcile.error", target_type="customer", target_id=str(customer.id), detail=str(exc)))
                db.commit()
        return len(changed)
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
                pass
            await asyncio.sleep(max(1, settings.billing_check_interval_minutes) * 60)

    task = asyncio.create_task(billing_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Share Manager", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


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
    response.set_cookie("sm_session", make_session(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
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
    revenue = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.paid_at >= month_start).scalar()
    stats = {
        "customers": db.query(Customer).count(),
        "active": db.query(Customer).filter(Customer.status == "active").count(),
        "grace": db.query(Customer).filter(Customer.status == "grace", Customer.exempt == False).count(),  # noqa: E712
        "suspended": db.query(Customer).filter(Customer.status == "suspended", Customer.exempt == False).count(),  # noqa: E712
        "due_soon": due_soon,
        "revenue": Decimal(revenue or 0),
    }
    recent = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()
    return render(request, "dashboard.html", stats=stats, recent=recent)


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
    ).order_by(Customer.name).all()
    tiers = db.query(BillingTier).options(joinedload(BillingTier.package)).filter(
        BillingTier.active == True, BillingTier.package.has(active=True)  # noqa: E712
    ).order_by(BillingTier.package_id, BillingTier.price).all()
    return render(request, "customers.html", customers=rows, tiers=tiers, error=error, notice=notice)


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


@app.post("/customers/{customer_id}/status")
def customer_status(request: Request, customer_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    c = db.get(Customer, customer_id)
    if not c:
        return RedirectResponse("/customers", status_code=303)
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
    if settings.reconcile_on_assign and c.plex_username:
        try:
            reconcile_customer(db, c)
        except Exception as exc:
            db.add(AuditLog(action="plex.reconcile.error", target_type="customer", target_id=str(c.id), detail=str(exc)))
            db.commit()
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
            db.add(AuditLog(action="plex.reconcile.error", target_type="customer", target_id=str(c.id), detail=str(exc)))
            db.commit()
    return RedirectResponse("/customers?notice=Subscription+assigned", status_code=303)


@app.post("/subscriptions/{subscription_id}/billing-dates")
def edit_subscription_dates(
    request: Request,
    subscription_id: int,
    start_date: str = Form(...),
    period_end: str = Form(""),
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
    if end and end <= start:
        return RedirectResponse("/customers?error=Paid-through+date+must+be+after+the+period+start", status_code=303)
    initialize_subscription_period(sub, start, end)
    if not sub.customer.exempt:
        sub.customer.status = "active"
    db.add(AuditLog(
        actor=settings.admin_username,
        action="subscription.dates",
        target_type="subscription",
        target_id=str(sub.id),
        detail=f"starts {start:%Y-%m-%d}; paid through {sub.current_period_end:%Y-%m-%d}; grace until {sub.grace_until:%Y-%m-%d}",
    ))
    db.commit()
    return RedirectResponse("/customers?notice=Billing+dates+updated", status_code=303)


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
def integrations(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate:
        return gate
    rows = db.query(Integration).order_by(Integration.name).all()
    enriched = []
    for integration in rows:
        libraries = []
        error = None
        if integration.kind == "plex" and integration.enabled:
            try:
                libraries = PlexIntegration(integration.base_url, integration.secret).libraries()
            except Exception as exc:
                error = str(exc)
        enriched.append((integration, libraries, error))
    return render(request, "integrations.html", integrations=enriched)


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
        from urllib.parse import quote_plus
        return RedirectResponse(f"/payments?error={quote_plus(str(exc))}", status_code=303)
    detail = f"£{amount} via {source} on {paid:%Y-%m-%d}"
    if payment.subscription_id:
        detail += f"; {payment.billing_periods or 1} billing period(s); coverage {payment.coverage_start:%Y-%m-%d} -> {payment.coverage_end:%Y-%m-%d}"
    db.add(AuditLog(actor=settings.admin_username, action="payment.record", target_type="payment", target_id=str(payment.id), detail=detail))
    db.commit()

    if payment.subscription_id and settings.reconcile_on_assign and customer.plex_username and not customer.exempt:
        try:
            reconcile_customer(db, customer)
        except Exception as exc:
            db.add(AuditLog(action="payment.reconcile.error", target_type="customer", target_id=str(customer.id), detail=str(exc)))
            db.commit()

    if apply_to_subscription and not payment.subscription_id:
        return RedirectResponse("/payments?notice=Payment+recorded+as+ledger+only%3B+customer+has+no+assigned+subscription", status_code=303)
    return RedirectResponse("/payments?notice=Payment+recorded", status_code=303)


@app.get("/api/integrations/{integration_id}/libraries")
def api_libraries(request: Request, integration_id: int, db: Session = Depends(get_db)):
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    integration = db.get(Integration, integration_id)
    return PlexIntegration(integration.base_url, integration.secret).libraries()
