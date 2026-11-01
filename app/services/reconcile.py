from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from threading import RLock
from typing import Callable

from sqlalchemy.orm import Session, joinedload
from ..models import ACCESS_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer, Integration, PlexReconcileJob, Subscription
from ..integrations.plex import PlexIntegration
from .notifications import notify_event

ACTIVE_STATES = ACCESS_SUBSCRIPTION_STATES
# The shipped application runs one worker. Serialize request/scheduler attempts
# so an older in-flight attempt cannot clear work created by a newer attempt.
_reconcile_lock = RLock()


def _result_detail(integration_name: str, result: dict) -> tuple[str, str]:
    libraries = result.get("libraries", [])
    names = ", ".join(libraries) or "none"
    count = len(libraries)
    state = result.get("state", "applied")

    if state == "invited":
        return (
            "plex.invite",
            f"Sent Plex invitation via {integration_name} with {count} libraries: {names}",
        )
    if state == "pending":
        return (
            "plex.invite.pending",
            f"Plex invitation via {integration_name} is awaiting acceptance with {count} libraries: {names}",
        )
    if state == "not_shared":
        return ("plex.suspension.skipped", f"No existing Plex share to retain via {integration_name}; no invitation sent")
    if state == "removed":
        return (
            "plex.reconcile",
            f"Removed Plex server access via {integration_name}",
        )
    return (
        "plex.reconcile",
        f"Applied and verified {count} Plex libraries via {integration_name}: {names}",
    )




def enqueue_reconciliation(
    db: Session,
    customer: Customer,
    *,
    integration_ids: list[int] | None = None,
    now: datetime | None = None,
    reset_backoff: bool = True,
) -> int:
    """Queue the customer's latest desired Plex state without contacting Plex.

    The queue stores only customer/integration identities; the worker recalculates
    desired access at execution time, so later payments/status/package changes
    cannot be overwritten by stale queued intent. Explicit/manual queueing resets
    any existing retry delay so operator actions are picked up promptly.
    """
    now = now or datetime.utcnow()
    if customer.exempt or not customer.plex_username:
        return 0

    query = db.query(Integration).filter(Integration.kind == "plex", Integration.enabled.is_(True))
    if integration_ids is not None:
        query = query.filter(Integration.id.in_(integration_ids))
    integrations = query.order_by(Integration.id).all()

    queued = 0
    for integration in integrations:
        job = db.get(PlexReconcileJob, (customer.id, integration.id))
        if job is None:
            job = PlexReconcileJob(
                customer_id=customer.id,
                integration_id=integration.id,
                attempts=0,
                next_attempt_at=now,
                last_error=None,
            )
            db.add(job)
            queued += 1
        else:
            # The desired state is evaluated when the job runs. Bump an existing
            # delayed retry to now for a fresh operator/customer-state change.
            if reset_backoff:
                job.attempts = 0
                job.next_attempt_at = now
                job.last_error = None
            queued += 1
    return queued

def reconcile_customer(
    db: Session,
    customer: Customer,
    *,
    integration_ids: list[int] | None = None,
    retry_only: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """Persist work before contacting Plex; explicit actions bypass retry delays.

    Callers commit billing/customer changes before invoking this function.
    Retries carry identities only and resolve the latest committed access rules.
    """
    with _reconcile_lock:
        return _reconcile_customer(db, customer, integration_ids=integration_ids, retry_only=retry_only, now=now)


def _reconcile_customer(db, customer, *, integration_ids, retry_only, now):
    now = now or datetime.utcnow()
    # A scheduler session may have been opened before a request finished paying
    # or exempting this customer. Do not replay its cached relationship state.
    db.expire_all()
    db.refresh(customer)
    messages = []
    if customer.exempt or not customer.plex_username:
        db.query(PlexReconcileJob).filter(PlexReconcileJob.customer_id == customer.id).delete(synchronize_session=False)
        db.commit()
        return ["Skipped: exempt or no Plex user linked"]

    query = db.query(Integration).filter(Integration.kind == "plex", Integration.enabled.is_(True))
    if integration_ids is not None:
        query = query.filter(Integration.id.in_(integration_ids))
    plex_integrations = query.order_by(Integration.id).all()
    jobs = {}
    for integration in plex_integrations:
        job = db.get(PlexReconcileJob, (customer.id, integration.id))
        if retry_only:
            if job is None or job.next_attempt_at > now:
                continue
        elif job is None:
            job = PlexReconcileJob(customer_id=customer.id, integration_id=integration.id,
                                   attempts=0, next_attempt_at=now)
            db.add(job)
        jobs[integration.id] = job
    # Include every server before attempting the first one. A failure must not
    # prevent later servers from being attempted, even after a process restart.
    db.commit()

    errors = []
    for integration in plex_integrations:
        job = jobs.get(integration.id)
        if job is None:
            continue
        desired = set()
        subs = (
            db.query(Subscription)
            .options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package))
            .filter(Subscription.customer_id == customer.id)
            .all()
        )
        for sub in subs:
            if sub.status not in ACTIVE_STATES or customer.status not in ACTIVE_STATES:
                continue
            package = sub.billing_tier.package
            for entitlement in package.entitlements:
                if entitlement.integration_id == integration.id and entitlement.resource_type == "library":
                    desired.add(entitlement.resource_name)

        # Soft suspension retains an existing share with only the configured empty
        # library. Never create a share solely to display the placeholder.
        soft_suspend = False
        if customer.status == "suspended" and not customer.archived and integration.suspension_library:
            cutoff = customer.suspended_at
            if cutoff is None:
                # Legacy suspended customers have no persisted transition timestamp.
                # Never re-invite them; leave previously revoked shares untouched.
                cutoff = None
            months = max(0, int(integration.suspension_retention_months if integration.suspension_retention_months is not None else 3))
            if cutoff is not None and (months == 0 or now < cutoff + relativedelta(months=months)):
                desired = {integration.suspension_library}
                soft_suspend = True
        try:
            client = PlexIntegration(integration.base_url, integration.secret)
            result = client.apply_libraries(
                customer.plex_username,
                sorted(desired),
                plex_user_id=customer.plex_user_id,
                email=customer.email,
                existing_share_only=soft_suspend,
            )
        except Exception as exc:
            job.attempts += 1
            delay_minutes = min(60, 2 ** min(job.attempts - 1, 6))
            job.next_attempt_at = now + timedelta(minutes=delay_minutes)
            # Do not store exception text that may contain tokens or URLs.
            job.last_error = type(exc).__name__[:120]
            db.commit()
            errors.append(f"{integration.name}: {exc}")
            continue
        action, detail = _result_detail(integration.name, result)
        if soft_suspend and result.get("state") == "applied":
            action = "plex.suspension.soft"
            detail = f"Retained existing Plex share via {integration.name} with suspension library: {integration.suspension_library}"
        if customer.status == "suspended" and result.get("state") == "removed" and customer.suspended_at and integration.suspension_library:
            months = int(integration.suspension_retention_months if integration.suspension_retention_months is not None else 3)
            if months > 0 and now >= customer.suspended_at + relativedelta(months=months):
                db.add(AuditLog(action="plex.suspension.expired", target_type="customer", target_id=str(customer.id), detail=f"soft-suspension-expired:{integration.id}:{customer.id}:{customer.suspended_at.isoformat()}"))
        db.add(AuditLog(action=action, target_type="customer", target_id=str(customer.id), detail=detail))
        db.delete(job)
        db.commit()
        messages.append(detail)
        if action == "plex.invite":
            notify_event(
                db,
                event="plex.invite_sent",
                title="Plex invitation sent",
                message=f"{customer.name}: {detail}",
                target_type="customer",
                target_id=str(customer.id),
                data={"customer": customer.name, "integration": integration.name},
            )
    db.commit()
    if errors:
        raise RuntimeError("; ".join(errors))
    return messages


def enqueue_expired_soft_suspensions(db: Session, *, now: datetime | None = None) -> int:
    """Schedule removal for retained shares once their retention deadline passes."""
    now = now or datetime.utcnow()
    count = 0
    integrations = db.query(Integration).filter(Integration.kind == "plex", Integration.enabled.is_(True), Integration.suspension_library.is_not(None)).all()
    if not integrations:
        return 0
    customers = db.query(Customer).filter(Customer.status == "suspended", Customer.exempt.is_(False), Customer.archived.is_(False), Customer.suspended_at.is_not(None)).all()
    for integration in integrations:
        months = int(integration.suspension_retention_months if integration.suspension_retention_months is not None else 3)
        if months == 0:
            continue
        for customer in customers:
            if customer.plex_username and customer.suspended_at + relativedelta(months=max(0, months)) <= now:
                # A completed removal should not be repeated every cycle. A single
                # durable marker records the last hard cleanup for this suspension.
                marker = f"soft-suspension-expired:{integration.id}:{customer.id}:{customer.suspended_at.isoformat()}"
                done = db.query(AuditLog).filter(AuditLog.action == "plex.suspension.expired", AuditLog.target_type == "customer", AuditLog.target_id == str(customer.id), AuditLog.detail == marker).first()
                if done is None:
                    enqueue_reconciliation(db, customer, integration_ids=[integration.id], now=now)
                    # Only commit marker once reconciliation actually completes;
                    # retry jobs handle transient Plex outages.
                    count += 1
    db.commit()
    return count


def retry_pending_reconciliations(
    db: Session,
    *,
    now: datetime | None = None,
    on_error: Callable[[Session, Customer, Exception], None] | None = None,
) -> int:
    """Retry due work on enabled servers without replaying billing transitions."""
    now = now or datetime.utcnow()
    rows = (
        db.query(PlexReconcileJob.customer_id, PlexReconcileJob.integration_id)
        .join(Integration, Integration.id == PlexReconcileJob.integration_id)
        .filter(PlexReconcileJob.next_attempt_at <= now,
                Integration.kind == "plex", Integration.enabled.is_(True))
        .order_by(PlexReconcileJob.customer_id, PlexReconcileJob.integration_id)
        .all()
    )
    by_customer = {}
    for customer_id, integration_id in rows:
        by_customer.setdefault(customer_id, []).append(integration_id)
    for customer_id, integration_ids in by_customer.items():
        customer = db.get(Customer, customer_id)
        if customer is None:
            db.query(PlexReconcileJob).filter(PlexReconcileJob.customer_id == customer_id).delete(synchronize_session=False)
            db.commit()
            continue
        try:
            reconcile_customer(db, customer, integration_ids=integration_ids, retry_only=True, now=now)
        except Exception as exc:
            if on_error is not None:
                on_error(db, customer, exc)
    return len(by_customer)
