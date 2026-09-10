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
    is_admin: bool = False


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

    def users(self, *, include_admin: bool = False) -> list[TautulliUser]:
        raw = self._call("get_users") or []
        result: list[TautulliUser] = []
        for row in raw if isinstance(raw, list) else []:
            try:
                is_admin = int(row.get("is_admin") or 0) == 1
            except (TypeError, ValueError):
                is_admin = False
            if is_admin and not include_admin:
                continue
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
                is_admin=is_admin,
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

    def history_page(
        self, user_id: str, *, length: int = 250, start: int = 0, order_dir: str = "desc"
    ) -> dict[str, Any]:
        """Return one normalized page of viewing history plus pagination metadata."""
        if order_dir not in {"asc", "desc"}:
            raise ValueError("order_dir must be 'asc' or 'desc'")
        data = self._call(
            "get_history",
            user_id=user_id,
            grouping=0,
            order_column="date",
            order_dir=order_dir,
            start=max(0, int(start)),
            length=max(1, min(1000, int(length))),
        ) or {}
        raw_rows = data.get("data", []) if isinstance(data, dict) else []
        rows: list[dict[str, Any]] = []
        for row in raw_rows if isinstance(raw_rows, list) else []:
            watched_at = self._dt(row.get("date") or row.get("stopped") or row.get("started"))
            if not watched_at:
                continue
            source_row_id = str(row.get("row_id") or row.get("reference_id") or row.get("history_id") or "").strip()
            if not source_row_id:
                source_row_id = ":".join([
                    str(row.get("rating_key") or ""),
                    str(row.get("started") or row.get("date") or ""),
                    str(row.get("player") or ""),
                ])
            try:
                duration = int(row.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            try:
                watched_status = int(row.get("watched_status")) if row.get("watched_status") is not None else None
            except (TypeError, ValueError):
                watched_status = None
            rows.append({
                "source_row_id": source_row_id[:64],
                "watched_at": watched_at,
                "title": row.get("full_title") or row.get("title") or "Unknown title",
                "library_name": row.get("section_name") or row.get("library_name") or None,
                "section_id": str(row.get("section_id") or "") or None,
                "media_type": row.get("media_type") or None,
                "platform": row.get("platform") or None,
                "player": row.get("player") or None,
                "duration_seconds": max(0, duration),
                "watched_status": watched_status,
            })
        total = len(raw_rows)
        if isinstance(data, dict):
            for key in ("recordsFiltered", "recordsTotal"):
                if key in data:
                    try:
                        total = max(0, int(data[key]))
                        break
                    except (TypeError, ValueError):
                        pass
        return {
            "rows": rows,
            "total": total,
            "raw_count": len(raw_rows) if isinstance(raw_rows, list) else 0,
            "start": max(0, int(start)),
        }

    def history(self, user_id: str, *, length: int = 250, start: int = 0) -> list[dict[str, Any]]:
        """Return normalized viewing-history rows for one Plex/Tautulli user."""
        return self.history_page(user_id, length=length, start=start, order_dir="desc")["rows"]

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
                "ip_address": row.get("ip_address") or row.get("ip"),
                "started_at": self._dt(row.get("started")),
            })
        return result

    def terminate_session(self, session_key: str, message: str) -> Any:
        if not (session_key or "").strip():
            raise TautulliError("Cannot terminate a session without a session key")
        query = {"apikey": self.api_key, "cmd": "terminate_session", "session_key": session_key, "message": message}
        try:
            response = httpx.post(f"{self.base_url}/api/v2", params=query, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TautulliError(f"Tautulli termination request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise TautulliError("Tautulli returned an invalid termination response") from exc
        envelope = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict) or envelope.get("result") != "success":
            message_text = envelope.get("message") if isinstance(envelope, dict) else None
            raise TautulliError(message_text or "Tautulli could not terminate the session")
        return envelope.get("data")
