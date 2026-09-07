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

class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    billing_tiers: Mapped[list["BillingTier"]] = relationship(back_populates="package", cascade="all, delete-orphan")
    entitlements: Mapped[list["PackageEntitlement"]] = relationship(back_populates="package", cascade="all, delete-orphan")

CURRENT_SUBSCRIPTION_STATES = {"active", "grace"}

class BillingTier(Base):
    __tablename__ = "billing_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    price: Mapped[float] = mapped_column(Numeric(10,2))
    interval_unit: Mapped[str] = mapped_column(String(16), default="month")
    interval_count: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    package: Mapped[Package] = relationship(back_populates="billing_tiers")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="billing_tier")

    @property
    def current_subscriptions(self):
        """Subscriptions that currently represent an assigned billing tier.

        Historical/cancelled rows are deliberately retained for audit/history but must
        not be treated as live package usage.
        """
        return [s for s in self.subscriptions if s.status in CURRENT_SUBSCRIPTION_STATES]

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

class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    billing_tier_id: Mapped[int] = mapped_column(ForeignKey("billing_tiers.id"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    billing_tier: Mapped[BillingTier] = relationship(back_populates="subscriptions")

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    amount: Mapped[float] = mapped_column(Numeric(10,2))
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
