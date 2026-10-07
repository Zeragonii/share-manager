from __future__ import annotations

from typing import Any
import httpx


class SeerrError(RuntimeError):
    pass


class SeerrIntegration:
    def __init__(self, base_url: str, api_key: str, timeout: float = 8.0):
        self.base_url = (base_url or "").rstrip("/")
        if self.base_url.endswith("/api/v1"):
            self.api_url = self.base_url
        else:
            self.api_url = f"{self.base_url}/api/v1"
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = httpx.request(method, f"{self.api_url}{path}", headers=self.headers, timeout=self.timeout, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                payload = exc.response.json()
                detail = payload.get("message") or payload.get("error") or ""
            except Exception:
                detail = exc.response.text[:200]
            raise SeerrError(f"Seerr returned HTTP {exc.response.status_code}{': ' + detail if detail else ''}") from exc
        except httpx.HTTPError as exc:
            raise SeerrError(f"Could not reach Seerr: {exc}") from exc
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SeerrError("Seerr returned an invalid JSON response") from exc

    def test(self) -> dict[str, Any]:
        users = self.list_users()
        return {"ok": True, "users": len(users)}

    def list_users(self) -> list[dict[str, Any]]:
        """Return every Seerr user, following Seerr's take/skip pagination.

        Seerr defaults collection endpoints to a small page size (commonly 10),
        so a single GET /user silently truncates larger installations.  Keep
        the page size conservative and use pageInfo.results when available;
        fall back to a short-page stop condition for compatible variants.
        """
        page_size = 50
        skip = 0
        users: list[dict[str, Any]] = []

        while True:
            payload = self._request("GET", "/user", params={"take": page_size, "skip": skip})

            # Older/compatible Seerr variants may return a bare list.  There is
            # no pagination metadata in that shape, so treat it as complete.
            if isinstance(payload, list):
                users.extend(row for row in payload if isinstance(row, dict))
                break

            if not isinstance(payload, dict):
                break

            rows = payload.get("results") or payload.get("users") or []
            if not isinstance(rows, list):
                break
            rows = [row for row in rows if isinstance(row, dict)]
            users.extend(rows)

            page_info = payload.get("pageInfo") if isinstance(payload.get("pageInfo"), dict) else {}
            total = page_info.get("results")
            try:
                total = int(total) if total is not None else None
            except (TypeError, ValueError):
                total = None

            skip += len(rows)
            if not rows:
                break
            if total is not None and skip >= total:
                break
            if total is None and len(rows) < page_size:
                break

        return users

    def user(self, user_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/user/{int(user_id)}")
        return payload if isinstance(payload, dict) else {}

    def user_settings(self, user_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/user/{int(user_id)}/settings/main")
        return payload if isinstance(payload, dict) else {}

    def update_user_settings(self, user_id: int, settings: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", f"/user/{int(user_id)}/settings/main", json=settings)
        return payload if isinstance(payload, dict) else {}

    def user_permissions(self, user_id: int) -> int:
        payload = self._request("GET", f"/user/{int(user_id)}/settings/permissions")
        if not isinstance(payload, dict):
            return 0
        try:
            return int(payload.get("permissions") or 0)
        except (TypeError, ValueError):
            return 0

    def update_user_permissions(self, user_id: int, permissions: int) -> int:
        payload = self._request(
            "POST",
            f"/user/{int(user_id)}/settings/permissions",
            json={"permissions": int(permissions)},
        )
        if not isinstance(payload, dict):
            return int(permissions)
        try:
            return int(payload.get("permissions", permissions))
        except (TypeError, ValueError):
            return int(permissions)

    def quota(self, user_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/user/{int(user_id)}/quota")
        return payload if isinstance(payload, dict) else {}

    def requests_for_user(self, user_id: int) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/user/{int(user_id)}/requests")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            rows = payload.get("results") or payload.get("requests") or []
            return rows if isinstance(rows, list) else []
        return []
