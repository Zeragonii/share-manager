"""Read-only Jellyfin user discovery for account linking."""
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
