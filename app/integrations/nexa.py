"""Read-only NEXA reseller API integration (Phase 1).

Only GET operations are intentionally exposed; no account creation or extensions.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://neva.yabadabado.store"
SECRET_PREFIX = "nexa-fernet-v1:"
MAX_RESPONSE_BYTES = 1_000_000


class NexaError(RuntimeError):
    """Safe, user-presentable provider error. Never include secrets or raw URLs."""


def encrypt_api_key(api_key: str, app_secret: str) -> str:
    if not api_key or not api_key.strip():
        raise NexaError("NEXA API key is required")
    if not app_secret or app_secret == "dev-only-change-me":
        raise NexaError("Configure a strong, persistent APP_SECRET before saving a NEXA key")
    key = base64.urlsafe_b64encode(hashlib.sha256(("share-manager/nexa/v1:" + app_secret).encode()).digest())
    token = Fernet(key).encrypt(api_key.strip().encode()).decode()
    return SECRET_PREFIX + token


def decrypt_api_key(value: str, app_secret: str) -> str:
    if not value or not value.startswith(SECRET_PREFIX):
        raise NexaError("NEXA API key is missing or has an unsupported storage format; enter a new key")
    if not app_secret or app_secret == "dev-only-change-me":
        raise NexaError("Configure the same persistent APP_SECRET used when the NEXA key was saved")
    key = base64.urlsafe_b64encode(hashlib.sha256(("share-manager/nexa/v1:" + app_secret).encode()).digest())
    try:
        return Fernet(key).decrypt(value[len(SECRET_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        raise NexaError("Unable to decrypt the NEXA key; verify APP_SECRET or enter a new key") from None


class NexaIntegration:
    """Minimal, fixed-origin, read-only client for the documented reseller API."""

    def __init__(self, api_key: str, timeout: float = 10.0):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise NexaError("NEXA API key is required")
        self.timeout = timeout

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict:
        # Fixed paths and host ensure credentials cannot be redirected or sent to user-supplied servers.
        if path not in ("/packages", "/templates", "/accounts/lookup"):
            raise NexaError("Unsupported NEXA read operation")
        try:
            response = httpx.get(
                f"{BASE_URL}/api/reseller/v1{path}",
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                params=params, timeout=self.timeout, follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise NexaError("NEXA did not respond before the request timed out") from None
        except httpx.HTTPError:
            raise NexaError("Unable to contact the NEXA API") from None
        if response.status_code != 200:
            messages = {
                401: "NEXA rejected the API key (invalid or revoked)",
                403: "NEXA denied access to this resource",
                404: "No matching NEXA account belongs to this reseller",
                409: "NEXA returned ambiguous account matches",
                429: "NEXA rate limit reached; try again later",
                503: "NEXA API is temporarily unavailable",
            }
            raise NexaError(messages.get(response.status_code, f"NEXA returned HTTP {response.status_code}"))
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise NexaError("NEXA returned an unexpectedly large response")
        try:
            payload = response.json()
        except ValueError:
            raise NexaError("NEXA returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise NexaError("Unexpected NEXA response format")
        if payload.get("success") is False:
            # Never reflect arbitrary API error text, which could contain secrets.
            raise NexaError("NEXA reported that the read request failed")
        return payload

    @staticmethod
    def _section(payload: dict, key: str) -> list[dict]:
        data = payload.get("data")
        items = payload.get(key)
        if items is None and isinstance(data, dict):
            items = data.get(key)
        if not isinstance(items, list):
            raise NexaError(f"NEXA did not return a {key} list")
        return [item for item in items[:500] if isinstance(item, dict)]

    def packages(self) -> dict:
        payload = self._get("/packages")
        items = self._section(payload, "packages")
        packages = []
        for row in items:
            if row.get("id") is None:
                continue
            packages.append({
                "id": str(row["id"]),
                "name": str(row.get("name") or row.get("title") or "Unnamed package"),
                "months": row.get("months"),
                "credits": row.get("credits", row.get("credit_price", row.get("price"))),
                "connections": row.get("connections", row.get("max_connections")),
                "account_types": row.get("account_types", row.get("types")),
            })
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        balance = next((x[k] for x in (payload, data) for k in ("balance", "credits_balance", "credit_balance", "credits_remaining", "credits") if k in x), None)
        return {"packages": packages, "balance": balance}

    def templates(self) -> list[dict]:
        payload = self._get("/templates")
        templates = []
        for row in self._section(payload, "templates"):
            if row.get("id") is None:
                continue
            templates.append({
                "id": str(row["id"]),
                "name": str(row.get("name") or row.get("title") or "Unnamed template"),
                "source": str(row.get("source") or ("Shared" if row.get("is_shared") else "Own")),
            })
        return templates

    def lookup(self, *, username: str = "", mac: str = "") -> dict:
        username, mac = username.strip(), mac.strip()
        if bool(username) == bool(mac):
            raise NexaError("Enter exactly one IPTV username or MAC address")
        if username and len(username) > 120:
            raise NexaError("IPTV username is too long")
        if mac and not re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", mac):
            raise NexaError("Use a MAC address in the form 00:1A:79:42:56:78")
        payload = self._get("/accounts/lookup", params={"username": username} if username else {"mac": mac})
        account = payload.get("account", payload)
        if not isinstance(account, dict) or account.get("id") is None:
            raise NexaError("NEXA lookup returned no account ID")
        raw_expiry = account.get("exp_date")
        try:
            expiry = datetime.fromtimestamp(int(raw_expiry), tz=timezone.utc).strftime("%d %b %Y %H:%M UTC") if raw_expiry is not None else None
        except (ValueError, TypeError, OverflowError, OSError):
            expiry = None
        return {
            "id": str(account["id"]),
            "username": str(account.get("username") or ""),
            "mac": str(account.get("mac") or ""),
            "type": str(account.get("type") or "Unknown"),
            "status": str(account.get("status") or "Unknown"),
            "is_trial": bool(account.get("is_trial")),
            "exp_date": raw_expiry,
            "expiry_label": expiry or "Unavailable",
        }
