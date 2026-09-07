from sqlalchemy import inspect, text
from .db import Base, engine
from . import models  # noqa: F401


Base.metadata.create_all(bind=engine)

# Lightweight in-place migration path for existing v0.1 PostgreSQL/SQLite installs.
# We can move to Alembic later; these additive migrations keep 0.2 a drop-in upgrade.
def add_column_if_missing(table: str, column: str, ddl: str):
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns(table)}
    if column in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


add_column_if_missing("billing_tiers", "grace_period_days", "INTEGER NOT NULL DEFAULT 3")
add_column_if_missing("subscriptions", "current_period_start", "TIMESTAMP NULL")
add_column_if_missing("subscriptions", "grace_until", "TIMESTAMP NULL")
add_column_if_missing("subscriptions", "cancelled_at", "TIMESTAMP NULL")
add_column_if_missing("payments", "subscription_id", "INTEGER NULL REFERENCES subscriptions(id)")
add_column_if_missing("payments", "coverage_start", "TIMESTAMP NULL")
add_column_if_missing("payments", "coverage_end", "TIMESTAMP NULL")
add_column_if_missing("payments", "billing_periods", "INTEGER NULL")
add_column_if_missing("payments", "created_at", "TIMESTAMP NULL")

# Backfill created_at for old payment rows after adding the nullable column.
with engine.begin() as conn:
    conn.execute(text("UPDATE payments SET created_at = COALESCE(created_at, paid_at) WHERE created_at IS NULL"))

print("Database schema ready")
