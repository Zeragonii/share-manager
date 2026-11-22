"""Phase 4: read-only NEXA monitoring, forecast and durable admin actions.

Provider polling uses GET only, is rate-budgeted and never authorises any
extension. NEXA expiry is always taken from the provider, not calculated here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy.orm import Session, joinedload

from ..db import SessionLocal
from ..config import settings
from ..integrations.nexa import NexaError, NexaIntegration, decrypt_api_key
from ..models import (
    ASSIGNED_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer, Integration,
    NexaAccount, NexaAccountProbe, NexaActionItem, NexaExtensionJob,
    NexaMonitorState, NexaPackagePolicy, Subscription,
)
from .billing import add_billing_interval
from .nexa_renewals import approved_policy
from .notifications import notify_event

logger = logging.getLogger("share-manager.nexa.monitor")
MAX_ACCOUNT_POLLS = 4
MAX_NOTICES_PER_CYCLE = 4
CATALOG_INTERVAL_HOURS = 6
SEVERITY_WEIGHT = {"info": 0, "warning": 1, "critical": 2}


class Finding(NamedTuple):
    code: str
    account_id: int | None
    severity: str
    subject: str
    message: str

    @property
    def key(self) -> str:
        return f"nexa:account:{self.account_id}:{self.code}" if self.account_id else f"nexa:global:{self.code}"


def get_monitor_state(db: Session) -> NexaMonitorState:
    state = db.get(NexaMonitorState, 1)
    if state is None:
        state = NexaMonitorState(
            id=1, enabled=True, notifications_enabled=True,
            expiry_warning_days=30, stale_after_hours=48,
            low_credit_threshold=12, poll_interval_hours=6,
            catalog_json="[]",
        )
        db.add(state)
        db.flush()
    return state


def _integration_client(db: Session) -> NexaIntegration:
    integration = db.query(Integration).filter(Integration.kind == "nexa").first()
    if integration is None:
        raise NexaError("NEXA connection has not been configured")
    return NexaIntegration(decrypt_api_key(integration.secret, settings.app_secret))


def _poll_catalog(db: Session, state: NexaMonitorState, now: datetime, *, force: bool = False) -> None:
    if not force and state.catalog_checked_at and now - state.catalog_checked_at < timedelta(hours=CATALOG_INTERVAL_HOURS):
        return
    state.catalog_checked_at = now
    try:
        data = _integration_client(db).packages()
        balance = data.get("balance")
        state.credit_balance = int(balance) if balance is not None else None
        state.catalog_json = json.dumps(data.get("packages", []))
        state.catalog_error = None
    except (ValueError, TypeError):
        state.catalog_error = "NEXA credit balance or package catalogue could not be interpreted"
    except NexaError as exc:
        state.catalog_error = str(exc)[:240]
    except Exception:
        state.catalog_error = "Unexpected error retrieving NEXA packages (see server logs)"
        logger.error("Unexpected read-only NEXA catalog error")
    db.commit()


def _poll_accounts(db: Session, state: NexaMonitorState, now: datetime, *, force: bool = False) -> int:
    period = timedelta(hours=max(1, state.poll_interval_hours))
    accounts = (db.query(NexaAccount)
                .order_by(NexaAccount.last_checked_at.asc(), NexaAccount.id.asc()).all())
    count = 0
    for account in accounts:
        if count >= MAX_ACCOUNT_POLLS:
            break
        probe = db.get(NexaAccountProbe, account.id)
        last_attempt = probe.attempted_at if probe else None
        if not force:
            if account.last_checked_at and now - account.last_checked_at < period:
                continue
            if last_attempt and now - last_attempt < period:
                continue
        # Avoid a provider GET in parallel with an in-flight paid extension,
        # preventing stale monitoring data from overwriting the new expiry.
        pending = (db.query(NexaExtensionJob.id)
                   .filter(NexaExtensionJob.account_id == account.id,
                           NexaExtensionJob.status == "in_flight").first())
        if pending:
            continue
        if probe is None:
            probe = NexaAccountProbe(account_id=account.id, consecutive_failures=0)
            db.add(probe)
        probe.attempted_at = now
        count += 1
        try:
            client = _integration_client(db)
            if account.account_type.lower() in ("mag", "mac") and account.provider_mac:
                result = client.lookup(mac=account.provider_mac)
            elif account.provider_username:
                result = client.lookup(username=account.provider_username)
            elif account.provider_mac:
                result = client.lookup(mac=account.provider_mac)
            else:
                raise NexaError("No NEXA username or MAC address is stored for lookup")
            if str(result.get("id")) != account.provider_account_id:
                raise NexaError("NEXA returned a different account identity; local expiry was left untouched")
            raw_expiry = result.get("exp_date")
            if raw_expiry is not None:
                expiry = int(raw_expiry)
                if not 0 < expiry <= 253402300799:
                    raise NexaError("NEXA returned an invalid Unix-seconds expiry")
            else:
                expiry = None
            # Update only provider-supplied data, never subscription or template fields.
            account.provider_expiry_ts = expiry
            account.provider_status = str(result.get("status") or "unknown")[:32]
            account.is_trial = bool(result.get("is_trial"))
            account.last_checked_at = now
            probe.error = None
            probe.failed_at = None
            probe.consecutive_failures = 0
        except (NexaError, ValueError, TypeError) as exc:
            # Error messages from the client are intentionally sanitized.
            probe.error = (str(exc) if isinstance(exc, NexaError)
                           else "NEXA returned an invalid account expiry")[:240]
            probe.failed_at = now
            probe.consecutive_failures = int(probe.consecutive_failures or 0) + 1
        except Exception:
            probe.error = "Unexpected read-only NEXA account lookup failure"
            probe.failed_at = now
            probe.consecutive_failures = int(probe.consecutive_failures or 0) + 1
            logger.error("Unexpected read-only NEXA account lookup failure for local account %s", account.id)
        db.commit()
    return count


def _current_subscriptions(db: Session) -> dict[int, Subscription]:
    result = {}
    rows = (db.query(Subscription)
            .options(joinedload(Subscription.billing_tier))
            .filter(Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES))
            .order_by(Subscription.id.desc()).all())
    for row in rows:
        result.setdefault(row.customer_id, row)
    return result


def _expiry_datetime(seconds: int | None) -> datetime | None:
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _catalog_lookup(state: NexaMonitorState) -> dict[str, dict]:
    try:
        items = json.loads(state.catalog_json or "[]")
        return {str(x["id"]): x for x in items if isinstance(x, dict) and x.get("id") is not None}
    except (ValueError, TypeError, KeyError):
        return {}


def build_findings(db: Session, state: NexaMonitorState, now: datetime) -> list[Finding]:
    """Derive action items from stored provider facts; no provider requests or purchases."""
    findings: list[Finding] = []
    subs = _current_subscriptions(db)
    accounts = (db.query(NexaAccount)
                .options(joinedload(NexaAccount.customer))
                .order_by(NexaAccount.id).all())
    jobs = (db.query(NexaExtensionJob)
            .order_by(NexaExtensionJob.id.desc()).all())
    latest_job: dict[int, NexaExtensionJob] = {}
    for job in jobs:
        latest_job.setdefault(job.account_id, job)

    def add(code: str, account: NexaAccount, level: str, message: str) -> None:
        label = (account.customer.name if account.customer else "Unlinked customer")
        findings.append(Finding(code, account.id, level, label, message))

    for account in accounts:
        customer = account.customer
        if customer is None:
            continue
        exp = _expiry_datetime(account.provider_expiry_ts)
        sub = subs.get(account.customer_id)
        grace_end = sub.grace_until if sub else None
        paid_end = sub.current_period_end if sub else None
        if customer.archived:
            add("archived_customer", account, "warning",
                "Customer is archived in Share Manager but the linked NEXA account may remain active. Review manually.")
        elif not customer.exempt:
            if not sub:
                add("subscription_missing", account, "warning",
                    "Linked IPTV account has no currently assigned Share Manager subscription. Review the Package and billing.")
            else:
                policy = db.get(NexaPackagePolicy, sub.billing_tier.package_id)
                if not policy or not policy.enabled or policy.integration_id != account.integration_id:
                    add("package_not_mapped", account, "warning",
                        "The currently assigned Share Manager Package does not include a NEXA entitlement.")
                if paid_end and paid_end < now:
                    if grace_end and grace_end < now:
                        add("payment_overdue", account, "critical",
                            f"Payment overdue beyond grace since {paid_end:%d %b %Y}; customer may still have prepaid NEXA access. Manual provider action required.")
                    else:
                        add("payment_overdue", account, "warning",
                            f"Customer billing coverage ended {paid_end:%d %b %Y}; payment is outstanding or in grace. NEXA access is not changed automatically.")
        if account.provider_status.lower() in ("disabled", "blocked"):
            add("provider_blocked", account, "critical",
                f"NEXA reports the account is {account.provider_status}. No automatic reactivation is attempted.")
        if exp is None:
            add("expiry_unknown", account, "warning",
                "NEXA expiry is unavailable or invalid. Review this provider account manually.")
        elif exp <= now:
            add("provider_expiry", account, "critical",
                f"NEXA access expired on {exp:%d %b %Y %H:%M} UTC. No automatic extension is triggered by monitoring.")
        elif exp - now <= timedelta(days=max(1, state.expiry_warning_days)):
            level = "critical" if exp - now <= timedelta(days=7) else "warning"
            add("provider_expiry", account, level,
                f"NEXA account expires {exp:%d %b %Y %H:%M} UTC ({(exp - now).days} days remaining).")
        if exp and paid_end and paid_end > exp and not customer.archived and not customer.exempt:
            add("coverage_gap", account, "critical" if exp <= now + timedelta(days=7) else "warning",
                f"NEXA expiry {exp:%d %b %Y} is earlier than Share Manager paid-through {paid_end:%d %b %Y}. Check paid renewal status.")
        if not account.last_checked_at or now - account.last_checked_at > timedelta(hours=max(1, state.stale_after_hours)):
            add("provider_data_stale", account, "warning",
                "Provider account expiry/status data is stale; the read-only background checker will retry.")
        probe = db.get(NexaAccountProbe, account.id)
        if probe and probe.error and probe.consecutive_failures >= 2:
            add("provider_lookup_failed", account, "warning",
                f"Read-only provider account lookup failed {probe.consecutive_failures} times: {probe.error}")
        if account.replacement_password_encrypted:
            add("password_rotated", account, "critical",
                "NEXA rotated the IPTV password. Retrieve it securely from the linked-account page, update the client and acknowledge it.")
        if account.automation_enabled:
            try:
                approved_policy(db, account)
            except ValueError as exc:
                add("automation_invalid", account, "warning",
                    f"Enabled account needs automation reapproval: {exc}")
        job = latest_job.get(account.id)
        if job:
            if job.status == "review":
                add("extension_review", account, "critical",
                    f"Extension job #{job.id} requires manual review. {job.last_error or 'Provider result needs checking.'}")
            elif job.status == "retry" and job.attempts >= 3:
                add("extension_retry", account, "warning",
                    f"NEXA extension job #{job.id} has failed {job.attempts} attempts. {job.last_error or 'Check provider availability.'}")
            elif job.status == "completed" and job.last_error:
                add("extension_warning", account, "warning",
                    f"Completed extension #{job.id} needs attention: {job.last_error}")

    if state.credit_balance is not None and state.credit_balance <= state.low_credit_threshold:
        findings.append(Finding("credits_low", None,
                                "critical" if state.credit_balance < 6 else "warning",
                                "NEXA reseller credits",
                                f"NEXA balance is {state.credit_balance} credits (configured alert threshold {state.low_credit_threshold})."))
    if state.catalog_error:
        findings.append(Finding("catalog_unreachable", None, "warning",
                                "NEXA API catalogue",
                                f"Package/credit lookup failed: {state.catalog_error}. Cached balances may be stale."))
    return findings


def _reconcile_action_items(db: Session, findings: list[Finding], now: datetime) -> None:
    current = {finding.key: finding for finding in findings}
    previous = {row.action_key: row for row in db.query(NexaActionItem).all()}
    for key, finding in current.items():
        row = previous.get(key)
        if row is None:
            db.add(NexaActionItem(
                action_key=key, account_id=finding.account_id, code=finding.code,
                severity=finding.severity, status="open", subject=finding.subject,
                message=finding.message, first_seen_at=now, last_seen_at=now,
                generation=1, notified_generation=0))
            continue
        changed_severity = row.severity != finding.severity
        if row.status == "resolved" or changed_severity:
            row.generation += 1
            row.status = "open"
            row.acknowledged_at = None
            row.acknowledged_by = None
            row.resolved_at = None
            row.resolution_note = None
        row.severity = finding.severity
        row.subject = finding.subject
        row.message = finding.message
        row.last_seen_at = now
    for key, row in previous.items():
        if key not in current and row.status != "resolved":
            row.status = "resolved"
            row.resolved_at = now
            row.resolution_note = "Automatically resolved: condition no longer detected"
    db.commit()


EVENT_FOR_CODE = {
    "payment_overdue": "nexa.payment_overdue",
    "provider_expiry": "nexa.expiry_warning",
    "coverage_gap": "nexa.expiry_warning",
    "extension_review": "nexa.extension_failed",
    "extension_retry": "nexa.extension_failed",
    "extension_warning": "nexa.extension_failed",
    "credits_low": "nexa.credits_low",
    "password_rotated": "nexa.password_rotated",
}


def _dispatch_new_notifications(db: Session, state: NexaMonitorState) -> None:
    if not state.notifications_enabled:
        return
    pending = (db.query(NexaActionItem)
               .filter(NexaActionItem.status == "open",
                       NexaActionItem.generation > NexaActionItem.notified_generation)
               .order_by(NexaActionItem.id).limit(MAX_NOTICES_PER_CYCLE).all())
    for item in pending:
        key = f"nexa-monitor:{item.id}:{item.generation}"
        try:
            notify_event(
                db, event=EVENT_FOR_CODE.get(item.code, "nexa.attention_required"),
                title=f"NEXA: {item.subject} — {item.code.replace('_', ' ')}",
                message=item.message, severity=item.severity,
                target_type="nexa_action", target_id=str(item.id),
                event_key=key, data={"url": "/integrations/nexa/monitor"},
            )
            item.notified_generation = item.generation
            db.commit()
        except Exception:
            # Notification failure must not interfere with provider monitoring.
            db.rollback()
            logger.error("Failed dispatching NEXA monitoring action %s", item.id)


def monitor_cycle(*, force: bool = False) -> dict:
    """One bounded read-only background scan; never POST to NEXA."""
    db = SessionLocal()
    try:
        state = get_monitor_state(db)
        if not state.enabled and not force:
            db.commit()
            return {"enabled": False, "checked": 0}
        now = datetime.utcnow()
        _poll_catalog(db, state, now, force=force)
        count = _poll_accounts(db, state, now, force=force)
        findings = build_findings(db, state, now)
        _reconcile_action_items(db, findings, now)
        state.scanned_at = now
        db.commit()
        _dispatch_new_notifications(db, state)
        return {"enabled": True, "checked": count, "findings": len(findings)}
    finally:
        db.close()


def forecast_credits(db: Session, state: NexaMonitorState, now: datetime, *, days: int = 30) -> dict:
    """Informational estimate: assume each active account pays its NEXT due cycle.

    Only the next prospective payment is considered, never historical/overdue
    payments or all future cycles. This forecast NEVER authorises spending.
    """
    catalog = _catalog_lookup(state)
    due_by = now + timedelta(days=days)
    total, known, unknown = 0, 0, 0
    upcoming = []
    for account in db.query(NexaAccount).all():
        if not account.automation_enabled:
            continue
        try:
            sub, package_id, months = approved_policy(db, account)
            customer = db.get(Customer, account.customer_id)
            due = sub.current_period_end
            if not due or not now <= due <= due_by or not customer or customer.status != "active":
                continue
            proposed_coverage = add_billing_interval(due, sub.billing_tier)
            actual_expiry = _expiry_datetime(account.provider_expiry_ts)
            if actual_expiry is None or proposed_coverage <= actual_expiry:
                continue
            row = catalog.get(package_id, {})
            raw_credits = row.get("credits")
            try:
                credits = int(raw_credits)
                if credits < 0:
                    raise ValueError
                known += 1
                total += credits
            except (ValueError, TypeError):
                credits = None
                unknown += 1
            upcoming.append({
                "customer": customer.name, "account_id": account.id,
                "due": due, "months": months, "credits": credits,
                "product_id": package_id,
            })
        except ValueError:
            continue
    upcoming.sort(key=lambda row: row["due"])
    return {
        "days": days, "total": total, "known": known, "unknown": unknown,
        "upcoming": upcoming, "shortfall": (
            max(0, total - state.credit_balance) if state.credit_balance is not None else None
        ),
    }


def dashboard_data(db: Session, *, days: int = 30) -> dict:
    now = datetime.utcnow()
    state = get_monitor_state(db)
    accounts = db.query(NexaAccount).all()
    actions = (db.query(NexaActionItem)
               .order_by(NexaActionItem.status.asc(), NexaActionItem.id.desc()).all())
    jobs = db.query(NexaExtensionJob).all()
    stats = {
        "accounts": len(accounts),
        "armed": sum(1 for a in accounts if a.automation_enabled),
        "open": sum(1 for a in actions if a.status == "open"),
        "acknowledged": sum(1 for a in actions if a.status == "acknowledged"),
        "critical": sum(1 for a in actions if a.status != "resolved" and a.severity == "critical"),
        "review_jobs": sum(1 for j in jobs if j.status == "review"),
        "spent_credits": sum(int(j.credits_charged or 0) for j in jobs if j.status == "completed"),
        "expiring": sum(1 for a in accounts if (
            (exp := _expiry_datetime(a.provider_expiry_ts)) is not None
            and now <= exp <= now + timedelta(days=days)
        )),
    }
    return {
        "state": state, "stats": stats, "actions": actions,
        "forecast": forecast_credits(db, state, now, days=days),
        "history": db.query(NexaExtensionJob).order_by(NexaExtensionJob.id.desc()).limit(25).all(),
        "now": now, "days": days,
    }


def action_transition(db: Session, action_id: int, transition: str, *, note: str = "") -> bool:
    item = db.get(NexaActionItem, action_id)
    if not item:
        return False
    now = datetime.utcnow()
    if transition == "ack":
        if item.status != "open":
            return False
        item.status = "acknowledged"
        item.acknowledged_at = now
        item.acknowledged_by = settings.admin_username
    elif transition == "resolve":
        if item.status == "resolved":
            return False
        item.status = "resolved"
        item.resolved_at = now
        item.resolution_note = (note.strip() or "Manually marked resolved")[:1000]
    elif transition == "reopen":
        if item.status != "resolved":
            return False
        item.status = "open"
        item.generation += 1
        item.notified_generation = item.generation  # explicit manual reopen does not re-notify
        item.resolved_at = None
        item.resolution_note = None
        item.acknowledged_at = None
        item.acknowledged_by = None
    else:
        return False
    db.add(AuditLog(actor=settings.admin_username, action=f"nexa.action.{transition}",
                    target_type="nexa_action", target_id=str(action_id),
                    detail=f"{item.code}: {(note or '')[:200]}"))
    db.commit()
    return True
