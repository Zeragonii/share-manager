import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_accept_postgresql_database_url():
    cfg = Settings(database_url="postgresql+psycopg://user:pass@postgres:5432/sharemanager")
    assert cfg.database_url.startswith("postgresql+")


def test_settings_reject_sqlite_database_url():
    with pytest.raises(ValidationError, match="requires PostgreSQL"):
        Settings(database_url="sqlite:///sharemanager.db")
