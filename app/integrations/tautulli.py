from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


class TautulliError(RuntimeError):
    pass


@dataclass
class TautulliUser:
    user_id: str
    username: str | None
    friendly_name: str | None
    email: str | None
    last_seen: datetime | None = None


class TautulliIntegration:
    """Small Tautulli API v2 wrapper used by Share Manager.

    The integration deliberately consumes only customer-management data:
    users, watch-time stats, recent history and current activity.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        if not self.base_url or not self.api_key:
            raise TautulliError("Tautulli URL and API key are required")

    def _call(self, cmd: str, **params: Any) -> Any:
        query = {"apikey": self.api_key, "cmd": cmd, **params}
        try:
            response = httpx.get(f"{self.base_url}/api/v2", params=query, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TautulliError(f"Tautulli request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise TautulliError("Tautulli returned an invalid JSON response") from exc

        envelope = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict):
            raise TautulliError("Unexpected Tautulli API response")
        if envelope.get("result") != "success":
            raise TautulliError(envelope.get("message") or f"Tautulli API command {cmd} failed")
        return envelope.get("data")

    def test(self) -> dict[str, Any]:
        info = self._call("get_tautulli_info") or {}
        users = self.users()
        return {
            "version": info.get("tautulli_version") or info.get("version") or "unknown",
            "user_count": len(users),
        }

    @staticmethod
    def _dt(timestamp: Any) -> datetime | None:
        try:
            value = int(timestamp or 0)
        except (TypeError, ValueError):
            return None
        return datetime.utcfromtimestamp(value) if value > 0 else None

    def users(self) -> list[TautulliUser]:
        raw = self._call("get_users") or []
        result: list[TautulliUser] = []
        for row in raw if isinstance(raw, list) else []:
            try:
                if int(row.get("is_admin") or 0) == 1:
                    continue
            except (TypeError, ValueError):
                pass
            user_id = str(row.get("user_id") or "").strip()
            if not user_id:
                continue
            # get_users does not guarantee last_seen on every Tautulli version.
            result.append(TautulliUser(
                user_id=user_id,
                username=(row.get("username") or None),
                friendly_name=(row.get("friendly_name") or row.get("username") or None),
                email=(row.get("email") or None),
                last_seen=self._dt(row.get("last_seen")),
            ))
        return result

    def user_details(self, user_id: str) -> dict[str, Any]:
        data = self._call("get_user", user_id=user_id, include_last_seen=1) or {}
        return data if isinstance(data, dict) else {}

    def watch_time_stats(self, user_id: str) -> dict[str, int]:
        rows = self._call("get_user_watch_time_stats", user_id=user_id, grouping=1, query_days="30,0") or []
        parsed = {30: {"time": 0, "plays": 0}, 0: {"time": 0, "plays": 0}}
        for row in rows if isinstance(rows, list) else []:
            try:
                days = int(row.get("query_days", -1))
            except (TypeError, ValueError):
                continue
            if days in parsed:
                parsed[days] = {
                    "time": int(row.get("total_time") or 0),
                    "plays": int(row.get("total_plays") or 0),
                }
        return {
            "watch_time_30d": parsed[30]["time"],
            "plays_30d": parsed[30]["plays"],
            "watch_time_lifetime": parsed[0]["time"],
            "plays_lifetime": parsed[0]["plays"],
        }

    def latest_history(self, user_id: str) -> dict[str, Any] | None:
        data = self._call(
            "get_history",
            user_id=user_id,
            grouping=1,
            order_column="date",
            order_dir="desc",
            start=0,
            length=1,
        ) or {}
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows:
            return None
        row = rows[0]
        return {
            "last_streamed_at": self._dt(row.get("date") or row.get("stopped") or row.get("started")),
            "last_title": row.get("full_title") or row.get("title") or None,
        }

    def activity(self) -> list[dict[str, Any]]:
        data = self._call("get_activity") or {}
        sessions = data.get("sessions", []) if isinstance(data, dict) else []
        result = []
        for row in sessions if isinstance(sessions, list) else []:
            result.append({
                "session_key": str(row.get("session_key") or row.get("session_id") or ""),
                "user_id": str(row.get("user_id") or ""),
                "username": row.get("user") or row.get("username") or row.get("friendly_name"),
                "title": row.get("full_title") or row.get("grandparent_title") or row.get("title") or "Unknown title",
                "state": row.get("state") or "playing",
                "media_type": row.get("media_type"),
                "player": row.get("player"),
            })
        return result
