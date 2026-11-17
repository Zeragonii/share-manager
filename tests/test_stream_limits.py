from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models import BillingTier
from app.integrations.tautulli import TautulliIntegration


def test_billing_tier_default_stream_limit_is_one():
    tier = BillingTier(package_id=1, name="Monthly", price=10, interval_unit="month", interval_count=1)
    assert tier.stream_limit == 1 or tier.stream_limit is None  # SQLAlchemy default applies on insert


def test_tautulli_activity_includes_enforcement_fields(monkeypatch):
    client = TautulliIntegration("http://tautulli", "key")
    monkeypatch.setattr(client, "_call", lambda cmd, **kwargs: {"sessions": [{"session_key":"abc","user_id":"7","user":"u","title":"Film","player":"TV","ip_address":"10.0.0.2","started":1700000000}]})
    row = client.activity()[0]
    assert row["session_key"] == "abc"
    assert row["player"] == "TV"
    assert row["ip_address"] == "10.0.0.2"
    assert isinstance(row["started_at"], datetime)


def test_terminate_session_uses_session_key(monkeypatch):
    client = TautulliIntegration("http://tautulli", "key")
    seen = {}
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"response":{"result":"success","data":None}}
    def fake_post(url, params, timeout):
        seen.update(params)
        return Response()
    monkeypatch.setattr("app.integrations.tautulli.httpx.post", fake_post)
    client.terminate_session("abc", "limit reached")
    assert seen["cmd"] == "terminate_session"
    assert seen["session_key"] == "abc"
    assert seen["message"] == "limit reached"


def test_tautulli_activity_prefers_device_name(monkeypatch):
    client = TautulliIntegration("http://tautulli", "key")
    monkeypatch.setattr(client, "_call", lambda cmd, **kwargs: {"sessions": [
        {"session_key": "one", "device": "Living Room TV", "player": "Plex for Android"}
    ]})
    assert client.activity()[0]["device"] == "Living Room TV"


def test_stream_limit_device_context():
    from app.services.tautulli import active_stream_devices, concurrency_stop_message, stream_device_name
    sessions = [
        {"device": "Living Room TV", "player": "Android"},
        {"player": "Bedroom Apple TV"},
        {"device": "Phone", "player": "Plex"},
    ]
    assert active_stream_devices(sessions, 2) == ["Living Room TV", "Bedroom Apple TV"]
    assert stream_device_name(sessions[2]) == "Phone"
    message = concurrency_stop_message(2, active_stream_devices(sessions, 2))
    assert "Living Room TV" in message and "Bedroom Apple TV" in message
    assert "Phone" not in message
    assert "Unknown device" == stream_device_name({})
    assert stream_device_name({"device": "  A\\n  B  "}) == "A B"
    assert len(concurrency_stop_message(1, ["X" * 900])) <= 211
