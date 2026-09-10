from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.integrations.tautulli import TautulliIntegration, TautulliUser
from app.models import Customer, TautulliActivity
from app.services.tautulli import dashboard_usage, match_customer


class FakeResponse:
    def __init__(self, data):
        self._data = data
    def raise_for_status(self):
        return None
    def json(self):
        return {"response": {"result": "success", "message": None, "data": self._data}}


def test_tautulli_users_and_watch_stats(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        cmd = params["cmd"]
        if cmd == "get_users":
            return FakeResponse([{"user_id": 42, "username": "snow", "friendly_name": "Jon Snow", "email": "jon@example.com"}])
        if cmd == "get_user_watch_time_stats":
            return FakeResponse([
                {"query_days": 30, "total_time": 3600, "total_plays": 4},
                {"query_days": 0, "total_time": 7200, "total_plays": 9},
            ])
        raise AssertionError(cmd)

    monkeypatch.setattr("app.integrations.tautulli.httpx.get", fake_get)
    client = TautulliIntegration("http://tautulli:8181", "key")
    users = client.users()
    assert users[0].user_id == "42"
    assert users[0].email == "jon@example.com"
    stats = client.watch_time_stats("42")
    assert stats == {
        "watch_time_30d": 3600,
        "plays_30d": 4,
        "watch_time_lifetime": 7200,
        "plays_lifetime": 9,
    }


def test_tautulli_latest_history_and_activity(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if params["cmd"] == "get_history":
            return FakeResponse({"data": [{"date": 1_700_000_000, "full_title": "The Expanse - CQB"}]})
        if params["cmd"] == "get_activity":
            return FakeResponse({"sessions": [{"session_key": "abc", "user_id": 7, "user": "amos", "full_title": "Dune", "state": "playing"}]})
        raise AssertionError(params["cmd"])

    monkeypatch.setattr("app.integrations.tautulli.httpx.get", fake_get)
    client = TautulliIntegration("http://tautulli:8181", "key")
    history = client.latest_history("7")
    assert history["last_title"] == "The Expanse - CQB"
    assert history["last_streamed_at"] == datetime.utcfromtimestamp(1_700_000_000)
    sessions = client.activity()
    assert sessions[0]["user_id"] == "7"
    assert sessions[0]["title"] == "Dune"


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_matching_prefers_plex_user_id_then_identity():
    db = make_db()
    by_id = Customer(name="By ID", plex_username="oldname", plex_user_id="99")
    by_name = Customer(name="By Name", plex_username="snow", email="jon@example.com")
    db.add_all([by_id, by_name]); db.commit()
    assert match_customer(db, TautulliUser("99", "different", None, None)).id == by_id.id
    assert match_customer(db, TautulliUser("123", "snow", "Jon Snow", "other@example.com")).id == by_name.id


def test_dashboard_usage_summarises_cached_activity():
    db = make_db()
    now = datetime(2026, 9, 8, 12, 0)
    customers = [Customer(name=f"C{i}") for i in range(4)]
    db.add_all(customers); db.flush()
    db.add_all([
        TautulliActivity(customer_id=customers[0].id, tautulli_user_id="1", last_streamed_at=now - timedelta(days=1), watch_time_30d=3600),
        TautulliActivity(customer_id=customers[1].id, tautulli_user_id="2", last_streamed_at=now - timedelta(days=20), watch_time_30d=1800),
        TautulliActivity(customer_id=customers[2].id, tautulli_user_id="3", last_streamed_at=now - timedelta(days=120), watch_time_30d=0),
        TautulliActivity(customer_id=customers[3].id, tautulli_user_id="4", last_streamed_at=None, watch_time_30d=0),
    ])
    db.commit()
    summary = dashboard_usage(db, now=now)
    assert summary["active_7d"] == 1
    assert summary["active_30d"] == 2
    assert summary["inactive_90d"] == 1
    assert summary["never"] == 1
    assert summary["watch_time_30d"] == 5400
    assert summary["average_30d"] == 2700


def test_customer_session_termination_verifies_owner(monkeypatch):
    from app.models import Integration, TautulliSettings
    from app.services.tautulli import terminate_customer_session
    db = make_db()
    customer = Customer(name="Portal User", plex_user_id="42")
    integration = Integration(kind="tautulli", name="T", enabled=True, base_url="http://tautulli", secret="key")
    db.add_all([customer, integration]); db.flush()
    db.add(TautulliSettings(id=1, integration_id=integration.id, sync_interval_minutes=30, live_refresh_seconds=10))
    db.commit()
    monkeypatch.setattr(TautulliIntegration, "activity", lambda self: [{"session_key":"mine","user_id":"42","title":"Film"}, {"session_key":"theirs","user_id":"99","title":"Other"}])
    seen = {}
    monkeypatch.setattr(TautulliIntegration, "terminate_session", lambda self, key, message: seen.update({"key":key,"message":message}))
    terminate_customer_session(db, customer, "mine")
    assert seen["key"] == "mine"
    try:
        terminate_customer_session(db, customer, "theirs")
        assert False, "other user's session should be rejected"
    except PermissionError:
        pass
