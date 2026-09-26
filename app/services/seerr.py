from __future__ import annotations

from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session, joinedload

from ..integrations.seerr import SeerrIntegration, SeerrError
from ..models import (
    ASSIGNED_SUBSCRIPTION_STATES,
    BillingTier,
    Customer,
    Package,
    RequestsPlatformSettings,
    Subscription,
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
            if (plex and _norm(cached.get("plexUsername")) == plex) or (not plex and email and _norm(cached.get("email")) == email):
                return cached, "cached"
        except SeerrError:
            # Re-resolve below if the cached ID disappeared.
            pass
    users = users if users is not None else client.list_users()
    if plex:
        matches = [u for u in users if _norm(u.get("plexUsername")) == plex]
        if len(matches) == 1:
            return matches[0], "plex_username"
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


def policy_values(package: Package) -> dict[str, int]:
    return {
        "movieQuotaLimit": max(0, int(package.seerr_movie_limit or 0)),
        "movieQuotaDays": max(1, int(package.seerr_movie_days or 30)),
        "tvQuotaLimit": max(0, int(package.seerr_tv_limit or 0)),
        "tvQuotaDays": max(1, int(package.seerr_tv_days or 30)),
    }


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
            cache_match(customer, None, None)
            return {"status": "unmatched", "error": customer.seerr_last_error}
        cache_match(customer, user, method)
        package = effective_policy(db, customer.id)
        current = client.user_settings(int(user["id"]))
        drift = quota_drift(current, package) if package else {}
        changed = False
        if write and package and drift:
            client.update_user_settings(int(user["id"]), policy_values(package))
            current = client.user_settings(int(user["id"]))
            drift = quota_drift(current, package)
            changed = True
        try:
            quota = client.quota(int(user["id"]))
            requests = client.requests_for_user(int(user["id"]))
            cache_usage(customer, quota, requests)
            if user.get("requestCount") is not None:
                try: customer.seerr_request_count = int(user.get("requestCount"))
                except (TypeError, ValueError): pass
        except SeerrError:
            quota, requests = {}, []
        return {"status": "matched", "user": user, "package": package, "settings": current, "drift": drift, "changed": changed, "quota": quota, "requests": requests}
    except SeerrError as exc:
        customer.seerr_last_error = str(exc)
        customer.seerr_last_sync_at = datetime.utcnow()
        return {"status": "error", "error": str(exc)}


def sync_all(db: Session, *, write: bool = True) -> dict[str, Any]:
    client = seerr_client(db)
    if client is None:
        raise ValueError("Seerr API integration is not configured")
    users = client.list_users()
    customers = db.query(Customer).filter(Customer.archived.is_(False)).all()
    result = {"matched": 0, "unmatched": 0, "errors": 0, "changed": 0, "drift": 0, "users": len(users)}
    for customer in customers:
        if not customer.plex_username:
            continue
        row = reconcile_customer(db, customer, client, users, write=write)
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
    cfg = db.get(RequestsPlatformSettings, 1)
    if cfg:
        cfg.last_sync_at = datetime.utcnow()
        cfg.last_sync_success_at = datetime.utcnow() if not result["errors"] else cfg.last_sync_success_at
        cfg.last_sync_error = None if not result["errors"] else f"{result['errors']} customer sync error(s)"
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
    def quota_view(part):
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
        pct = min(100, max(0, int(round(used * 100 / limit)))) if used is not None and limit else 0
        return {"limit": limit, "remaining": remaining, "used": used, "days": days, "restricted": bool(part.get("restricted")), "pct": pct}
    return {
        "status": "matched",
        "user": row["user"],
        "package": row.get("package"),
        "quota": quota,
        "movie_quota": quota_view(movie_quota),
        "tv_quota": quota_view(tv_quota),
        "settings": row.get("settings") or {},
        "drift": row.get("drift") or {},
        "requests": requests,
        "movie_requests_total": movie_count,
        "tv_seasons_total": tv_seasons,
    }
