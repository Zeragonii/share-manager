from datetime import datetime
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
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
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    portal_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    portal_username: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    portal_password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    portal_session_version: Mapped[int] = mapped_column(Integer, default=1)
    portal_enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    portal_disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    portal_last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    portal_last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    referral_code: Mapped[str | None] = mapped_column(String(5), nullable=True, index=True)
    referrer_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    referral_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    credits: Mapped[list["SubscriptionCredit"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    support_tickets: Mapped[list["SupportTicket"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    referrer: Mapped["Customer"] = relationship(remote_side="Customer.id", foreign_keys=[referrer_customer_id], uselist=False)


class PortalDailyMetric(Base):
    __tablename__ = "portal_daily_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    metric_date: Mapped[datetime] = mapped_column(Date, index=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    session_count: Mapped[int] = mapped_column(Integer, default=0)
    account_views: Mapped[int] = mapped_column(Integer, default=0)
    activity_views: Mapped[int] = mapped_column(Integer, default=0)
    support_views: Mapped[int] = mapped_column(Integer, default=0)
    history_views: Mapped[int] = mapped_column(Integer, default=0)
    news_views: Mapped[int] = mapped_column(Integer, default=0)
    request_clicks: Mapped[int] = mapped_column(Integer, default=0)
    customer: Mapped["Customer"] = relationship()
    __table_args__ = (UniqueConstraint("customer_id", "metric_date", name="uq_portal_daily_customer_date"),)


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
    stream_limit: Mapped[int] = mapped_column(Integer, default=1)
    referral_credits: Mapped[int] = mapped_column(Integer, default=0)
    referral_redeem_cost: Mapped[int] = mapped_column(Integer, default=0)
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


class ReferralSettings(Base):
    __tablename__ = "referral_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credits_per_reward: Mapped[int] = mapped_column(Integer, default=10)
    reward_periods: Mapped[int] = mapped_column(Integer, default=1)


class ReferralCreditEntry(Base):
    __tablename__ = "referral_credit_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    referred_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"), nullable=True, index=True)
    billing_tier_id: Mapped[int | None] = mapped_column(ForeignKey("billing_tiers.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="earn")
    credits: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    customer: Mapped["Customer"] = relationship(foreign_keys=[customer_id])
    referred_customer: Mapped["Customer"] = relationship(foreign_keys=[referred_customer_id])
    payment: Mapped["Payment"] = relationship("Payment")
    billing_tier: Mapped["BillingTier"] = relationship("BillingTier")
    __table_args__ = (UniqueConstraint("payment_id", "kind", name="uq_referral_payment_kind"),)


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
    prior_state_captured: Mapped[bool] = mapped_column(Boolean, default=False)
    prior_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prior_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prior_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prior_grace_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prior_subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prior_customer_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
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


class RequestsPlatformSettings(Base):
    __tablename__ = "requests_platform_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(120), default="Seerr")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    button_label: Mapped[str] = mapped_column(String(80), default="Request Content")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NewsBanner(Base):
    __tablename__ = "news_banners"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(24), default="info", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


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
    admin_user_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    history_backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    history_backfill_offset: Mapped[int] = mapped_column(Integer, default=0)
    history_backfill_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    history_backfill_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_backfill_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_backfill_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer: Mapped[Customer] = relationship()


class TautulliWatchHistory(Base):
    __tablename__ = "tautulli_watch_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    tautulli_user_id: Mapped[str] = mapped_column(String(64), index=True)
    source_row_id: Mapped[str] = mapped_column(String(64))
    watched_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    title: Mapped[str] = mapped_column(Text)
    library_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    player: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    watched_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    customer: Mapped[Customer] = relationship()
    __table_args__ = (UniqueConstraint("customer_id", "source_row_id", name="uq_tautulli_watch_customer_row"),)


class TautulliHistoryLibrarySync(Base):
    __tablename__ = "tautulli_history_library_sync"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    tautulli_user_id: Mapped[str] = mapped_column(String(64), index=True)
    section_id: Mapped[str] = mapped_column(String(64), index=True)
    library_name: Mapped[str] = mapped_column(String(255))
    offset: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer: Mapped[Customer] = relationship()
    __table_args__ = (UniqueConstraint("customer_id", "section_id", name="uq_tautulli_history_library_sync"),)


class StreamLimitEvent(Base):
    __tablename__ = "stream_limit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    billing_tier_id: Mapped[int | None] = mapped_column(ForeignKey("billing_tiers.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    allowed_streams: Mapped[int] = mapped_column(Integer)
    detected_streams: Mapped[int] = mapped_column(Integer)
    session_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    player: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer: Mapped[Customer] = relationship()
    billing_tier: Mapped[BillingTier | None] = relationship()


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
    notification_event_id: Mapped[int | None] = mapped_column(ForeignKey("notification_events.id", ondelete="SET NULL"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="endpoint")
    push_subscription_id: Mapped[int | None] = mapped_column(ForeignKey("push_subscriptions.id", ondelete="SET NULL"), nullable=True)
    recipient_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recipient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    final_failure: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    endpoint: Mapped[NotificationEndpoint | None] = relationship()


class NotificationEvent(Base):
    """Canonical application event that can be delivered through one or more channels."""
    __tablename__ = "notification_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(120), index=True)
    event_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    customer: Mapped[Customer | None] = relationship()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16), index=True)  # admin | customer
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    customer: Mapped[Customer | None] = relationship()


class CustomerNotificationPreference(Base):
    __tablename__ = "customer_notification_preferences"
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    events: Mapped[str] = mapped_column(Text, default="*")
    ticket_notifications_default: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    customer: Mapped[Customer] = relationship()


class AdminNotificationPreference(Base):
    __tablename__ = "admin_notification_preferences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    events: Mapped[str] = mapped_column(Text, default="*")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)




class ScheduledCustomerBroadcast(Base):
    __tablename__ = "scheduled_customer_broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    destination: Mapped[str] = mapped_column(String(255), default="/portal")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default="scheduled", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("notification_events.id", ondelete="SET NULL"), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event: Mapped[NotificationEvent | None] = relationship()


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    subject: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(24), default="normal", index=True)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    admin_unread: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    customer_unread: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    customer: Mapped[Customer] = relationship(back_populates="support_tickets")
    messages: Mapped[list["SupportTicketMessage"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", order_by="SupportTicketMessage.created_at")
    attachments: Mapped[list["SupportTicketAttachment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    author_type: Mapped[str] = mapped_column(String(20), index=True)  # customer | admin | internal
    author_label: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    visible_to_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")
    attachments: Mapped[list["SupportTicketAttachment"]] = relationship(back_populates="message", order_by="SupportTicketAttachment.id")


class SupportTicketAttachment(Base):
    __tablename__ = "support_ticket_attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("support_ticket_messages.id", ondelete="CASCADE"), nullable=True, index=True)
    draft_token: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    uploader_type: Mapped[str] = mapped_column(String(20), index=True)
    uploader_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_filename: Mapped[str] = mapped_column(String(100), unique=True)
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ticket: Mapped[SupportTicket | None] = relationship(back_populates="attachments")
    message: Mapped[SupportTicketMessage | None] = relationship(back_populates="attachments")


class NotificationPlatformSettings(Base):
    __tablename__ = "notification_platform_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    vapid_private_key: Mapped[str] = mapped_column(Text)
    vapid_public_key: Mapped[str] = mapped_column(Text)
    vapid_subject: Mapped[str] = mapped_column(String(255), default="mailto:admin@share-manager.local")
    ticket_events_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
