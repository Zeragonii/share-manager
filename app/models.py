from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plex_username: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    plex_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    credits: Mapped[list["SubscriptionCredit"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    billing_tiers: Mapped[list["BillingTier"]] = relationship(back_populates="package", cascade="all, delete-orphan")
    entitlements: Mapped[list["PackageEntitlement"]] = relationship(back_populates="package", cascade="all, delete-orphan")


# Assigned means the customer still belongs to this tier, even if billing has
# temporarily suspended their access. Cancelled rows are historical only.
ASSIGNED_SUBSCRIPTION_STATES = {"active", "grace", "suspended"}
ACCESS_SUBSCRIPTION_STATES = {"active", "grace"}
# Backwards-compatible alias used by v0.1 code/tests.
CURRENT_SUBSCRIPTION_STATES = ASSIGNED_SUBSCRIPTION_STATES


class BillingTier(Base):
    __tablename__ = "billing_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    interval_unit: Mapped[str] = mapped_column(String(16), default="month")
    interval_count: Mapped[int] = mapped_column(Integer, default=1)
    grace_period_days: Mapped[int] = mapped_column(Integer, default=3)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    package: Mapped[Package] = relationship(back_populates="billing_tiers")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="billing_tier")

    @property
    def current_subscriptions(self):
        return [s for s in self.subscriptions if s.status in ASSIGNED_SUBSCRIPTION_STATES]

    @property
    def current_subscription_count(self) -> int:
        return len(self.current_subscriptions)


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    __table_args__ = (UniqueConstraint("kind", "name"),)


class PackageEntitlement(Base):
    __tablename__ = "package_entitlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"))
    resource_type: Mapped[str] = mapped_column(String(50), default="library")
    resource_id: Mapped[str] = mapped_column(String(255))
    resource_name: Mapped[str] = mapped_column(String(255))
    package: Mapped[Package] = relationship(back_populates="entitlements")
    integration: Mapped[Integration] = relationship()
    __table_args__ = (UniqueConstraint("package_id", "integration_id", "resource_type", "resource_id"),)


class PlexReconcileJob(Base):
    """Durable outstanding work, not a snapshot of the customer's access rules."""
    __tablename__ = "plex_reconcile_jobs"
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    billing_tier_id: Mapped[int] = mapped_column(ForeignKey("billing_tiers.id"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manual_access_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    billing_tier: Mapped[BillingTier] = relationship(back_populates="subscriptions")
    payments: Mapped[list["Payment"]] = relationship(back_populates="subscription")
    credits: Mapped[list["SubscriptionCredit"]] = relationship(back_populates="subscription")


class SubscriptionCredit(Base):
    __tablename__ = "subscription_credits"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    billing_periods: Mapped[int] = mapped_column(Integer)
    coverage_start: Mapped[datetime] = mapped_column(DateTime)
    coverage_end: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    granted_by: Mapped[str] = mapped_column(String(120), default="admin")
    customer: Mapped[Customer] = relationship(back_populates="credits")
    subscription: Mapped[Subscription] = relationship(back_populates="credits")


class PaymentSource(Base):
    __tablename__ = "payment_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    coverage_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    coverage_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billing_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    customer: Mapped[Customer] = relationship(back_populates="payments")
    subscription: Mapped[Subscription | None] = relationship(back_populates="payments")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackupSettings(Base):
    __tablename__ = "backup_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_hour: Mapped[int] = mapped_column(Integer, default=3)
    check_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    retention_daily: Mapped[int] = mapped_column(Integer, default=7)
    retention_weekly: Mapped[int] = mapped_column(Integer, default=4)
    retention_monthly: Mapped[int] = mapped_column(Integer, default=6)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TautulliSettings(Base):
    __tablename__ = "tautulli_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    integration_id: Mapped[int | None] = mapped_column(ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    live_refresh_seconds: Mapped[int] = mapped_column(Integer, default=10)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_matched_count: Mapped[int] = mapped_column(Integer, default=0)
    last_unmatched_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    integration: Mapped[Integration | None] = relationship()


class TautulliActivity(Base):
    __tablename__ = "tautulli_activity"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), unique=True)
    tautulli_user_id: Mapped[str] = mapped_column(String(64))
    tautulli_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_streamed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    watch_time_30d: Mapped[int] = mapped_column(Integer, default=0)
    plays_30d: Mapped[int] = mapped_column(Integer, default=0)
    watch_time_lifetime: Mapped[int] = mapped_column(Integer, default=0)
    plays_lifetime: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    customer: Mapped[Customer] = relationship()


class NotificationEndpoint(Base):
    __tablename__ = "notification_endpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[str] = mapped_column(String(1000))
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    events: Mapped[str] = mapped_column(Text, default="")
    min_severity: Mapped[str] = mapped_column(String(16), default="info")
    due_reminder_days: Mapped[str] = mapped_column(String(120), default="3")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int | None] = mapped_column(ForeignKey("notification_endpoints.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    event: Mapped[str] = mapped_column(String(120))
    event_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[NotificationEndpoint | None] = relationship()
