"""Phase 5: durable, explicitly approved NEXA new-account provisioning.

Creating an intent does not spend credits. A manual approval or an *incoming*
applied payment releases the immutable intent; a separate worker checks
eligibility and calls NEXA with a persistent idempotency key.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db import SessionLocal
from ..integrations.nexa import (
    NexaError, NexaProvisionError, NexaIntegration, decrypt_api_key, encrypt_api_key,
)
from ..models import (
    ASSIGNED_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer, Integration,
    NexaAccount, NexaPackagePolicy, NexaProvisionJob, Payment, Subscription,
)

logger = logging.getLogger("share-manager.nexa.provision")
OPEN_STATES = ("queued", "retry", "in_flight")
BLOCKED_DUPLICATE_STATES = ("awaiting_payment", "queued", "retry", "in_flight", "review", "completed")
LEASE_SECONDS = 120


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def current_provision_policy(db: Session, customer_id: int):
    """Resolve the same Package duration that Phase 3 uses, without a linked account."""
    customer = db.get(Customer, customer_id)
    if customer is None or customer.archived or customer.exempt or customer.status != "active":
        raise ValueError("Customer must be present, active and non-exempt to provision IPTV")
    sub = (db.query(Subscription)
           .options(joinedload(Subscription.billing_tier))
           .filter(Subscription.customer_id == customer_id,
                   Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
           .order_by(Subscription.id.desc()).first())
    if not sub or sub.status != "active":
        raise ValueError("Customer must have an active Share Manager subscription")
    tier = sub.billing_tier
    policy = db.get(NexaPackagePolicy, tier.package_id)
    if not policy or not policy.enabled:
        raise ValueError("Current Share Manager Package does not include NEXA IPTV")
    if tier.interval_unit == "month" and tier.interval_count == 1:
        product_id, months = policy.monthly_provider_package_id, 6
    elif tier.interval_unit == "year" and tier.interval_count == 1:
        product_id, months = policy.yearly_provider_package_id, 12
    else:
        raise ValueError("NEXA provisioning requires a monthly or yearly billing tier")
    if not product_id or not re.fullmatch(r"[0-9]{1,18}", str(product_id)):
        raise ValueError("The Package has no valid NEXA product for this billing cycle")
    integration = db.get(Integration, policy.integration_id)
    if not integration or integration.kind != "nexa":
        raise ValueError("Mapped NEXA integration no longer exists")
    return customer, sub, policy, integration, str(product_id), months


def _client(integration: Integration) -> NexaIntegration:
    return NexaIntegration(decrypt_api_key(integration.secret, settings.app_secret))


def _product_ok(client: NexaIntegration, product_id: str, months: int, account_type: str) -> tuple[dict, int | None]:
    data = client.packages()
    product = next((x for x in data["packages"] if str(x["id"]) == product_id), None)
    if not product or int(product.get("months") or 0) != months:
        raise NexaError("Mapped NEXA package is missing or has the wrong duration")
    allowed = product.get("account_types")
    if allowed:
        if not isinstance(allowed, (list, tuple, set)):
            raise NexaError("NEXA product account-type permissions are unavailable")
        types = {str(x).lower() for x in allowed}
        if account_type not in types and not (account_type == "mag" and "mac" in types):
            raise NexaError("Mapped NEXA package does not support the requested account type")
    try:
        credits = int(product["credits"])
        if credits < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        credits = None
    return product, credits


def _credentials_ok(account_type: str, username: str, mac: str) -> tuple[str, str]:
    account_type = account_type.strip().lower()
    username, mac = username.strip(), mac.strip().upper()
    if account_type == "username":
        if not username or len(username) > 120 or any(c.isspace() for c in username) or mac:
            raise ValueError("Choose only a username (no spaces; maximum 120 characters)")
    elif account_type == "mag":
        if username or not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", mac):
            raise ValueError("Choose only a MAG MAC address in the form 00:1A:79:42:56:78")
    else:
        raise ValueError("Choose username or MAG account type")
    return username, mac


def _ensure_no_duplicate(db: Session, customer_id: int, username: str, mac: str, integration_id: int, *, exclude_job_id: int | None = None):
    if db.query(NexaAccount.id).filter(NexaAccount.customer_id == customer_id).first():
        raise ValueError("Customer already has a linked NEXA account; use their existing line")
    if username and db.query(NexaAccount.id).filter(
            NexaAccount.integration_id == integration_id,
            NexaAccount.provider_username == username).first():
        raise ValueError("That IPTV username is already linked to another Share Manager account")
    if mac and db.query(NexaAccount.id).filter(
            NexaAccount.integration_id == integration_id,
            NexaAccount.provider_mac == mac).first():
        raise ValueError("That MAC address is already linked in Share Manager")
    active_jobs = db.query(NexaProvisionJob.id).filter(
        NexaProvisionJob.integration_id == integration_id,
        NexaProvisionJob.status.in_(BLOCKED_DUPLICATE_STATES))
    if exclude_job_id is not None:
        active_jobs = active_jobs.filter(NexaProvisionJob.id != exclude_job_id)
    if active_jobs.filter(NexaProvisionJob.customer_id == customer_id).first():
        raise ValueError("This customer already has an active or completed NEXA provisioning request")
    if username and active_jobs.filter(NexaProvisionJob.provider_username == username).first():
        raise ValueError("That username is already reserved by another provisioning request")
    if mac and active_jobs.filter(NexaProvisionJob.provider_mac == mac).first():
        raise ValueError("That MAC address is already reserved by another provisioning request")


def _valid_payment(db: Session, payment: Payment | None, sub: Subscription, now: datetime) -> bool:
    return bool(payment and payment.subscription_id == sub.id
                and payment.voided_at is None and payment.amount is not None
                and payment.amount > 0 and payment.coverage_end
                and payment.coverage_end > now
                and sub.current_period_end and sub.current_period_end >= payment.coverage_end)


def _latest_paid_payment(db: Session, sub: Subscription, now: datetime) -> Payment | None:
    payments = (db.query(Payment)
                .filter(Payment.subscription_id == sub.id,
                        Payment.voided_at.is_(None),
                        Payment.coverage_end > now,
                        Payment.amount > 0)
                .order_by(Payment.id.desc()).all())
    return next((payment for payment in payments if _valid_payment(db, payment, sub, now)), None)


def stage_request(
    db: Session, *, customer_id: int, account_type: str,
    username: str, mac: str, template_id: str, mode: str,
    approve_spend: bool, allow_unpaid_override: bool, arm_future_renewals: bool,
) -> NexaProvisionJob:
    """Persist an operator-approved intent; no POST to provider here."""
    if not approve_spend:
        raise ValueError("Explicit approval of the future NEXA credit charge is required")
    if mode not in ("manual", "payment"):
        raise ValueError("Choose create-now or await a new applied payment")
    username, mac = _credentials_ok(account_type, username, mac)
    # Serialise multiple browser submissions for the same customer.
    customer = db.query(Customer).filter(Customer.id == customer_id).with_for_update().first()
    if not customer:
        raise ValueError("Customer not found")
    _, sub, policy, integration, product_id, months = current_provision_policy(db, customer_id)
    _ensure_no_duplicate(db, customer_id, username, mac, integration.id)
    template_id = template_id.strip()
    client = _client(integration)
    _product, credits = _product_ok(client, product_id, months, account_type)
    if template_id and template_id not in {str(row["id"]) for row in client.templates()}:
        raise ValueError("Selected NEXA template is unavailable or inaccessible")
    now = datetime.utcnow()
    payment = _latest_paid_payment(db, sub, now) if mode == "manual" else None
    if mode == "manual" and not payment and not allow_unpaid_override:
        raise ValueError("No active applied payment covers this customer. Use await-payment, or explicitly approve an unpaid manual purchase")
    if mode == "payment" and allow_unpaid_override:
        raise ValueError("Unpaid override is only valid for an immediate, explicitly authorised purchase")
    job = NexaProvisionJob(
        customer_id=customer.id, integration_id=integration.id,
        billing_subscription_id=sub.id,
        payment_id=payment.id if payment else None,
        provider_package_id=product_id, months=months,
        account_type=account_type, provider_username=username or None,
        provider_mac=mac or None, template_id=template_id or None,
        approved_credit_cost=credits,
        approved_policy_at=policy.updated_at,
        approval_mode=mode,
        allow_unpaid_override=bool(mode == "manual" and allow_unpaid_override and not payment),
        arm_future_renewals=bool(arm_future_renewals),
        idempotency_key="sm-nexa-create-" + uuid.uuid4().hex,
        status="queued" if mode == "manual" else "awaiting_payment",
        next_attempt_at=now,
    )
    db.add(job)
    db.flush()
    db.add(AuditLog(
        actor=settings.admin_username, action="nexa.provision.approved",
        target_type="nexa_provision_job", target_id=str(job.id),
        detail=f"Customer {customer.id}; {mode}; product {product_id}, {months}mo; template {template_id or 'none'}; charge approved. No provider POST yet",
    ))
    return job


def activate_for_payment(db: Session, payment: Payment) -> int:
    """Activate staged intentions only for a new qualifying applied payment.

    Called inside the same transaction as Share Manager's apply_payment().
    """
    if not payment.subscription_id or payment.voided_at or not payment.coverage_end or payment.amount <= 0:
        return 0
    now = datetime.utcnow()
    jobs = (db.query(NexaProvisionJob)
            .filter(NexaProvisionJob.customer_id == payment.customer_id,
                    NexaProvisionJob.billing_subscription_id == payment.subscription_id,
                    NexaProvisionJob.status == "awaiting_payment").all())
    changed = 0
    for job in jobs:
        try:
            customer, sub, policy, integration, product_id, months = current_provision_policy(db, payment.customer_id)
            if not _valid_payment(db, payment, sub, now):
                continue
            if (integration.id != job.integration_id
                    or policy.updated_at != job.approved_policy_at
                    or product_id != job.provider_package_id or months != job.months):
                job.status = "review"
                job.last_error = "Package mapping changed since the provision request was approved"
                continue
            job.payment_id = payment.id
            job.status = "queued"
            job.next_attempt_at = now
            changed += 1
        except ValueError as exc:
            job.status = "review"
            job.last_error = str(exc)[:240]
    db.flush()
    return changed


def on_payment_changed(db: Session, payment: Payment):
    """Editing/voiding a payment cancels only purchases that have not been sent."""
    for job in db.query(NexaProvisionJob).filter(NexaProvisionJob.payment_id == payment.id).all():
        if job.status in ("queued", "retry") and not job.post_attempted:
            job.status = "cancelled"
            job.completed_at = datetime.utcnow()
            job.last_error = "Source payment edited/voided; re-stage with a new explicit approval"
        elif job.post_attempted or job.status in ("completed", "in_flight"):
            db.add(AuditLog(
                actor=settings.admin_username, action="nexa.provision.payment_amended",
                target_type="nexa_provision_job", target_id=str(job.id),
                detail="Payment edited or voided after NEXA creation may have been sent; provider purchase cannot be reversed",
            ))


def _claim_next(db: Session) -> int | None:
    now = datetime.utcnow()
    ids = (db.query(NexaProvisionJob.id, NexaProvisionJob.customer_id)
           .filter(NexaProvisionJob.status.in_(OPEN_STATES),
                   NexaProvisionJob.next_attempt_at <= now,
                   or_(NexaProvisionJob.locked_until.is_(None),
                       NexaProvisionJob.locked_until <= now))
           .order_by(NexaProvisionJob.id).limit(30).all())
    for job_id, customer_id in ids:
        customer = (db.query(Customer).filter(Customer.id == customer_id)
                    .with_for_update(skip_locked=True).first())
        if not customer:
            continue
        job = (db.query(NexaProvisionJob)
               .filter(NexaProvisionJob.id == job_id)
               .with_for_update(skip_locked=True).first())
        if not job or job.status not in OPEN_STATES or job.next_attempt_at > now:
            continue
        if job.locked_until and job.locked_until > now:
            continue
        job.status = "in_flight"
        job.locked_until = now + timedelta(seconds=LEASE_SECONDS)
        job.attempts += 1
        job.started_at = now
        db.commit()
        return job.id
    db.rollback()
    return None


def _finish(db: Session, job: NexaProvisionJob, status: str, message: str | None = None):
    job.status = status
    job.last_error = (message or "")[:240] or None
    job.locked_until = None
    if status in ("completed", "review", "cancelled"):
        job.completed_at = datetime.utcnow()
    db.add(AuditLog(
        actor="system", action="nexa.provision." + status,
        target_type="nexa_provision_job", target_id=str(job.id),
        detail=f"Customer {job.customer_id}; NEXA product {job.provider_package_id}; status {status}; {job.last_error or ''}"[:500],
    ))
    db.commit()


def _retry(db: Session, job: NexaProvisionJob, message: str):
    job.status = "retry"
    job.last_error = message[:240]
    job.locked_until = None
    job.next_attempt_at = datetime.utcnow() + timedelta(
        seconds=min(3600, 30 * 2 ** min(job.attempts, 7)))
    db.commit()


def process_one_provision() -> bool:
    """Execute at most one approved creation request; never invent a new key."""
    db = SessionLocal()
    try:
        job_id = _claim_next(db)
    finally:
        db.close()
    if job_id is None:
        return False
    db = SessionLocal()
    try:
        job = db.get(NexaProvisionJob, job_id)
        if not job or job.status != "in_flight":
            return True
        customer = db.get(Customer, job.customer_id)
        integration = db.get(Integration, job.integration_id)
        if not customer or not integration or integration.kind != "nexa":
            _finish(db, job, "review", "Customer or NEXA integration no longer exists")
            return True
        client = _client(integration)
        # After a possible paid POST, NEVER modify the body, choose a new key
        # or abort recovery merely because the Package/payment changed.
        if not job.post_attempted:
            try:
                current_customer, sub, policy, mapped_integration, product_id, months = current_provision_policy(db, job.customer_id)
            except ValueError as exc:
                _finish(db, job, "review", str(exc))
                return True
            if (sub.id != job.billing_subscription_id
                    or mapped_integration.id != integration.id
                    or policy.updated_at != job.approved_policy_at
                    or product_id != job.provider_package_id or months != job.months):
                _finish(db, job, "review", "Package or subscription changed after provisioning approval")
                return True
            if job.approval_mode == "payment" or not job.allow_unpaid_override:
                payment = db.get(Payment, job.payment_id) if job.payment_id else None
                if not _valid_payment(db, payment, sub, datetime.utcnow()):
                    _finish(db, job, "review", "A valid applied payment is required before IPTV creation")
                    return True
            _ensure_no_duplicate(db, job.customer_id, job.provider_username or "", job.provider_mac or "", integration.id, exclude_job_id=job.id)
            _, credits = _product_ok(client, job.provider_package_id, job.months, job.account_type)
            if job.template_id and job.template_id not in {str(row["id"]) for row in client.templates()}:
                _finish(db, job, "review", "Selected NEXA template no longer exists or is inaccessible")
                return True
            # Do not create a duplicate of a provider line already created
            # manually in the portal and not linked locally.
            try:
                if job.account_type == "mag":
                    found = client.lookup(mac=job.provider_mac or "")
                else:
                    found = client.lookup(username=job.provider_username or "")
            except NexaError as exc:
                if "No matching NEXA account" not in str(exc):
                    raise
            else:
                _finish(db, job, "review",
                        f"NEXA already has an account for this identifier (ID {found['id']}); link it instead")
                return True
            # Once this is committed, every retry must replay the same request.
            job.post_attempted = True
            db.commit()

        result = client.create_account(
            package_id=job.provider_package_id, months=job.months,
            account_type=job.account_type, username=job.provider_username or "",
            mac=job.provider_mac or "", template_id=job.template_id or "",
            idempotency_key=job.idempotency_key,
        )
        # Either account creation succeeds alongside local linking and password
        # encryption, or the job remains replayable using the same provider key.
        already = (db.query(NexaAccount).filter(
            NexaAccount.integration_id == integration.id,
            NexaAccount.provider_account_id == result["id"]).first())
        if already is not None:
            if already.customer_id != job.customer_id:
                _finish(db, job, "review", "Created provider ID is already linked to another customer")
                return True
            # A previous successful worker might have committed local state.
            job.created_nexa_account_id = already.id
        else:
            other = db.query(NexaAccount.id).filter(NexaAccount.customer_id == job.customer_id).first()
            if other:
                _finish(db, job, "review", "Customer acquired a different IPTV account before completion")
                return True
            line = NexaAccount(
                integration_id=integration.id, customer_id=job.customer_id,
                provider_account_id=result["id"],
                provider_username=result["username"] or None,
                provider_mac=result["mac"] or None,
                account_type=result["type"], provider_status="active",
                provider_expiry_ts=result["exp_date"],
                last_checked_at=datetime.utcnow(), recorded_package_id=job.provider_package_id,
                desired_template_id=job.template_id,
                confirmed_template_id=job.template_id,
                template_confirmed_at=datetime.utcnow() if job.template_id else None,
                template_confirmed_by="nexa_api" if job.template_id else None,
                automation_enabled=False,
            )
            if job.arm_future_renewals:
                line.automation_enabled = True
                line.automation_approved_package_id = job.provider_package_id
                line.automation_approved_months = job.months
                line.automation_approved_policy_at = job.approved_policy_at
                line.automation_armed_at = datetime.utcnow()
            if result["password"]:
                line.initial_password_encrypted = encrypt_api_key(result["password"], settings.app_secret)
                line.initial_password_recorded_at = datetime.utcnow()
            db.add(line)
            db.flush()
            job.created_nexa_account_id = line.id
        job.provider_account_id = result["id"]
        job.credits_charged = result["credits_charged"]
        job.credits_remaining = int(result["credits_remaining"]) if result["credits_remaining"] is not None else None
        job.replayed = result["replayed"]
        _finish(db, job, "completed")
        return True
    except NexaProvisionError as exc:
        db.rollback()
        job = db.get(NexaProvisionJob, job_id)
        if exc.retryable:
            _retry(db, job, str(exc))
        else:
            _finish(db, job, "review", str(exc))
        return True
    except NexaError as exc:
        db.rollback()
        job = db.get(NexaProvisionJob, job_id)
        message = str(exc)
        if any(s in message.lower() for s in ("timed out", "timeout", "unable to contact", "temporarily", "rate limit")):
            _retry(db, job, message)
        else:
            _finish(db, job, "review", message)
        return True
    except (ValueError, TypeError, IntegrityError):
        db.rollback()
        job = db.get(NexaProvisionJob, job_id)
        _finish(db, job, "review", "Invalid provider response or database conflict; retry original key after review")
        return True
    except Exception:
        # Never emit API JSON / credentials to logs or error pages.
        logger.error("Unexpected provisioning error for job %s; sanitized", job_id)
        db.rollback()
        job = db.get(NexaProvisionJob, job_id)
        _finish(db, job, "review", "Unexpected provisioning error; manual review of original request required")
        return True
    finally:
        db.close()
