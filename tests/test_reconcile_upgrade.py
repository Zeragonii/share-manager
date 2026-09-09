import runpy
from datetime import datetime

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.db as database
from app.models import Customer, PlexReconcileJob


def test_startup_adds_retry_table_without_changing_existing_customers(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    # Reproduce a pre-v0.5.2 database with all existing tables but no retry table.
    database.Base.metadata.create_all(engine)
    PlexReconcileJob.__table__.drop(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        customer = Customer(name="Existing", status="suspended", plex_username="existing",
                            created_at=datetime(2026, 1, 1))
        db.add(customer)
        db.commit()
        customer_id = customer.id
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    for _ in range(2):  # Initialization must remain safe on each restart.
        runpy.run_module("app.init_db")
    assert "plex_reconcile_jobs" in inspect(engine).get_table_names()
    with factory() as db:
        customer = db.get(Customer, customer_id)
        assert customer.name == "Existing" and customer.status == "suspended"
        assert customer.created_at == datetime(2026, 1, 1)
        assert db.query(PlexReconcileJob).count() == 0
    engine.dispose()
