from __future__ import annotations

from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session, joinedload

from ..integrations.seerr import SeerrIntegration, SeerrError
from ..models import (
    ACCESS_SUBSCRIPTION_STATES,
    ASSIGNED_SUBSCRIPTION_STATES,
    BillingTier,
    Customer,
    Integration,
    Package,
    PackageEntitlement,
    RequestsPlatformSettings,
    SeerrPermissionBaseline,
    Subscription,
)


# Seerr permission bits used only for enforcing a hard zero-request policy.
# Seerr treats quota limit 0 as unlimited, so Share Manager uses request
# permissions to represent its own 0 = blocked semantics while preserving the
# user's original request permissions as a baseline for later restoration.
SEERR_ADMIN = 2
SEERR_MANAGE_USERS = 8
SEERR_REQUEST = 32
SEERR_REQUEST_4K = 1024
SEERR_REQUEST_4K_MOVIE = 2048
SEERR_REQUEST_4K_TV = 4096
SEERR_REQUEST_MOVIE = 262144
SEERR_REQUEST_TV = 524288
SEERR_REQUEST_PERMISSION_MASK = (
    SEERR_REQUEST
    | SEERR_REQUEST_4K
    | SEERR_REQUEST_4K_MOVIE
    | SEERR_REQUEST_4K_TV
    | SEERR_REQUEST_MOVIE
    | SEERR_REQUEST_TV
)


def seerr_client(db: Session) -> SeerrIntegration | None:
    cfg = db.get(RequestsPlatformSettings, 1)
    if not cfg or not cfg.enabled or not cfg.base_url or not cfg.api_key:
        return None
    return SeerrIntegration(cfg.base_url, cfg.api_key)


def assigned_packages(db: Session, customer_id: int) -> list[Package]:
    rows = (
        db.query(Package)
        .join(BillingTier, BillingTier.package_id == Package.id)
        .join(Subscription, Subscription.billing_tier_id == BillingTier.id)
        .filter(
            Subscription.customer_id == customer_id,
            Subscription.status.in_(ASSIGNED_SUBSCRIPTION_STATES),
            Package.active.is_(True),
        )
        .distinct()
        .all()
    )
    return rows


def effective_policy(db: Session, customer_id: int) -> Package | None:
    candidates = [p for p in assigned_packages(db, customer_id) if bool(p.seerr_manage_quotas)]
    if not candidates:
        return None
    # Explicit and deterministic for multi-package accounts: highest priority wins,
    # then lowest package id to make equal priorities stable.
    return sorted(candidates, key=lambda p: (-int(p.seerr_policy_priority or 0), int(p.id)))[0]


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def match_customer(client: SeerrIntegration, customer: Customer, users: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    plex = _norm(customer.plex_username)
    email = _norm(customer.email)
    if customer.seerr_user_id:
        try:
            cached = client.user(int(customer.seerr_user_id))
            cached_plex = _norm(cached.get("plexUsername"))
            cached_username = _norm(cached.get("username"))
            cached_email = _norm(cached.get("email"))
            if plex and plex in {cached_plex, cached_username}:
                return cached, "cached"
            if not plex and email and cached_email == email:
                return cached, "cached"
        except SeerrError:
            # Re-resolve below if the cached ID disappeared.
            pass
    users = users if users is not None else client.list_users()
    if plex:
        matches = [u for u in users if _norm(u.get("plexUsername")) == plex]
        if len(matches) == 1:
            return matches[0], "plex_username"
        # Some Plex-authenticated Seerr users expose the visible Plex name in
        # `username` while `plexUsername` is blank. Treat the ordinary Seerr
        # username as a safe exact-match fallback; never pick an ambiguous row.
        matches = [u for u in users if _norm(u.get("username")) == plex]
        if len(matches) == 1:
            return matches[0], "username"
    if email:
        matches = [u for u in users if _norm(u.get("email")) == email]
        if len(matches) == 1:
            return matches[0], "email"
    return None, None


def cache_match(customer: Customer, user: dict[str, Any] | None, method: str | None, error: str | None = None) -> None:
    if user and user.get("id") is not None:
        customer.seerr_user_id = int(user["id"])
        customer.seerr_username = user.get("plexUsername") or user.get("username") or user.get("email")
        customer.seerr_match_method = method
        customer.seerr_last_error = None
    else:
        customer.seerr_user_id = None
        customer.seerr_match_method = None
        customer.seerr_last_error = error or "No matching Seerr user"
    customer.seerr_last_sync_at = datetime.utcnow()




def customer_has_valid_seerr_access(db: Session, customer: Customer) -> bool:
    """Mirror current Plex access semantics for Seerr request eligibility.

    A valid request customer must be non-archived, currently active/in grace,
    and have at least one active/in-grace subscription whose package maps a
    Plex library. Suspended/cancelled/history-only customers are not valid.
    """
    if customer.archived or customer.status not in ACCESS_SUBSCRIPTION_STATES:
        return False
    return (
        db.query(Subscription.id)
        .join(BillingTier, BillingTier.id == Subscription.billing_tier_id)
        .join(Package, Package.id == BillingTier.package_id)
        .join(PackageEntitlement, PackageEntitlement.package_id == Package.id)
        .join(Integration, Integration.id == PackageEntitlement.integration_id)
        .filter(
            Subscription.customer_id == customer.id,
            Subscription.status.in_(ACCESS_SUBSCRIPTION_STATES),
            Package.active.is_(True),
            PackageEntitlement.resource_type == "library",
            Integration.kind.ilike("plex"),
        )
        .first()
        is not None
    )


def _user_id(user: dict[str, Any]) -> int | None:
    try:
        return int(user.get("id")) if user.get("id") is not None else None
    except (TypeError, ValueError):
        return None


def _user_label(user: dict[str, Any]) -> str | None:
    return user.get("plexUsername") or user.get("username") or user.get("email")


def reconcile_unmanaged_user_permissions(
    db: Session,
    client: SeerrIntegration,
    users: list[dict[str, Any]],
    valid_user_ids: set[int],
    *,
    enabled: bool,
) -> dict[str, int]:
    """Block request permissions for invalid Seerr users and restore later.

    Only request-related bits are owned by this feature. Existing Admin or
    Manage Users accounts are protected from automatic revocation. Baselines
    are stored by Seerr user ID so an account can be restored even when it has
    no Share Manager customer row.
    """
    result = {"blocked": 0, "restored": 0, "protected": 0, "errors": 0}
    baselines = {row.seerr_user_id: row for row in db.query(SeerrPermissionBaseline).all()}
    users_by_id = {uid: user for user in users if (uid := _user_id(user)) is not None}

    # When enforcement is disabled, unwind every permission change previously
    # made by this feature rather than leaving users stranded without request
    # access.
    restore_ids = set(baselines) if not enabled else (set(baselines) & valid_user_ids)
    for uid in sorted(restore_ids):
        baseline = baselines.get(uid)
        if baseline is None:
            continue
        try:
            current = client.user_permissions(uid)
            desired = (int(current) & ~SEERR_REQUEST_PERMISSION_MASK) | (int(baseline.request_permissions) & SEERR_REQUEST_PERMISSION_MASK)
            if desired != int(current):
                client.update_user_permissions(uid, desired)
            db.delete(baseline)
            result["restored"] += 1
        except SeerrError:
            result["errors"] += 1

    if not enabled:
        return result

    for uid, user in users_by_id.items():
        if uid in valid_user_ids:
            continue
        try:
            current = client.user_permissions(uid)
            # Never leave Seerr administrators or user managers under this
            # guard. If one was previously blocked before gaining elevated
            # permissions, restore its saved request bits immediately.
            if current & (SEERR_ADMIN | SEERR_MANAGE_USERS):
                baseline = baselines.get(uid)
                if baseline is not None:
                    desired = (int(current) & ~SEERR_REQUEST_PERMISSION_MASK) | (int(baseline.request_permissions) & SEERR_REQUEST_PERMISSION_MASK)
                    if desired != int(current):
                        client.update_user_permissions(uid, desired)
                    db.delete(baseline)
                    baselines.pop(uid, None)
                    result["restored"] += 1
                result["protected"] += 1
                continue
            desired = int(current) & ~SEERR_REQUEST_PERMISSION_MASK
            baseline = baselines.get(uid)
            if baseline is None and desired != int(current):
                baseline = SeerrPermissionBaseline(
                    seerr_user_id=uid,
                    username=_user_label(user),
                    request_permissions=int(current) & SEERR_REQUEST_PERMISSION_MASK,
                    blocked_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(baseline)
                baselines[uid] = baseline
            elif baseline is not None:
                baseline.username = _user_label(user)
                baseline.updated_at = datetime.utcnow()
            if desired != int(current):
                client.update_user_permissions(uid, desired)
                result["blocked"] += 1
        except SeerrError:
            result["errors"] += 1
    return result


def _configured_limit(value: int | None) -> int:
    # Package defaults are -1 from v0.13.2a onward. Keep this helper explicit
    # rather than using ``or`` so zero retains its new blocked meaning.
    return int(value) if value is not None else -1


def policy_values(package: Package) -> dict[str, int]:
    """Return only quota fields Share Manager should write to Seerr.

    Package semantics:
      -1 = unmanaged (leave Seerr's quota override untouched)
       0 = blocked (permission enforcement handles the hard block; Seerr's
           numeric zero is still written for a predictable override value)
      >0 = rolling quota enforced by Seerr
    """
    values: dict[str, int] = {}
    movie_limit = _configured_limit(package.seerr_movie_limit)
    tv_limit = _configured_limit(package.seerr_tv_limit)
    if movie_limit >= 0:
        values["movieQuotaLimit"] = movie_limit
        values["movieQuotaDays"] = max(1, int(package.seerr_movie_days or 30))
    if tv_limit >= 0:
        values["tvQuotaLimit"] = tv_limit
        values["tvQuotaDays"] = max(1, int(package.seerr_tv_days or 30))
    return values


def quota_drift(settings: dict[str, Any], package: Package) -> dict[str, tuple[Any, Any]]:
    wanted = policy_values(package)
    drift: dict[str, tuple[Any, Any]] = {}
    for key, expected in wanted.items():
        actual = settings.get(key)
        # Seerr may serialize numeric values as null/string depending on override state.
        try:
            actual_cmp = int(actual) if actual is not None else None
        except (TypeError, ValueError):
            actual_cmp = actual
        if actual_cmp != expected:
            drift[key] = (actual, expected)
    return drift


def policy_permissions(current: int, baseline: int, package: Package) -> int:
    """Apply only Share Manager's hard-block request permission changes.

    Start from the user's original request permission bits, but preserve every
    unrelated permission from Seerr's current value. Generic request grants are
    split into media-specific grants when only one media type is blocked so the
    other type keeps the same effective access.
    """
    current = int(current or 0)
    baseline = int(baseline or 0)
    desired = (current & ~SEERR_REQUEST_PERMISSION_MASK) | (baseline & SEERR_REQUEST_PERMISSION_MASK)
    movie_limit = _configured_limit(package.seerr_movie_limit)
    tv_limit = _configured_limit(package.seerr_tv_limit)

    if movie_limit == 0 or tv_limit == 0:
        if desired & SEERR_REQUEST:
            desired &= ~SEERR_REQUEST
            if movie_limit != 0:
                desired |= SEERR_REQUEST_MOVIE
            if tv_limit != 0:
                desired |= SEERR_REQUEST_TV
        if desired & SEERR_REQUEST_4K:
            desired &= ~SEERR_REQUEST_4K
            if movie_limit != 0:
                desired |= SEERR_REQUEST_4K_MOVIE
            if tv_limit != 0:
                desired |= SEERR_REQUEST_4K_TV

    if movie_limit == 0:
        desired &= ~SEERR_REQUEST_MOVIE
        desired &= ~SEERR_REQUEST_4K_MOVIE
    if tv_limit == 0:
        desired &= ~SEERR_REQUEST_TV
        desired &= ~SEERR_REQUEST_4K_TV
    return desired


def policy_has_hard_block(package: Package) -> bool:
    return _configured_limit(package.seerr_movie_limit) == 0 or _configured_limit(package.seerr_tv_limit) == 0


def permission_drift(current: int, baseline: int, package: Package) -> tuple[int, int] | None:
    wanted = policy_permissions(current, baseline, package)
    return (int(current or 0), wanted) if int(current or 0) != wanted else None


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cache_usage(customer: Customer, quota: dict[str, Any], requests: list[dict[str, Any]]) -> None:
    movie = quota.get("movie") if isinstance(quota.get("movie"), dict) else {}
    tv = quota.get("tv") if isinstance(quota.get("tv"), dict) else {}
    customer.seerr_movie_limit = _int_or_none(movie.get("limit"))
    customer.seerr_tv_limit = _int_or_none(tv.get("limit"))
    movie_remaining = _int_or_none(movie.get("remaining"))
    tv_remaining = _int_or_none(tv.get("remaining"))
    movie_used = _int_or_none(movie.get("used"))
    tv_used = _int_or_none(tv.get("used"))
    customer.seerr_movie_used = movie_used if movie_used is not None else (max(0, customer.seerr_movie_limit - movie_remaining) if customer.seerr_movie_limit is not None and movie_remaining is not None else None)
    customer.seerr_tv_used = tv_used if tv_used is not None else (max(0, customer.seerr_tv_limit - tv_remaining) if customer.seerr_tv_limit is not None and tv_remaining is not None else None)
    movie_total = 0
    tv_seasons_total = 0
    for req in requests:
        media = req.get("media") or {}
        media_type = req.get("type") or req.get("mediaType") or media.get("mediaType")
        if str(media_type).lower() == "movie":
            movie_total += 1
        elif str(media_type).lower() == "tv":
            seasons = req.get("seasons") or []
            tv_seasons_total += len(seasons) if isinstance(seasons, list) else 0
    customer.seerr_request_count = len(requests)
    customer.seerr_movie_requests_total = movie_total
    customer.seerr_tv_seasons_total = tv_seasons_total


def reconcile_customer(db: Session, customer: Customer, client: SeerrIntegration | None = None, users: list[dict[str, Any]] | None = None, *, write: bool = True) -> dict[str, Any]:
    client = client or seerr_client(db)
    if client is None:
        return {"status": "disabled"}
    if not customer.plex_username:
        cache_match(customer, None, None, "Customer has no Plex identity")
        return {"status": "unmatched", "error": customer.seerr_last_error}
    try:
        user, method = match_customer(client, customer, users)
        if not user:
            visible = len(users) if users is not None else None
            suffix = f" ({visible} Seerr users inspected)" if visible is not None else ""
            identity = customer.plex_username or customer.email or "unknown identity"
            cache_match(customer, None, None, f"No exact Seerr match for {identity!r} using plexUsername, username, or email{suffix}")
            return {"status": "unmatched", "error": customer.seerr_last_error}
        cache_match(customer, user, method)
        user_id = int(user["id"])
        package = effective_policy(db, customer.id)
        current = client.user_settings(user_id)
        current_permissions = client.user_permissions(user_id)

        # Capture the original request-permission state immediately before Share
        # Manager first enforces a hard block. This lets later policy changes
        # restore the user's pre-management request access exactly.
        baseline = customer.seerr_permissions_baseline
        hard_block = bool(package and policy_has_hard_block(package))
        if hard_block and write and baseline is None:
            baseline = int(current_permissions)
            customer.seerr_permissions_baseline = baseline

        quota_changes = quota_drift(current, package) if package else {}
        permission_change = (
            permission_drift(current_permissions, baseline, package)
            if package and baseline is not None
            else None
        )
        drift = dict(quota_changes)
        if permission_change:
            drift["requestPermissions"] = permission_change

        changed = False
        if write and package:
            if quota_changes:
                values = policy_values(package)
                if values:
                    client.update_user_settings(user_id, values)
                changed = True
            if permission_change:
                current_permissions = client.update_user_permissions(user_id, permission_change[1])
                changed = True
            if changed:
                current = client.user_settings(user_id)
                current_permissions = client.user_permissions(user_id)
                quota_changes = quota_drift(current, package)
                permission_change = permission_drift(current_permissions, baseline if baseline is not None else current_permissions, package)
                drift = dict(quota_changes)
                if permission_change:
                    drift["requestPermissions"] = permission_change

            # A prior hard block may have captured a baseline. Once the current
            # package no longer blocks either media type and those permissions
            # are restored, release the baseline so Share Manager stops owning
            # request permissions while positive/unmanaged quotas continue.
            if baseline is not None and not hard_block and not permission_change:
                customer.seerr_permissions_baseline = None
                baseline = None

        # If the customer no longer has any package managing Seerr quotas, put
        # back the request permission bits captured before Share Manager touched
        # them. Other permissions remain exactly as they are now.
        if write and not package and baseline is not None:
            restored = (int(current_permissions) & ~SEERR_REQUEST_PERMISSION_MASK) | (int(baseline) & SEERR_REQUEST_PERMISSION_MASK)
            if restored != int(current_permissions):
                current_permissions = client.update_user_permissions(user_id, restored)
                changed = True
            customer.seerr_permissions_baseline = None

        try:
            quota = client.quota(user_id)
            requests = client.requests_for_user(user_id)
            cache_usage(customer, quota, requests)
            if user.get("requestCount") is not None:
                try: customer.seerr_request_count = int(user.get("requestCount"))
                except (TypeError, ValueError): pass
        except SeerrError:
            quota, requests = {}, []
        return {"status": "matched", "user": user, "package": package, "settings": current, "permissions": current_permissions, "drift": drift, "changed": changed, "quota": quota, "requests": requests}
    except SeerrError as exc:
        customer.seerr_last_error = str(exc)
        customer.seerr_last_sync_at = datetime.utcnow()
        return {"status": "error", "error": str(exc)}


def sync_all(db: Session, *, write: bool = True, enforce_access: bool = False) -> dict[str, Any]:
    client = seerr_client(db)
    if client is None:
        raise ValueError("Seerr API integration is not configured")
    users = client.list_users()
    customers = db.query(Customer).filter(Customer.archived.is_(False)).all()

    # Resolve all valid customer identities before writing anything. This lets
    # previously blocked accounts be restored before package policy is applied.
    valid_customer_ids: set[int] = set()
    valid_user_ids: set[int] = set()
    for customer in customers:
        if not customer.plex_username or not customer_has_valid_seerr_access(db, customer):
            continue
        try:
            user, _method = match_customer(client, customer, users)
        except SeerrError:
            continue
        uid = _user_id(user) if user else None
        if uid is not None:
            valid_customer_ids.add(customer.id)
            valid_user_ids.add(uid)

    access = reconcile_unmanaged_user_permissions(
        db, client, users, valid_user_ids, enabled=enforce_access
    )

    result = {
        "matched": 0, "unmatched": 0, "errors": 0, "changed": 0,
        "drift": 0, "users": len(users),
        "access_blocked": access["blocked"],
        "access_restored": access["restored"],
        "access_protected": access["protected"],
        "access_errors": access["errors"],
    }
    for customer in customers:
        if not customer.plex_username:
            continue
        # Invalid customers are still matched/read for diagnostics and cached
        # usage, but package quota/permission policy must not re-grant access.
        customer_write = bool(write and customer.id in valid_customer_ids)
        row = reconcile_customer(db, customer, client, users, write=customer_write)
        if row["status"] == "matched":
            result["matched"] += 1
            if row.get("changed"):
                result["changed"] += 1
            if row.get("drift"):
                result["drift"] += 1
        elif row["status"] == "unmatched":
            result["unmatched"] += 1
        elif row["status"] == "error":
            result["errors"] += 1

    # Enforce invalid-user blocking last as a final guard against any
    # customer-specific reconciliation path re-granting request bits.
    if enforce_access:
        access2 = reconcile_unmanaged_user_permissions(
            db, client, users, valid_user_ids, enabled=True
        )
        for key in ("blocked", "restored", "protected", "errors"):
            if key == "blocked":
                result["access_blocked"] += access2[key]
            elif key == "restored":
                result["access_restored"] += access2[key]
            elif key == "protected":
                result["access_protected"] = max(result["access_protected"], access2[key])
            elif key == "errors":
                result["access_errors"] += access2[key]

    cfg = db.get(RequestsPlatformSettings, 1)
    if cfg:
        cfg.last_sync_at = datetime.utcnow()
        total_errors = result["errors"] + result["access_errors"]
        cfg.last_sync_success_at = datetime.utcnow() if not total_errors else cfg.last_sync_success_at
        cfg.last_sync_error = None if not total_errors else f"{total_errors} Seerr sync error(s)"
        cfg.last_matched_count = result["matched"]
        cfg.last_unmatched_count = result["unmatched"]
        cfg.last_drift_count = result["drift"]
    return result


def customer_usage(db: Session, customer: Customer) -> dict[str, Any] | None:
    client = seerr_client(db)
    if client is None or not customer.plex_username:
        return None
    row = reconcile_customer(db, customer, client, write=False)
    if row.get("status") != "matched" or not row.get("user"):
        return {"status": row.get("status"), "error": row.get("error")}
    user_id = int(row["user"]["id"])
    quota = row.get("quota")
    requests = row.get("requests")
    if quota is None or requests is None:
        try:
            quota = client.quota(user_id)
            requests = client.requests_for_user(user_id)
        except SeerrError as exc:
            return {"status": "error", "error": str(exc)}
    cache_usage(customer, quota, requests)
    movie_count = 0
    tv_seasons = 0
    for req in requests:
        media = req.get("media") or {}
        media_type = req.get("type") or req.get("mediaType") or media.get("mediaType")
        if str(media_type).lower() == "movie":
            movie_count += 1
        elif str(media_type).lower() == "tv":
            seasons = req.get("seasons") or []
            tv_seasons += len(seasons) if isinstance(seasons, list) else 0
    movie_quota = quota.get("movie") if isinstance(quota.get("movie"), dict) else {}
    tv_quota = quota.get("tv") if isinstance(quota.get("tv"), dict) else {}
    def quota_view(part, configured_limit):
        limit = part.get("limit")
        remaining = part.get("remaining")
        days = part.get("days")
        try: limit = int(limit) if limit is not None else None
        except (TypeError, ValueError): limit = None
        try: remaining = int(remaining) if remaining is not None else None
        except (TypeError, ValueError): remaining = None
        try: days = int(days) if days is not None else None
        except (TypeError, ValueError): days = None
        try: direct_used = int(part.get("used")) if part.get("used") is not None else None
        except (TypeError, ValueError): direct_used = None
        used = direct_used if direct_used is not None else (max(0, limit - remaining) if limit is not None and remaining is not None else None)
        configured_limit = _configured_limit(configured_limit) if configured_limit is not None else None
        blocked = configured_limit == 0
        unmanaged = configured_limit == -1
        if blocked:
            limit, remaining, used = 0, 0, 0
        pct = min(100, max(0, int(round(used * 100 / limit)))) if used is not None and limit else 0
        return {"limit": limit, "remaining": remaining, "used": used, "days": days, "restricted": bool(part.get("restricted")) or blocked, "pct": pct, "blocked": blocked, "unmanaged": unmanaged}
    return {
        "status": "matched",
        "user": row["user"],
        "package": row.get("package"),
        "quota": quota,
        "movie_quota": quota_view(movie_quota, row.get("package").seerr_movie_limit if row.get("package") else None),
        "tv_quota": quota_view(tv_quota, row.get("package").seerr_tv_limit if row.get("package") else None),
        "settings": row.get("settings") or {},
        "drift": row.get("drift") or {},
        "requests": requests,
        "movie_requests_total": movie_count,
        "tv_seasons_total": tv_seasons,
    }
