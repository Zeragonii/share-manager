"""Administrator-only Phase 5 NEXA provisioning UI.

Creation approval is a local, durable intention. A separate worker executes the
only chargeable provider POST; GET screens and forms never purchase accounts.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import get_db
from .integrations.nexa import NexaError, decrypt_api_key
from .models import AuditLog, Customer, NexaAccount, NexaProvisionJob, Payment
from .services.nexa_provisioning import (
    current_provision_policy, stage_request,
    _client, _latest_paid_payment,
)


def _redirect(kind: str, message: str):
    return RedirectResponse("/integrations/nexa/provision?" + urlencode({kind: message}), status_code=303)


def register_nexa_provision_routes(app, auth, render):
    @app.get("/integrations/nexa/provision", response_class=HTMLResponse)
    def nexa_provision_page(
        request: Request, customer_id: int | None = None,
        error: str | None = None, notice: str | None = None,
        db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        eligible = []
        for customer in (db.query(Customer)
                         .filter(Customer.archived.is_(False))
                         .order_by(Customer.name.asc()).all()):
            if db.query(NexaAccount.id).filter(NexaAccount.customer_id == customer.id).first():
                continue
            try:
                _, sub, policy, integration, product_id, months = current_provision_policy(db, customer.id)
                eligible.append({"id": customer.id, "name": customer.name, "product_id": product_id,
                                 "months": months, "template_id": policy.default_template_id or "",
                                 "subscription_id": sub.id})
            except ValueError:
                continue
        selected = next((x for x in eligible if x["id"] == customer_id), None)
        catalogs, templates, balance, api_error = [], [], None, None
        if selected:
            try:
                _, _, _, integration, product_id, months = current_provision_policy(db, customer_id)
                client = _client(integration)
                response = client.packages()
                catalogs = response["packages"]
                balance = response.get("balance")
                templates = client.templates()
            except (ValueError, NexaError) as exc:
                api_error = str(exc)
        paid = False
        if selected:
            _, sub, _, _, _, _ = current_provision_policy(db, selected["id"])
            paid = _latest_paid_payment(db, sub, datetime.utcnow()) is not None
        jobs = (db.query(NexaProvisionJob)
                .order_by(NexaProvisionJob.id.desc()).limit(150).all())
        product = next((x for x in catalogs if x["id"] == selected["product_id"]), None) if selected else None
        response = render(
            request, "nexa_provision.html",
            eligible=eligible, selected=selected, templates=templates,
            product=product, balance=balance, paid=paid, api_error=api_error,
            jobs=jobs, error=error, notice=notice,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/integrations/nexa/provision")
    def nexa_request_provision(
        request: Request,
        customer_id: int = Form(...), account_type: str = Form(...),
        username: str = Form(""), mac: str = Form(""),
        template_id: str = Form(""), mode: str = Form(...),
        approve_spend: str = Form(""), approve_unpaid: str = Form(""),
        arm_future_renewals: str = Form(""),
        db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        try:
            job = stage_request(
                db, customer_id=customer_id, account_type=account_type,
                username=username, mac=mac, template_id=template_id, mode=mode,
                approve_spend=approve_spend == "approve-credit-spend",
                allow_unpaid_override=approve_unpaid == "approve-unpaid-purchase",
                arm_future_renewals=arm_future_renewals == "approve-future-renewals",
            )
            db.commit()
        except (ValueError, NexaError, IntegrityError) as exc:
            db.rollback()
            message = "This IPTV request conflicts with an existing account or request" if isinstance(exc, IntegrityError) else str(exc)
            return _redirect("error", message)
        label = ("Staged; only a NEW qualifying payment can trigger the creation"
                 if job.status == "awaiting_payment"
                 else "Approved provisioning queued; no NEXA credits have been charged by this form")
        return _redirect("notice", label)

    @app.post("/integrations/nexa/provision/{job_id}/cancel")
    def nexa_cancel_provision(request: Request, job_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        job = (db.query(NexaProvisionJob)
               .filter(NexaProvisionJob.id == job_id)
               .with_for_update().first())
        if not job:
            return _redirect("error", "Provisioning job not found")
        if job.post_attempted or job.status not in ("awaiting_payment", "queued", "retry", "review"):
            return _redirect("error", "This request may already have reached NEXA; do not cancel or create a new key")
        job.status = "cancelled"
        job.completed_at = datetime.utcnow()
        job.locked_until = None
        job.last_error = "Cancelled by administrator before any provider POST"
        db.add(AuditLog(
            actor=settings.admin_username, action="nexa.provision.cancelled",
            target_type="nexa_provision_job", target_id=str(job.id),
            detail="Cancelled before NEXA purchase; no provider action"))
        db.commit()
        return _redirect("notice", "Unsent IPTV creation request cancelled")

    @app.post("/integrations/nexa/provision/{job_id}/retry")
    def nexa_retry_provision(request: Request, job_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        job = (db.query(NexaProvisionJob)
               .filter(NexaProvisionJob.id == job_id)
               .with_for_update().first())
        if not job or job.status != "review":
            return _redirect("error", "Only reviewed creation requests can be retried")
        if not job.post_attempted:
            # If the first request never reached the provider, financial
            # eligibility will be verified again by the worker.
            if job.approval_mode == "payment" and job.payment_id is None:
                return _redirect("error", "Staged request has not received a qualifying payment")
        job.status = "retry"
        job.locked_until = None
        job.completed_at = None
        job.next_attempt_at = datetime.utcnow()
        db.add(AuditLog(
            actor=settings.admin_username, action="nexa.provision.retry",
            target_type="nexa_provision_job", target_id=str(job.id),
            detail="Same NEXA create request (account, product, duration, template and idempotency key) queued again"))
        db.commit()
        return _redirect("notice", "Original account-creation request queued for idempotent retry")

    @app.post("/integrations/nexa/provision/{account_id}/show-password", response_class=HTMLResponse)
    def nexa_show_initial_password(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if line is None or not line.initial_password_encrypted:
            return _redirect("error", "No unretrieved initial IPTV password exists")
        try:
            password = decrypt_api_key(line.initial_password_encrypted, settings.app_secret)
        except NexaError as exc:
            return _redirect("error", str(exc))
        response = render(request, "nexa_initial_credentials.html", account=line, password=password)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.post("/integrations/nexa/provision/{account_id}/ack-password")
    def nexa_ack_initial_password(request: Request, account_id: int, db: Session = Depends(get_db)):
        gate = auth(request)
        if gate:
            return gate
        line = db.get(NexaAccount, account_id)
        if not line or not line.initial_password_encrypted:
            return _redirect("error", "No initial password remains to acknowledge")
        line.initial_password_encrypted = None
        line.initial_password_ack_at = datetime.utcnow()
        db.add(AuditLog(
            actor=settings.admin_username, action="nexa.provision.initial_password_ack",
            target_type="nexa_account", target_id=str(line.id),
            detail="Initial IPTV password removed from encrypted storage after manual delivery"))
        db.commit()
        return _redirect("notice", "Initial IPTV password removed from Share Manager")
