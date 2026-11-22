"""Opt-in, payment-backed NEXA renewals.

A payment inserts an outbox entry inside the same SQL transaction. The worker
never guesses expiry dates, never fires on account import/arming, and freezes
the provider account/product/months/key before the first paid request.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..db import SessionLocal
from ..integrations.nexa import (
    NexaError, NexaExtensionError, NexaIntegration, decrypt_api_key, encrypt_api_key,
)
from ..models import (
    ASSIGNED_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer, Integration,
    NexaAccount, NexaExtensionJob, NexaPackagePolicy, Payment, Subscription,
)

logger = logging.getLogger("share-manager.nexa")
OPEN_STATES = ("queued", "retry", "in_flight")
FINAL_STATES = ("completed", "not_needed", "review", "cancelled")
RETRY_CAP_SECONDS = 3600
LEASE_SECONDS = 120


def _epoch(value: datetime) -> int:
    """Persist Share Manager naive timestamps as UTC, without host TZ dependence."""
    return int(value.replace(tzinfo=timezone.utc).timestamp())


def eligible_policy(db: Session, account: NexaAccount) -> tuple[Subscription, NexaPackagePolicy, str, int]:
    """Resolve the *current* assigned Package and its monthly/yearly product."""
    customer = db.get(Customer, account.customer_id)
    if customer is None or customer.archived or customer.exempt:
        raise ValueError("Customer is archived/exempt or no longer present")
    subscription = (
        db.query(Subscription)
        .options(joinedload(Subscription.billing_tier))
        .filter(Subscription.customer_id == account.customer_id,
                Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
        .order_by(Subscription.id.desc()).first()
    )
    if subscription is None:
        raise ValueError("Customer has no assigned subscription")
    tier = subscription.billing_tier
    policy = db.get(NexaPackagePolicy, tier.package_id)
    if not policy or not policy.enabled or policy.integration_id != account.integration_id:
        raise ValueError("Current Package does not include an enabled NEXA entitlement")
    if tier.interval_unit == "month" and tier.interval_count == 1:
        package_id, months = policy.monthly_provider_package_id, 6
    elif tier.interval_unit == "year" and tier.interval_count == 1:
        package_id, months = policy.yearly_provider_package_id, 12
    else:
        raise ValueError("NEXA renewals support only one-month or one-year billing tiers")
    if not package_id:
        raise ValueError(f"Current Package has no {months}-month NEXA provider product")
    if not str(package_id).isdigit():
        raise ValueError("Invalid NEXA provider package identifier")
    return subscription, policy, str(package_id), months


def approved_policy(db: Session, account: NexaAccount) -> tuple[Subscription, str, int]:
    if not account.automation_enabled:
        raise ValueError("Customer IPTV automation is not enabled")
    sub, policy, package_id, months = eligible_policy(db, account)
    if (account.automation_approved_package_id != package_id
            or account.automation_approved_months != months
            or account.automation_approved_policy_at != policy.updated_at):
        raise ValueError("NEXA Package mapping changed since approval; re-arm this account")
    return sub, package_id, months


def enqueue_for_payment(db: Session, payment: Payment) -> int:
    """Transactional outbox; call after apply_payment and before its DB commit.

    Never scan historical payments; no provider network calls occur here.
    """
    if not payment.subscription_id or payment.voided_at or not payment.coverage_end:
        return 0
    added = 0
    accounts = (db.query(NexaAccount)
                .filter(NexaAccount.customer_id == payment.customer_id,
                        NexaAccount.automation_enabled.is_(True)).all())
    for account in accounts:
        try:
            sub, package_id, months = approved_policy(db, account)
            if sub.id != payment.subscription_id:
                continue
            if db.query(NexaExtensionJob.id).filter_by(account_id=account.id, payment_id=payment.id).first():
                continue
            db.add(NexaExtensionJob(
                account_id=account.id, payment_id=payment.id,
                provider_account_id=account.provider_account_id,
                provider_package_id=package_id, months=months,
                paid_through_at=payment.coverage_end,
                idempotency_key="sm-nexa-" + uuid.uuid4().hex,
                status="queued", attempts=0, post_attempted=False,
                next_attempt_at=datetime.utcnow(),
            ))
            added += 1
        except ValueError as exc:
            # Do not block unrelated Plex/Jellyfin payments on an IPTV mapping error.
            db.add(AuditLog(actor="system", action="nexa.payment_not_queued",
                            target_type="nexa_account", target_id=str(account.id),
                            detail=f"Payment {payment.id}: {exc}"))
    return added


def on_payment_changed(db: Session, payment: Payment) -> None:
    """Cancel undelivered jobs when a payment is edited/voided.

    NEXA cannot refund paid extensions. Keep completed/in-flight history intact.
    """
    for job in db.query(NexaExtensionJob).filter(NexaExtensionJob.payment_id == payment.id).all():
        if job.status in ("queued", "retry") and not job.post_attempted:
            job.status = "cancelled"
            job.completed_at = datetime.utcnow()
            job.last_error = "Original payment was amended or voided; create a new qualifying payment for renewal"
        elif job.status in ("completed", "in_flight") or job.post_attempted:
            db.add(AuditLog(actor=settings.admin_username, action="nexa.payment_after_extension",
                            target_type="nexa_extension_job", target_id=str(job.id),
                            detail="Original payment edited/voided after extension might have been sent; manual review necessary"))


def _client(db: Session, account: NexaAccount) -> NexaIntegration:
    integration = db.get(Integration, account.integration_id)
    if not integration or integration.kind != "nexa":
        raise NexaError("NEXA integration is no longer configured")
    return NexaIntegration(decrypt_api_key(integration.secret, settings.app_secret))


def _claim_next(db: Session) -> int | None:
    """DB account row lock serialises dispatch across workers/containers."""
    now = datetime.utcnow()
    candidates = (db.query(NexaExtensionJob.id, NexaExtensionJob.account_id)
                  .filter(NexaExtensionJob.status.in_(OPEN_STATES),
                          NexaExtensionJob.next_attempt_at <= now,
                          or_(NexaExtensionJob.locked_until.is_(None),
                              NexaExtensionJob.locked_until <= now))
                  .order_by(NexaExtensionJob.id.asc()).limit(50).all())
    for job_id, account_id in candidates:
        account = (db.query(NexaAccount)
                   .filter(NexaAccount.id == account_id)
                   .with_for_update(skip_locked=True).first())
        if account is None:
            continue
        job = (db.query(NexaExtensionJob)
               .filter(NexaExtensionJob.id == job_id)
               .with_for_update(skip_locked=True).first())
        if job is None or job.status not in OPEN_STATES or job.next_attempt_at > now:
            continue
        if job.locked_until and job.locked_until > now:
            continue
        older = (db.query(NexaExtensionJob.id)
                 .filter(NexaExtensionJob.account_id == account_id,
                         NexaExtensionJob.id < job.id,
                         NexaExtensionJob.status.in_(OPEN_STATES))
                 .first())
        if older is not None:
            continue
        active = (db.query(NexaExtensionJob.id)
                  .filter(NexaExtensionJob.account_id == account_id,
                          NexaExtensionJob.id != job.id,
                          NexaExtensionJob.status == "in_flight",
                          NexaExtensionJob.locked_until > now).first())
        if active is not None:
            continue
        job.status = "in_flight"
        job.attempts += 1
        job.started_at = now
        job.locked_until = now + timedelta(seconds=LEASE_SECONDS)
        db.commit()
        return job.id
    db.rollback()
    return None


def _finish(db: Session, job: NexaExtensionJob, status: str, error: str | None = None) -> None:
    job.status = status
    job.last_error = (error or "")[:240] or None
    job.locked_until = None
    if status in FINAL_STATES:
        job.completed_at = datetime.utcnow()
    db.add(AuditLog(actor="system", action="nexa.extension." + status,
                    target_type="nexa_extension_job", target_id=str(job.id),
                    detail=f"Account {job.provider_account_id}; payment {job.payment_id}; {job.last_error or status}"))
    db.commit()


def _retry(db: Session, job: NexaExtensionJob, message: str) -> None:
    job.status = "retry"
    job.last_error = message[:240]
    job.locked_until = None
    job.next_attempt_at = datetime.utcnow() + timedelta(
        seconds=min(RETRY_CAP_SECONDS, 30 * 2 ** min(job.attempts, 7)))
    db.commit()


def process_one_extension() -> bool:
    """Process at most one NEXA intention; external calls happen outside row locks."""
    db = SessionLocal()
    try:
        job_id = _claim_next(db)
    finally:
        db.close()
    if job_id is None:
        return False

    db = SessionLocal()
    try:
        job = db.get(NexaExtensionJob, job_id)
        account = db.get(NexaAccount, job.account_id)
        if account is None or account.provider_account_id != job.provider_account_id:
            _finish(db, job, "review", "Linked NEXA account identity changed")
            return True
        try:
            sub, package_id, months = approved_policy(db, account)
        except ValueError as exc:
            _finish(db, job, "review", str(exc))
            return True
        payment = db.get(Payment, job.payment_id)
        if (not payment or payment.voided_at or payment.subscription_id != sub.id
                or payment.coverage_end is None
                or payment.coverage_end < job.paid_through_at
                or sub.current_period_end is None
                or sub.current_period_end < job.paid_through_at
                or sub.status != "active"):
            _finish(db, job, "review", "Payment coverage or active subscription no longer validates")
            return True
        if package_id != job.provider_package_id or months != job.months:
            _finish(db, job, "review", "Provider package mapping changed since the job was queued")
            return True
        if _epoch(job.paid_through_at) <= int(datetime.now(timezone.utc).timestamp()):
            _finish(db, job, "not_needed", "Payment coverage has already elapsed")
            return True

        client = _client(db, account)
        if not job.post_attempted:
            catalog = client.packages()
            product = next((row for row in catalog["packages"] if row["id"] == package_id), None)
            if not product or int(product.get("months") or 0) != months:
                _finish(db, job, "review", "Mapped provider package is missing or has incompatible duration")
                return True
            if account.account_type.lower() not in ("username", "mag", "mac"):
                _finish(db, job, "review", "Unrecognised IPTV account type")
                return True
            provider = (
                client.lookup(mac=account.provider_mac)
                if account.account_type.lower() in ("mag", "mac") and account.provider_mac
                else client.lookup(username=account.provider_username or "")
            )
            if provider["id"] != account.provider_account_id:
                _finish(db, job, "review", "NEXA account ID mismatch; no extension sent")
                return True
            account.provider_status = provider["status"]
            account.provider_expiry_ts = provider["exp_date"]
            account.last_checked_at = datetime.utcnow()
            if provider["status"].lower() not in ("active", "expired") or provider["is_trial"]:
                _finish(db, job, "review", "Account is blocked, disabled, trial or otherwise ineligible")
                return True
            if provider["exp_date"] is None:
                _finish(db, job, "review", "NEXA returned no finite expiry; manual review required")
                return True
            if int(provider["exp_date"]) >= _epoch(job.paid_through_at):
                _finish(db, job, "not_needed", "NEXA expiry already covers the paid-through date")
                return True
            # Commit the fact that a paid POST *may* be about to occur.
            # Any interruption after this point MUST replay this same key/body.
            job.post_attempted = True
            db.commit()

        response = client.extend(
            account_id=job.provider_account_id,
            package_id=job.provider_package_id,
            months=job.months,
            idempotency_key=job.idempotency_key,
        )
        job.previous_expiry_ts = int(response["previous_expiry"]) if response["previous_expiry"] is not None else None
        job.new_expiry_ts = response["new_expiry"]
        job.credits_charged = response["credits_charged"]
        job.credits_remaining = int(response["credits_remaining"]) if response["credits_remaining"] is not None else None
        job.replayed = response["replayed"]
        job.password_changed = response["password_changed"]
        account.provider_expiry_ts = response["account_expiry"]
        account.provider_status = "active"
        account.last_checked_at = datetime.utcnow()
        if response["password_changed"] and response["replacement_password"]:
            account.replacement_password_encrypted = encrypt_api_key(
                response["replacement_password"], settings.app_secret)
            account.replacement_password_recorded_at = datetime.utcnow()
            account.replacement_password_ack_at = None
        warning = None
        if response["password_changed"] and not response["replacement_password"]:
            warning = "NEXA rotated the IPTV password but did not return it; contact provider"
        elif response["account_expiry"] < _epoch(job.paid_through_at):
            warning = "Paid coverage exceeds the new provider expiry; further manual renewal may be needed"
        _finish(db, job, "completed", warning)
        return True
    except NexaExtensionError as exc:
        db.rollback()
        job = db.get(NexaExtensionJob, job_id)
        if exc.retryable:
            _retry(db, job, str(exc))
        else:
            _finish(db, job, "review", str(exc))
        return True
    except NexaError as exc:
        db.rollback()
        job = db.get(NexaExtensionJob, job_id)
        message = str(exc)
        if any(term in message.lower() for term in ("timeout", "timed out", "unable to contact", "rate limit", "temporarily")):
            _retry(db, job, message)
        else:
            _finish(db, job, "review", message)
        return True
    except Exception:
        # Never log raw exception details from an API response containing credentials.
        logger.exception("NEXA queue internal error on job %s", job_id)
        db.rollback()
        job = db.get(NexaExtensionJob, job_id)
        _finish(db, job, "review", "Internal processing error; inspect server logs without exposing credentials")
        return True
    finally:
        db.close()
