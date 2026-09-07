from datetime import datetime
from decimal import Decimal
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import get_db
from .models import AuditLog, BillingTier, Customer, Integration, Package, PackageEntitlement, Payment, Subscription, CURRENT_SUBSCRIPTION_STATES
from .integrations.plex import PlexIntegration
from .security import logged_in, make_session, valid_credentials
from .services.reconcile import reconcile_customer
from .version import APP_VERSION

app = FastAPI(title="Share Manager", version=APP_VERSION)
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
    response.set_cookie("sm_session", make_session(), httponly=True, samesite="lax", max_age=60*60*24*7)
    return response

@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("sm_session")
    return response

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    stats = {
        "customers": db.query(Customer).count(),
        "active": db.query(Customer).filter(Customer.status == "active").count(),
        "packages": db.query(Package).filter(Package.active == True).count(),  # noqa: E712
        "integrations": db.query(Integration).filter(Integration.enabled == True).count(),  # noqa: E712
        "payments": db.query(Payment).count(),
    }
    recent = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()
    return render(request, "dashboard.html", stats=stats, recent=recent)

@app.get("/customers", response_class=HTMLResponse)
def customers(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    rows = db.query(Customer).options(joinedload(Customer.subscriptions).joinedload(Subscription.billing_tier)).order_by(Customer.name).all()
    tiers = db.query(BillingTier).options(joinedload(BillingTier.package)).filter(BillingTier.active == True).all()  # noqa: E712
    return render(request, "customers.html", customers=rows, tiers=tiers)

@app.post("/customers")
def create_customer(request: Request, name: str = Form(...), email: str = Form(""), plex_username: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    c = Customer(name=name.strip(), email=email.strip() or None, plex_username=plex_username.strip() or None, notes=notes.strip() or None)
    db.add(c); db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="customer.create", target_type="customer", target_id=str(c.id), detail=c.name))
    db.commit()
    return RedirectResponse("/customers", status_code=303)

@app.post("/customers/{customer_id}/status")
def customer_status(request: Request, customer_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    c = db.get(Customer, customer_id)
    if not c:
        return RedirectResponse("/customers", status_code=303)

    allowed = {"active", "grace", "suspended", "cancelled", "exempt"}
    if status not in allowed:
        return RedirectResponse("/customers", status_code=303)

    if status == "exempt":
        # Preserve the customer's lifecycle state so it can be resumed when exemption is removed.
        c.exempt = True
        detail = "exempt"
    else:
        c.exempt = False
        c.status = status
        detail = status

    db.add(AuditLog(actor=settings.admin_username, action="customer.status", target_type="customer", target_id=str(c.id), detail=detail))
    db.commit()
    if settings.reconcile_on_assign and c.plex_username:
        try:
            reconcile_customer(db, c)
        except Exception:
            pass
    return RedirectResponse("/customers", status_code=303)

@app.post("/customers/{customer_id}/subscribe")
def subscribe(request: Request, customer_id: int, billing_tier_id: int = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    c = db.get(Customer, customer_id)
    tier = db.get(BillingTier, billing_tier_id)
    existing = db.query(Subscription).filter(
        Subscription.customer_id == customer_id,
        Subscription.status.in_(CURRENT_SUBSCRIPTION_STATES),
    ).all()
    for sub in existing:
        sub.status = "cancelled"
    sub = Subscription(customer_id=customer_id, billing_tier_id=billing_tier_id, status="active")
    db.add(sub)
    db.add(AuditLog(actor=settings.admin_username, action="subscription.assign", target_type="customer", target_id=str(c.id), detail=f"{tier.package.name} / {tier.name}"))
    db.commit()
    if settings.reconcile_on_assign and c.plex_username:
        try: reconcile_customer(db, c)
        except Exception as e:
            db.add(AuditLog(action="plex.reconcile.error", target_type="customer", target_id=str(c.id), detail=str(e))); db.commit()
    return RedirectResponse("/customers", status_code=303)

@app.post("/customers/{customer_id}/reconcile")
def reconcile(request: Request, customer_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    c = db.get(Customer, customer_id)
    try:
        messages = reconcile_customer(db, c)
        db.add(AuditLog(actor=settings.admin_username, action="reconcile.manual", target_type="customer", target_id=str(c.id), detail="; ".join(messages)))
    except Exception as e:
        db.add(AuditLog(actor=settings.admin_username, action="reconcile.error", target_type="customer", target_id=str(c.id), detail=str(e)))
    db.commit()
    return RedirectResponse("/customers", status_code=303)

@app.get("/packages", response_class=HTMLResponse)
def packages(request: Request, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    rows = db.query(Package).options(
        joinedload(Package.billing_tiers).joinedload(BillingTier.subscriptions),
        joinedload(Package.entitlements).joinedload(PackageEntitlement.integration),
    ).order_by(Package.name).all()
    integrations = db.query(Integration).filter(Integration.enabled == True).all()  # noqa: E712
    selected = {}
    for package in rows:
        for entitlement in package.entitlements:
            selected.setdefault(f"{package.id}:{entitlement.integration_id}", []).append(entitlement.resource_id)
    return render(request, "packages.html", packages=rows, integrations=integrations, selected=selected, error=error, notice=notice)

@app.post("/packages")
def create_package(request: Request, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    p = Package(name=name.strip(), description=description.strip() or None)
    db.add(p); db.flush(); db.add(AuditLog(actor=settings.admin_username, action="package.create", target_type="package", target_id=str(p.id), detail=p.name)); db.commit()
    return RedirectResponse("/packages", status_code=303)

@app.post("/packages/{package_id}/edit")
def edit_package(request: Request, package_id: int, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
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
    if gate: return gate
    p = db.query(Package).options(joinedload(Package.billing_tiers).joinedload(BillingTier.subscriptions)).filter(Package.id == package_id).first()
    if not p:
        return RedirectResponse("/packages?error=Package+not+found", status_code=303)
    assigned = sum(t.current_subscription_count for t in p.billing_tiers)
    if assigned:
        return RedirectResponse(f"/packages?error=Cannot+delete+package%3A+it+is+used+by+{assigned}+subscription%28s%29", status_code=303)
    name = p.name
    db.delete(p)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="package.delete", target_type="package", target_id=str(package_id), detail=name))
    db.commit()
    return RedirectResponse("/packages?notice=Package+deleted", status_code=303)

@app.post("/packages/{package_id}/tiers")
def add_tier(request: Request, package_id: int, name: str = Form(...), price: Decimal = Form(...), interval_unit: str = Form(...), interval_count: int = Form(1), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    t = BillingTier(package_id=package_id, name=name.strip(), price=price, interval_unit=interval_unit, interval_count=interval_count)
    db.add(t); db.commit()
    return RedirectResponse("/packages", status_code=303)

@app.post("/packages/{package_id}/tiers/{tier_id}/edit")
def edit_tier(request: Request, package_id: int, tier_id: int, name: str = Form(...), price: Decimal = Form(...), interval_unit: str = Form(...), interval_count: int = Form(1), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    t = db.query(BillingTier).filter(BillingTier.id == tier_id, BillingTier.package_id == package_id).first()
    if not t:
        return RedirectResponse("/packages?error=Billing+tier+not+found", status_code=303)
    if interval_unit not in {"week", "month", "year"} or interval_count < 1 or price < 0:
        return RedirectResponse("/packages?error=Invalid+billing+tier+values", status_code=303)
    t.name = name.strip()
    t.price = price
    t.interval_unit = interval_unit
    t.interval_count = interval_count
    db.add(AuditLog(actor=settings.admin_username, action="billing_tier.update", target_type="billing_tier", target_id=str(t.id), detail=f"{t.name}: £{price} / {interval_count} {interval_unit}"))
    db.commit()
    return RedirectResponse("/packages?notice=Billing+tier+updated", status_code=303)

@app.post("/packages/{package_id}/tiers/{tier_id}/delete")
def delete_tier(request: Request, package_id: int, tier_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    t = db.query(BillingTier).options(joinedload(BillingTier.subscriptions)).filter(BillingTier.id == tier_id, BillingTier.package_id == package_id).first()
    if not t:
        return RedirectResponse("/packages?error=Billing+tier+not+found", status_code=303)
    if t.current_subscription_count:
        return RedirectResponse(f"/packages?error=Cannot+delete+billing+tier%3A+it+is+used+by+{t.current_subscription_count}+subscription%28s%29", status_code=303)
    name = t.name
    db.delete(t)
    db.flush()
    db.add(AuditLog(actor=settings.admin_username, action="billing_tier.delete", target_type="billing_tier", target_id=str(tier_id), detail=name))
    db.commit()
    return RedirectResponse("/packages?notice=Billing+tier+deleted", status_code=303)

@app.post("/packages/{package_id}/entitlements")
def set_entitlements(request: Request, package_id: int, integration_id: int = Form(...), library_ids: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    integration = db.get(Integration, integration_id)
    if integration.kind != "plex": return RedirectResponse("/packages", status_code=303)
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
    if gate: return gate
    rows = db.query(Integration).order_by(Integration.name).all()
    enriched=[]
    for i in rows:
        libraries=[]; error=None
        if i.kind == "plex" and i.enabled:
            try: libraries=PlexIntegration(i.base_url, i.secret).libraries()
            except Exception as e: error=str(e)
        enriched.append((i,libraries,error))
    return render(request, "integrations.html", integrations=enriched)

@app.post("/integrations/plex")
def add_plex(request: Request, name: str = Form(...), base_url: str = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    client = PlexIntegration(base_url, token)
    info = client.test()
    i = Integration(kind="plex", name=name.strip(), base_url=base_url.strip(), secret=token.strip(), machine_identifier=info["machine_identifier"])
    db.add(i); db.flush(); db.add(AuditLog(actor=settings.admin_username, action="integration.create", target_type="integration", target_id=str(i.id), detail=f"Plex: {info['server_name']}")); db.commit()
    return RedirectResponse("/integrations", status_code=303)

@app.post("/integrations/{integration_id}/import-users")
def import_plex_users(request: Request, integration_id: int, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    i = db.get(Integration, integration_id)
    client = PlexIntegration(i.base_url, i.secret)
    created=0
    for u in client.users():
        identifier = u["username"] or u["email"]
        if not identifier: continue
        existing = db.query(Customer).filter(Customer.plex_username == identifier).first()
        if existing: continue
        db.add(Customer(name=u["username"] or u["email"], email=u["email"], plex_username=identifier, plex_user_id=u["id"] or None))
        created += 1
    db.add(AuditLog(actor=settings.admin_username, action="plex.import_users", target_type="integration", target_id=str(i.id), detail=f"Imported {created} users")); db.commit()
    return RedirectResponse("/customers", status_code=303)

@app.get("/payments", response_class=HTMLResponse)
def payments(request: Request, db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    rows = db.query(Payment).order_by(Payment.paid_at.desc()).limit(100).all()
    customers = db.query(Customer).order_by(Customer.name).all()
    lookup = {c.id:c for c in customers}
    return render(request, "payments.html", payments=rows, customers=customers, customer_lookup=lookup)

@app.post("/payments")
def add_payment(request: Request, customer_id: int = Form(...), amount: Decimal = Form(...), source: str = Form("manual"), external_reference: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)):
    gate = auth(request)
    if gate: return gate
    p = Payment(customer_id=customer_id, amount=amount, source=source, external_reference=external_reference.strip() or None, note=note.strip() or None)
    db.add(p); db.flush(); db.add(AuditLog(actor=settings.admin_username, action="payment.record", target_type="payment", target_id=str(p.id), detail=f"£{amount} via {source}")); db.commit()
    return RedirectResponse("/payments", status_code=303)

@app.get("/api/integrations/{integration_id}/libraries")
def api_libraries(request: Request, integration_id: int, db: Session = Depends(get_db)):
    if not logged_in(request): return JSONResponse({"error":"unauthorized"}, status_code=401)
    i=db.get(Integration,integration_id)
    return PlexIntegration(i.base_url,i.secret).libraries()
