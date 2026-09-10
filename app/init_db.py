from sqlalchemy import inspect, text
from .db import Base, engine
from .config import settings
from . import models  # noqa: F401


Base.metadata.create_all(bind=engine)

# Lightweight in-place migration path for existing PostgreSQL installs.
# We can move to Alembic later; these additive migrations keep 0.2 a drop-in upgrade.
def add_column_if_missing(table: str, column: str, ddl: str):
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns(table)}
    if column in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


add_column_if_missing("customers", "archived", "BOOLEAN NOT NULL DEFAULT FALSE")
add_column_if_missing("customers", "archived_at", "TIMESTAMP NULL")
add_column_if_missing("customers", "portal_enabled", "BOOLEAN NOT NULL DEFAULT FALSE")
add_column_if_missing("customers", "portal_username", "VARCHAR(120) NULL")
add_column_if_missing("customers", "portal_password_hash", "TEXT NULL")
add_column_if_missing("customers", "portal_session_version", "INTEGER NOT NULL DEFAULT 1")
add_column_if_missing("customers", "portal_enabled_at", "TIMESTAMP NULL")
add_column_if_missing("customers", "portal_disabled_at", "TIMESTAMP NULL")
add_column_if_missing("customers", "portal_last_login_at", "TIMESTAMP NULL")
with engine.begin() as conn:
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_customers_portal_username_lower ON customers (lower(portal_username)) WHERE portal_username IS NOT NULL"))
add_column_if_missing("billing_tiers", "grace_period_days", "INTEGER NOT NULL DEFAULT 3")
add_column_if_missing("billing_tiers", "stream_limit", "INTEGER NOT NULL DEFAULT 1")
add_column_if_missing("tautulli_settings", "admin_user_ids", "TEXT NULL")
add_column_if_missing("tautulli_activity", "history_backfill_complete", "BOOLEAN NOT NULL DEFAULT FALSE")
add_column_if_missing("tautulli_activity", "history_backfill_offset", "INTEGER NOT NULL DEFAULT 0")
add_column_if_missing("tautulli_activity", "history_backfill_total", "INTEGER NULL")
add_column_if_missing("tautulli_activity", "history_backfill_started_at", "TIMESTAMP NULL")
add_column_if_missing("tautulli_activity", "history_backfill_updated_at", "TIMESTAMP NULL")
add_column_if_missing("tautulli_activity", "history_backfill_error", "TEXT NULL")
add_column_if_missing("subscriptions", "current_period_start", "TIMESTAMP NULL")
add_column_if_missing("subscriptions", "manual_access_end", "TIMESTAMP NULL")
add_column_if_missing("subscriptions", "grace_until", "TIMESTAMP NULL")
add_column_if_missing("subscriptions", "cancelled_at", "TIMESTAMP NULL")
add_column_if_missing("payments", "subscription_id", "INTEGER NULL REFERENCES subscriptions(id)")
add_column_if_missing("payments", "coverage_start", "TIMESTAMP NULL")
add_column_if_missing("payments", "coverage_end", "TIMESTAMP NULL")
add_column_if_missing("payments", "billing_periods", "INTEGER NULL")
add_column_if_missing("payments", "prior_state_captured", "BOOLEAN NOT NULL DEFAULT FALSE")
add_column_if_missing("payments", "prior_started_at", "TIMESTAMP NULL")
add_column_if_missing("payments", "prior_period_start", "TIMESTAMP NULL")
add_column_if_missing("payments", "prior_period_end", "TIMESTAMP NULL")
add_column_if_missing("payments", "prior_grace_until", "TIMESTAMP NULL")
add_column_if_missing("payments", "prior_subscription_status", "VARCHAR(32) NULL")
add_column_if_missing("payments", "prior_customer_status", "VARCHAR(32) NULL")
add_column_if_missing("payments", "created_at", "TIMESTAMP NULL")
add_column_if_missing("payments", "voided_at", "TIMESTAMP NULL")
add_column_if_missing("payments", "voided_by", "VARCHAR(120) NULL")
_notification_columns = {c["name"] for c in inspect(engine).get_columns("notification_endpoints")}
_notification_due_days_was_missing = "due_reminder_days" not in _notification_columns
add_column_if_missing("notification_endpoints", "due_reminder_days", "VARCHAR(120) NOT NULL DEFAULT '3'")
if _notification_due_days_was_missing:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE notification_endpoints SET due_reminder_days = :days"),
            {"days": str(max(0, int(settings.notification_due_soon_days)))},
        )

# Backfill created_at for old payment rows after adding the nullable column.
with engine.begin() as conn:
    conn.execute(text("UPDATE payments SET created_at = COALESCE(created_at, paid_at) WHERE created_at IS NULL"))

print("Database schema ready")


# Seed editable payment-source choices. Payments keep their source text so historical
# ledger entries remain unchanged if a source is later renamed or archived.
from .db import SessionLocal
from .models import BackupSettings, Payment, PaymentSource

db = SessionLocal()
try:
    defaults = ["manual", "bank_transfer", "cash", "paypal", "stripe", "other"]
    historical = [row[0] for row in db.query(Payment.source).distinct().all() if row[0]]
    existing = {row.name.lower() for row in db.query(PaymentSource).all()}
    for name in defaults + historical:
        if name.lower() not in existing:
            db.add(PaymentSource(name=name, active=True))
            existing.add(name.lower())

    # v0.4.1: move backup automation policy into the database. Environment
    # variables remain first-run defaults so existing deployments migrate cleanly.
    if db.get(BackupSettings, 1) is None:
        db.add(BackupSettings(
            id=1,
            enabled=True,
            schedule_hour=max(0, min(23, int(settings.backup_schedule_hour))),
            check_interval_minutes=max(1, int(settings.backup_check_interval_minutes)),
            retention_daily=max(1, int(settings.backup_retention_daily)),
            retention_weekly=max(0, int(settings.backup_retention_weekly)),
            retention_monthly=max(0, int(settings.backup_retention_monthly)),
        ))
    db.commit()
finally:
    db.close()
