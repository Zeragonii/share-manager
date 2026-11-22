"""NEXA Phase 2: read-only account linking and per-Package IPTV mappings.

No provider write endpoints, renewal workers or billing hooks are registered here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .db import get_db
from .integrations.nexa import NexaError, NexaIntegration, decrypt_api_key
from .services.nexa_renewals import approved_policy, eligible_policy
from .integrations.nexa import encrypt_api_key, decrypt_api_key
from .models import (
    ASSIGNED_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer,
    Integration, NexaAccount, NexaExtensionJob, NexaPackagePolicy, Package, Subscription,
)


def _redirect(kind: str, message: str, location: str = "/integrations/nexa/accounts"):
    return RedirectResponse(location + "?" + urlencode({kind: message}), status_code=303)


def _client(db: Session) -> tuple[Integration, NexaIntegration]:
    integration = db.query(Integration).filter(Integration.kind == "nexa").first()
    if integration is None:
        raise NexaError("Connect NEXA under Integrations first")
    return integration, NexaIntegration(decrypt_api_key(integration.secret, settings.app_secret))


def _expiry_ts(lookup: dict) -> int | None:
    value = lookup.get("exp_date")
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
        if not 0 < seconds <= 253402300799:
            raise ValueError
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        return seconds
    except (ValueError, OverflowError, OSError, TypeError):
        raise NexaError("NEXA returned an unsupported expiry timestamp; account was not linked or updated") from None


def _expiry_label(seconds: int | None) -> str:
    if seconds is None:
        return "Unavailable / unlimited (manual review)"
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
    except (ValueError, OverflowError, OSError, TypeError):
        return "Invalid provider expiry"


def _apply_lookup(account: NexaAccount, result: dict, *, username: str = "", mac: str = "") -> None:
    account.provider_username = (result.get("username") or username or "")[:120] or None
    account.provider_mac = (result.get("mac") or mac or "").upper()[:17] or None
    account.account_type = (result.get("type") or "unknown")[:32]
    account.provider_status = (result.get("status") or "unknown")[:32]
    account.is_trial = bool(result.get("is_trial"))
    account.provider_expiry_ts = _expiry_ts(result)
    account.last_checked_at = datetime.utcnow()


def _option_ids(rows: list[dict]) -> set[str]:
    return {str(row["id"]) for row in rows}


def _require_template(client: NexaIntegration, identifier: str) -> None:
    if identifier and identifier not in _option_ids(client.templates()):
        raise NexaError("The selected NEXA template is unavailable or inaccessible")


def _require_package(client: NexaIntegration, identifier: str) -> None:
    if identifier and identifier not in _option_ids(client.packages()["packages"]):
        raise NexaError("The selected NEXA package is unavailable")


def validate_package_policy(catalog: list[dict], monthly_id: str, yearly_id: str, enabled: bool) -> None:
    """Validate against the live catalogue, not hard-coded provider IDs."""
    by_id = {str(row["id"]): row for row in catalog}
    for label, identifier, duration in (("Monthly", monthly_id, 6), ("Yearly", yearly_id, 12)):
        if not identifier:
            continue
        row = by_id.get(identifier)
        if row is None:
            raise NexaError(f"{label} NEXA package is not available to this reseller")
        try:
            months = int(row.get("months"))
        except (TypeError, ValueError):
            raise NexaError(f"NEXA did not provide a valid duration for the {label.lower()} package") from None
        if months != duration:
            raise NexaError(f"{label} billing requires a {duration}-month NEXA package")
    if enabled and not (monthly_id or yearly_id):
        raise NexaError("Select a 6-month and/or 12-month NEXA package before enabling this entitlement")


def register_nexa_phase2_routes(app, auth, render):
    """Register admin-only Phase 2 forms without a dependency on app.main."""

    @app.get("/packages/{package_id}/nexa", response_class=HTMLResponse)
    def nexa_package_config(request: Request, package_id: int, error: str | None = None, notice: str | None = None, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        package = db.get(Package, package_id)
        if not package or not package.active:
            return _redirect("error", "Package not found", "/packages")
        policy = db.get(NexaPackagePolicy, package_id)
        catalog, templates, api_error = [], [], None
        try:
            _integration, client = _client(db)
            catalog = client.packages()["packages"]
            templates = client.templates()
        except NexaError as exc:
            api_error = str(exc)
        response = render(request, "nexa_package.html", package=package, policy=policy,
                          catalog=catalog, nexa_templates=templates, api_error=api_error, error=error, notice=notice)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/packages/{package_id}/nexa")
    def nexa_package_save(
        request: Request, package_id: int,
        enabled: str | None = Form(None),
        monthly_provider_package_id: str = Form(""),
        yearly_provider_package_id: str = Form(""),
        default_template_id: str = Form(""),
        db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        package = db.get(Package, package_id)
        if package is None or not package.active:
            return _redirect("error", "Package not found", "/packages")
        location = f"/packages/{package_id}/nexa"
        monthly_id = monthly_provider_package_id.strip()
        yearly_id = yearly_provider_package_id.strip()
        template_id = default_template_id.strip()
        try:
            integration, client = _client(db)
            validate_package_policy(client.packages()["packages"], monthly_id, yearly_id, enabled is not None)
            _require_template(client, template_id)
        except NexaError as exc:
            return _redirect("error", str(exc), location)
        policy = db.get(NexaPackagePolicy, package_id)
        if policy is None:
            policy = NexaPackagePolicy(package_id=package_id, integration_id=integration.id)
            db.add(policy)
        policy.enabled = enabled is not None
        policy.monthly_provider_package_id = monthly_id or None
        policy.yearly_provider_package_id = yearly_id or None
        policy.default_template_id = template_id or None
        policy.updated_at = datetime.utcnow()
        db.add(AuditLog(actor=settings.admin_username, action="nexa.package_policy",
                        target_type="package", target_id=str(package_id),
                        detail=f"IPTV entitlement={'configured' if policy.enabled else 'disabled'}; monthly={monthly_id or '-'}; yearly={yearly_id or '-'}; default template={template_id or '-'}; read-only"))
        db.commit()
        return _redirect("notice", "IPTV package mapping saved; provider accounts are unchanged", location)

    @app.get("/integrations/nexa/accounts", response_class=HTMLResponse)
    def nexa_accounts_page(request: Request, error: str | None = None, notice: str | None = None,
                           db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        accounts = (db.query(NexaAccount)
                    .options(joinedload(NexaAccount.customer))
                    .order_by(NexaAccount.id.desc()).all())
        customers = (db.query(Customer).filter(Customer.archived.is_(False))
                     .order_by(Customer.name.asc()).all())
        selected_packages = {}
        for sub in (db.query(Subscription)
                    .options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package))
                    .filter(Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
                    .order_by(Subscription.id.desc()).all()):
            selected_packages.setdefault(sub.customer_id, sub.billing_tier.package_id)
        enabled_package_ids = {row.package_id for row in db.query(NexaPackagePolicy).filter(NexaPackagePolicy.enabled.is_(True)).all()}
        templates, catalog, api_error = [], [], None
        try:
            _, client = _client(db)
            templates = client.templates()
            catalog = client.packages()["packages"]
        except NexaError as exc:
            api_error = str(exc)
        renewal_state = {}
        for line in accounts:
            try:
                sub, policy, product_id, months = eligible_policy(db, line)
                try:
                    approved_policy(db, line)
                    ready = True
                except ValueError:
                    ready = False
                renewal_state[line.id] = {
                    "message": f"{sub.billing_tier.name}: NEXA product {product_id} ({months} months)",
                    "ready": ready,
                    "package_id": product_id, "months": months,
                }
            except ValueError as exc:
                renewal_state[line.id] = {"message": str(exc), "ready": False}
        jobs = (db.query(NexaExtensionJob)
                .order_by(NexaExtensionJob.id.desc()).limit(300).all())
        job_map = {}
        for job in jobs:
            job_map.setdefault(job.account_id, []).append(job)
        response = render(
            request, "nexa_accounts.html",
            accounts=accounts, customers=customers, nexa_templates=templates, catalog=catalog,
            expiry_labels={row.id: _expiry_label(row.provider_expiry_ts) for row in accounts},
            selected_packages=selected_packages, enabled_package_ids=enabled_package_ids,
            renewal_state=renewal_state, job_map=job_map,
            api_error=api_error, error=error, notice=notice,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/integrations/nexa/accounts/link")
    def nexa_link_account(
        request: Request, customer_id: int = Form(...),
        username: str = Form(""), mac: str = Form(""),
        recorded_package_id: str = Form(""), desired_template_id: str = Form(""),
        db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        customer = db.get(Customer, customer_id)
        if customer is None or customer.archived:
            return _redirect("error", "Select an active Share Manager customer")
        try:
            integration, client = _client(db)
            recorded_package_id, desired_template_id = recorded_package_id.strip(), desired_template_id.strip()
            _require_package(client, recorded_package_id)
            _require_template(client, desired_template_id)
            result = client.lookup(username=username, mac=mac)
            existing = (db.query(NexaAccount)
                        .filter(NexaAccount.integration_id == integration.id,
                                NexaAccount.provider_account_id == str(result["id"])).first())
            if existing is not None:
                return _redirect("error", "This NEXA account is already linked; unlink it before assigning another customer")
            line = NexaAccount(
                integration_id=integration.id, customer_id=customer.id,
                provider_account_id=str(result["id"]),
                recorded_package_id=recorded_package_id or None,
                desired_template_id=desired_template_id or None,
                automation_enabled=False,
            )
            _apply_lookup(line, result, username=username.strip(), mac=mac.strip())
            db.add(line)
            db.flush()
            db.add(AuditLog(actor=settings.admin_username, action="nexa.account_link",
                            target_type="nexa_account", target_id=str(line.id),
                            detail=f"Linked NEXA account {line.provider_account_id} to customer {customer.id}, read-only"))
            db.commit()
        except NexaError as exc:
            db.rollback()
            return _redirect("error", str(exc))
        except IntegrityError:
            db.rollback()
            return _redirect("error", "This NEXA account is already linked")
        return _redirect("notice", "NEXA account linked read-only; no changes made on the provider")

    @app.post("/integrations/nexa/accounts/{account_id}/refresh")
    def nexa_refresh_account(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None:
            return _redirect("error", "Linked account not found")
        try:
            _, client = _client(db)
            # Lookup remains the source of truth; never infer the expiry locally.
            if line.account_type.lower() == "mag" and line.provider_mac:
                result = client.lookup(mac=line.provider_mac)
            elif line.provider_username:
                result = client.lookup(username=line.provider_username)
            elif line.provider_mac:
                result = client.lookup(mac=line.provider_mac)
            else:
                raise NexaError("No username or MAC is stored for this account")
            if str(result["id"]) != line.provider_account_id:
                raise NexaError("NEXA lookup returned a different account ID; no local changes saved")
            _apply_lookup(line, result)
            db.add(AuditLog(actor=settings.admin_username, action="nexa.account_refresh",
                            target_type="nexa_account", target_id=str(line.id),
                            detail="Refreshed actual NEXA status and expiry using read-only lookup"))
            db.commit()
        except NexaError as exc:
            db.rollback()
            return _redirect("error", str(exc))
        return _redirect("notice", "NEXA account expiry and status refreshed from provider")

    @app.post("/integrations/nexa/accounts/{account_id}/settings")
    def nexa_account_settings(request: Request, account_id: int,
                              recorded_package_id: str = Form(""),
                              desired_template_id: str = Form(""),
                              db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None:
            return _redirect("error", "Linked account not found")
        recorded_package_id, desired_template_id = recorded_package_id.strip(), desired_template_id.strip()
        try:
            _, client = _client(db)
            _require_package(client, recorded_package_id)
            _require_template(client, desired_template_id)
        except NexaError as exc:
            return _redirect("error", str(exc))
        if line.recorded_package_id != (recorded_package_id or None):
            # A change in the manually identified provider package requires
            # renewed approval of the chargeable extension product.
            line.automation_enabled = False
            line.automation_approved_package_id = None
            line.automation_approved_months = None
            line.automation_approved_policy_at = None
        line.recorded_package_id = recorded_package_id or None
        if line.desired_template_id != (desired_template_id or None):
            line.desired_template_id = desired_template_id or None
            line.confirmed_template_id = None
            line.template_confirmed_at = None
            line.template_confirmed_by = None
        db.add(AuditLog(actor=settings.admin_username, action="nexa.account_settings",
                        target_type="nexa_account", target_id=str(account_id),
                        detail="Updated administrator-recorded package and desired template; no provider changes"))
        db.commit()
        return _redirect("notice", "NEXA account preferences saved locally; no provider changes made")

    @app.post("/integrations/nexa/accounts/{account_id}/confirm-template")
    def nexa_confirm_template(request: Request, account_id: int,
                              confirmed_on_provider: str | None = Form(None),
                              db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None:
            return _redirect("error", "Linked account not found")
        if not line.desired_template_id:
            return _redirect("error", "Choose a desired template before recording manual confirmation")
        if confirmed_on_provider != "yes":
            return _redirect("error", "Confirm that you manually checked/applied the template in NEXA")
        line.confirmed_template_id = line.desired_template_id
        line.template_confirmed_at = datetime.utcnow()
        line.template_confirmed_by = settings.admin_username
        db.add(AuditLog(actor=settings.admin_username, action="nexa.template_manual_confirmation",
                        target_type="nexa_account", target_id=str(account_id),
                        detail="Administrator manually confirmed template on NEXA; not API verified"))
        db.commit()
        return _redirect("notice", "Manual template confirmation recorded; not independently verified by API")

    @app.post("/integrations/nexa/accounts/{account_id}/unlink")
    def nexa_unlink_account(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None:
            return _redirect("error", "Linked account not found")
        if db.query(NexaExtensionJob.id).filter(NexaExtensionJob.account_id == account_id).first():
            return _redirect("error", "Extension history exists: disarm the account instead of deleting its audit trail")
        provider_id, customer_id = line.provider_account_id, line.customer_id
        db.add(AuditLog(actor=settings.admin_username, action="nexa.account_unlink",
                        target_type="nexa_account", target_id=str(account_id),
                        detail=f"Removed local link for provider account {provider_id} / customer {customer_id}; provider unaffected"))
        db.delete(line)
        db.commit()
        return _redirect("notice", "Local NEXA account link removed; provider account remains untouched")

    @app.post("/integrations/nexa/accounts/{account_id}/automation")
    def nexa_automation(
        request: Request, account_id: int, action: str = Form(...),
        acknowledge: str = Form(""), db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None:
            return _redirect("error", "Linked NEXA account not found")
        if action == "disable":
            line.automation_enabled = False
            db.add(AuditLog(actor=settings.admin_username, action="nexa.automation.off",
                            target_type="nexa_account", target_id=str(account_id),
                            detail="Administrator disabled payment-triggered extensions; already sent charges cannot be reversed"))
            db.commit()
            return _redirect("notice", "IPTV automation disabled; any already in-flight provider action cannot be cancelled")
        if action != "enable" or acknowledge != "approve-credit-spend":
            return _redirect("error", "Explicit consent to credit-spending renewal automation is required")
        if not line.recorded_package_id:
            return _redirect("error", "Record the current NEXA account package before enabling automation")
        try:
            sub, policy, product_id, months = eligible_policy(db, line)
            integration, client = _client(db)
            catalog = client.packages()["packages"]
            product = next((row for row in catalog if row["id"] == product_id), None)
            if not product or int(product.get("months") or 0) != months:
                raise NexaError("Mapped provider product is missing or has an incompatible duration")
            if line.account_type.lower() not in ("username", "mag", "mac"):
                raise NexaError("Unsupported NEXA account type")
            lookup = (client.lookup(mac=line.provider_mac)
                      if line.account_type.lower() in ("mag", "mac") and line.provider_mac
                      else client.lookup(username=line.provider_username or ""))
            if lookup["id"] != line.provider_account_id:
                raise NexaError("NEXA returned a different account ID; automation cannot be enabled")
            _apply_lookup(line, lookup)
            if lookup["status"].lower() not in ("active", "expired") or lookup["is_trial"]:
                raise NexaError("Disabled, blocked, trial or other unsupported accounts require manual review")
            if _expiry_ts(lookup) is None:
                raise NexaError("NEXA did not supply a finite expiry")
            line.automation_enabled = True
            line.automation_approved_package_id = product_id
            line.automation_approved_months = months
            line.automation_approved_policy_at = policy.updated_at
            line.automation_armed_at = datetime.utcnow()
            db.add(AuditLog(actor=settings.admin_username, action="nexa.automation.on",
                            target_type="nexa_account", target_id=str(account_id),
                            detail=f"Approved {product_id} / {months} months against Package {policy.package_id}; future new applied payments only"))
            db.commit()
        except (NexaError, ValueError) as exc:
            db.rollback()
            return _redirect("error", str(exc))
        return _redirect("notice", "NEXA automation armed; NO old payments or account expiries have been queued")

    @app.post("/integrations/nexa/jobs/{job_id}/retry")
    def nexa_retry_job(request: Request, job_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        job = db.get(NexaExtensionJob, job_id)
        if job is None or job.status != "review":
            return _redirect("error", "Only manually reviewed jobs can be retried")
        job.status = "retry"
        job.locked_until = None
        job.next_attempt_at = datetime.utcnow()
        db.add(AuditLog(actor=settings.admin_username, action="nexa.extension.retry",
                        target_type="nexa_extension_job", target_id=str(job.id),
                        detail="Administrator retried the unchanged account, product, duration and idempotency key"))
        db.commit()
        return _redirect("notice", "Original extension queued for retry using its original idempotency key")

    @app.post("/integrations/nexa/accounts/{account_id}/reveal-password", response_class=HTMLResponse)
    def nexa_reveal_password(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        account = db.get(NexaAccount, account_id)
        if not account or not account.replacement_password_encrypted:
            return _redirect("error", "No unacknowledged replacement password is available")
        try:
            password = decrypt_api_key(account.replacement_password_encrypted, settings.app_secret)
        except NexaError as exc:
            return _redirect("error", str(exc))
        response = render(request, "nexa_password.html", account=account, password=password)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.post("/integrations/nexa/accounts/{account_id}/ack-password")
    def nexa_ack_password(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        account = db.get(NexaAccount, account_id)
        if not account or not account.replacement_password_encrypted:
            return _redirect("error", "No replacement password remains to acknowledge")
        account.replacement_password_encrypted = None
        account.replacement_password_ack_at = datetime.utcnow()
        db.add(AuditLog(actor=settings.admin_username, action="nexa.password_acknowledged",
                        target_type="nexa_account", target_id=str(account_id),
                        detail="Encrypted replacement password cleared after manual handling"))
        db.commit()
        return _redirect("notice", "Replacement password cleared from Share Manager")
