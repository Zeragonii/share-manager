"""Jellyfin user discovery and opt-in account provisioning."""
from urllib.parse import urlsplit
import httpx


class JellyfinError(RuntimeError):
    pass


class JellyfinIntegration:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not self.api_key:
            raise JellyfinError("Jellyfin requires a valid HTTP(S) server URL and API key")
        self.timeout = timeout

    def _get(self, path: str):
        try:
            response = httpx.get(f"{self.base_url}{path}", headers={"Authorization": f'MediaBrowser Token="{self.api_key}"', "Accept": "application/json"}, timeout=self.timeout, follow_redirects=False)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JellyfinError(f"Jellyfin API request failed: {exc}") from exc

    def create_user(self, username: str, password: str):
        """Create a local Jellyfin user. Never include passwords in exceptions or logs."""
        try:
            response = httpx.post(
                f"{self.base_url}/Users/New",
                headers={"Authorization": f'MediaBrowser Token="{self.api_key}"',
                         "Accept": "application/json", "Content-Type": "application/json"},
                json={"Name": username, "Password": password},
                timeout=self.timeout, follow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not result.get("Id"):
                raise JellyfinError("Jellyfin user creation returned no user ID; check server before retrying")
            return {"id": str(result["Id"]), "name": str(result.get("Name") or username)}
        except httpx.HTTPStatusError as exc:
            raise JellyfinError(f"Jellyfin refused user creation ({exc.response.status_code}) for {username}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise JellyfinError(f"Jellyfin user creation could not be confirmed for {username}; refresh discovery before retrying") from None

    def users(self):
        payload = self._get("/Users")
        if not isinstance(payload, list):
            raise JellyfinError("Unexpected Jellyfin users response")
        users = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("Id"):
                continue
            policy = item.get("Policy") or {}
            users.append({"id": str(item["Id"]), "name": str(item.get("Name") or "Unnamed"),
                          "admin": bool(policy.get("IsAdministrator")), "disabled": bool(policy.get("IsDisabled"))})
        return users


    def libraries(self):
        """Return actual virtual-library IDs for package grants."""
        payload = self._get("/Library/VirtualFolders")
        if not isinstance(payload, list):
            raise JellyfinError("Unexpected Jellyfin library response")
        return [{"id": str(row["ItemId"]), "name": str(row.get("Name") or "Unnamed"),
                 "type": str(row.get("CollectionType") or "mixed")}
                for row in payload if isinstance(row, dict) and row.get("ItemId")]

    def user(self, user_id: str):
        payload = self._get(f"/Users/{user_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("Policy"), dict):
            raise JellyfinError("Jellyfin did not return a complete user policy; no changes made")
        return payload

    def apply_libraries(self, user_id: str, library_ids: list[str]):
        """Only change folder access; retain every other Jellyfin policy field."""
        data = self.user(user_id)
        policy = dict(data["Policy"])
        if policy.get("IsAdministrator"):
            return {"state": "skipped_admin", "libraries": []}
        desired = sorted(set(str(x) for x in library_ids))
        if policy.get("EnableAllFolders") is False and sorted(policy.get("EnabledFolders") or []) == desired:
            return {"state": "unchanged", "libraries": desired}
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = desired
        try:
            response = httpx.post(f"{self.base_url}/Users/{user_id}/Policy",
                headers={"Authorization": f'MediaBrowser Token="{self.api_key}"',
                         "Accept": "application/json", "Content-Type": "application/json"},
                json=policy, timeout=self.timeout, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"Jellyfin library policy update failed: {type(exc).__name__}") from None
        verified = self.user(user_id)["Policy"]
        if verified.get("EnableAllFolders") is not False or sorted(verified.get("EnabledFolders") or []) != desired:
            raise JellyfinError("Jellyfin returned unexpected library permissions after updating user")
        return {"state": "applied", "libraries": desired}
