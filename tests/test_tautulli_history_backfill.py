from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.integrations.tautulli import TautulliIntegration
from app.models import Customer, Integration, TautulliActivity, TautulliSettings, TautulliWatchHistory, TautulliHistoryLibrarySync
from app.services.tautulli import backfill_watch_history_page, watch_history_backfill_status


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"response": {"result": "success", "message": None, "data": self._data}}


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_history_page_returns_total_and_uses_oldest_first(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return FakeResponse({
            "recordsTotal": 3,
            "recordsFiltered": 3,
            "data": [
                {"row_id": 1, "date": 100, "full_title": "Oldest", "player": "TV"},
                {"row_id": 2, "date": 200, "full_title": "Middle", "player": "Phone"},
            ],
        })

    monkeypatch.setattr("app.integrations.tautulli.httpx.get", fake_get)
    client = TautulliIntegration("http://tautulli", "key")
    page = client.history_page("42", length=2, start=0, order_dir="asc")
    assert page["total"] == 3
    assert page["raw_count"] == 2
    assert [row["source_row_id"] for row in page["rows"]] == ["1", "2"]
    assert seen["grouping"] == 0
    assert seen["order_dir"] == "asc"


def test_backfill_is_resumable_and_completes(monkeypatch):
    db = make_db()
    customer = Customer(name="Backfill User", plex_user_id="42")
    integration = Integration(kind="tautulli", name="T", enabled=True, base_url="http://tautulli", secret="key")
    db.add_all([customer, integration]); db.flush()
    db.add(TautulliSettings(id=1, integration_id=integration.id, sync_interval_minutes=30, live_refresh_seconds=10))
    db.add(TautulliActivity(customer_id=customer.id, tautulli_user_id="42"))
    db.commit()

    pages = {
        0: {"rows": [
            {"source_row_id":"1", "watched_at":datetime(2020,1,1), "title":"A", "library_name":"Movies", "section_id":"1", "media_type":"movie", "platform":"Roku", "player":"TV", "duration_seconds":100, "watched_status":1},
            {"source_row_id":"2", "watched_at":datetime(2020,1,2), "title":"B", "library_name":"Movies", "section_id":"1", "media_type":"movie", "platform":"Roku", "player":"TV", "duration_seconds":200, "watched_status":1},
        ], "total":3, "raw_count":2, "start":0},
        2: {"rows": [
            {"source_row_id":"3", "watched_at":datetime(2020,1,3), "title":"C", "library_name":"Movies", "section_id":"1", "media_type":"movie", "platform":"Android", "player":"Phone", "duration_seconds":300, "watched_status":1},
        ], "total":3, "raw_count":1, "start":2},
    }

    monkeypatch.setattr(TautulliIntegration, "libraries", lambda self: [{"section_id":"1", "section_name":"Movies"}])
    monkeypatch.setattr(
        TautulliIntegration, "history_page",
        lambda self, user_id, length, start, order_dir, section_id=None, library_name=None: pages[start],
    )
    first = backfill_watch_history_page(db, page_size=2, now=datetime(2026,9,10,10,0))
    assert first["offset"] == 2
    assert first["complete"] is False
    assert db.query(TautulliWatchHistory).count() == 2

    second = backfill_watch_history_page(db, page_size=2, now=datetime(2026,9,10,10,1))
    assert second["offset"] == 3
    assert second["complete"] is True
    assert db.query(TautulliWatchHistory).count() == 3
    assert {row.library_name for row in db.query(TautulliWatchHistory).all()} == {"Movies"}

    status = watch_history_backfill_status(db)
    assert status["customers_complete"] == 1
    assert status["customers_total"] == 1
    assert status["known_total"] == 3
    assert status["progress_rows"] == 3
    assert status["complete"] is True


def test_history_page_uses_library_context_when_tautulli_row_omits_it(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        assert params["section_id"] == "7"
        return FakeResponse({
            "recordsTotal": 1,
            "recordsFiltered": 1,
            "data": [{"row_id": 9, "date": 300, "full_title": "Example", "player": "Chrome"}],
        })

    monkeypatch.setattr("app.integrations.tautulli.httpx.get", fake_get)
    client = TautulliIntegration("http://tautulli", "key")
    page = client.history_page("42", section_id="7", library_name="TV Shows")
    assert page["rows"][0]["library_name"] == "TV Shows"
    assert page["rows"][0]["section_id"] == "7"


def test_force_full_resync_clears_only_history_cache_and_checkpoints():
    from app.models import TautulliHistoryLibrarySync
    from app.services.tautulli import force_full_watch_history_resync

    db = make_db()
    customer = Customer(name="Reset User", plex_user_id="42")
    integration = Integration(kind="tautulli", name="T", enabled=True, base_url="http://tautulli", secret="key")
    db.add_all([customer, integration]); db.flush()
    db.add(TautulliSettings(id=1, integration_id=integration.id, sync_interval_minutes=30, live_refresh_seconds=10))
    activity = TautulliActivity(
        customer_id=customer.id,
        tautulli_user_id="42",
        history_backfill_complete=True,
        history_backfill_offset=500,
        history_backfill_total=500,
        history_backfill_started_at=datetime(2026, 9, 10, 10, 0),
        history_backfill_updated_at=datetime(2026, 9, 10, 10, 5),
        history_backfill_error="old error",
    )
    db.add(activity); db.flush()
    db.add(TautulliHistoryLibrarySync(
        customer_id=customer.id, tautulli_user_id="42", section_id="1",
        library_name="TV Shows", offset=500, total=500, complete=True,
    ))
    db.add(TautulliWatchHistory(
        customer_id=customer.id, tautulli_user_id="42", source_row_id="abc",
        watched_at=datetime(2026, 9, 10, 10, 0), title="Episode",
        library_name="TV Shows", section_id="1", media_type="episode",
        duration_seconds=1200,
    ))
    db.commit()

    result = force_full_watch_history_resync(db, now=datetime(2026, 9, 10, 12, 0))
    db.commit()

    assert result == {"deleted_history_rows": 1, "deleted_checkpoints": 1, "customers_reset": 1}
    assert db.query(TautulliWatchHistory).count() == 0
    assert db.query(TautulliHistoryLibrarySync).count() == 0
    assert db.query(Customer).count() == 1
    refreshed = db.query(TautulliActivity).filter(TautulliActivity.customer_id == customer.id).one()
    assert refreshed.history_backfill_complete is False
    assert refreshed.history_backfill_offset == 0
    assert refreshed.history_backfill_total is None
    assert refreshed.history_backfill_started_at is None
    assert refreshed.history_backfill_error is None


def test_backfill_status_hides_partial_denominator_until_all_targets_measured():
    db = make_db()
    customer = Customer(name="Progress User", plex_user_id="42")
    db.add(customer); db.flush()
    db.add_all([
        TautulliHistoryLibrarySync(customer_id=customer.id, tautulli_user_id="42", section_id="1", library_name="TV", offset=100, total=100, complete=True),
        TautulliHistoryLibrarySync(customer_id=customer.id, tautulli_user_id="42", section_id="2", library_name="Movies", offset=0, total=None, complete=False),
    ])
    db.commit()

    status = watch_history_backfill_status(db)
    assert status["known_total"] == 100
    assert status["progress_rows"] == 100
    assert status["targets_measured"] == 1
    assert status["targets_total"] == 2
    assert status["total_is_final"] is False

    movie = db.query(TautulliHistoryLibrarySync).filter_by(section_id="2").one()
    movie.total = 900
    movie.offset = 250
    db.commit()

    status = watch_history_backfill_status(db)
    assert status["known_total"] == 1000
    assert status["progress_rows"] == 350
    assert status["targets_measured"] == 2
    assert status["targets_total"] == 2
    assert status["total_is_final"] is True
