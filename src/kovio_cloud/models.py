"""SQLAlchemy 2.0 ORM mirroring the 11 Supabase tables EXACTLY.

This file is the column-for-column mirror of migration
``001_initial_schema_with_money`` already applied to Supabase. Do not add a
column here without adding it to the database first — the service assumes the
ORM and the live schema are identical.

PK defaults use Python-side ``uuid.uuid4`` rather than the DB's
``gen_random_uuid()`` so that ``create_all`` works on a vanilla local Postgres
(dev) without the pgcrypto extension. The server-side default still exists in
prod and is simply overridden by the client-supplied value — identical result.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# =====================================================================
# 1. organizations
# =====================================================================
class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('advertiser', 'oem', 'admin')", name="organizations_kind_check"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'"), default="active"
    )
    # Advertiser money state
    balance_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    # OEM money state
    pending_payout_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    lifetime_payout_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    stripe_connect_id: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    users: Mapped[list["User"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


# =====================================================================
# 2. fleets
# =====================================================================
class Fleet(Base):
    __tablename__ = "fleets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    region: Mapped[str | None] = mapped_column(String(100))
    blocked_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list
    )
    blocked_advertisers: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'"), default=list
    )
    revenue_share_pct: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("60.00"), default=60.00
    )
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 3. api_keys
# =====================================================================
class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_key_prefix", "key_prefix"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    fleet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 4. robots
# =====================================================================
class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (
        UniqueConstraint("fleet_id", "external_id", name="robots_fleet_id_external_id_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    fleet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'provisioning'"), default="provisioning"
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 5. campaigns
# =====================================================================
class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_review','active','paused','completed','rejected')",
            name="campaigns_status_check",
        ),
        Index("ix_campaigns_org_status", "org_id", "status"),
        Index("ix_campaigns_enabled", "enabled", postgresql_where=text("enabled = TRUE")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    advertiser: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=text("''"), default=""
    )
    creative_url: Mapped[str] = mapped_column(Text, nullable=False)
    targeting: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10"), default=10
    )
    encounter_cap_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300"), default=300
    )
    category: Mapped[str | None] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    # Zero-cost free-tier campaign (an org's first). The spend processor records
    # its impressions but moves no money and skips the balance gate.
    is_promo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'"), default="active"
    )
    fleet_allowlist: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'"), default=list
    )
    fleet_blocklist: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'"), default=list
    )
    # Budget and pricing
    budget_total_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    budget_spent_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    cost_per_impression_cents: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("1.0"), default=1.0
    )
    cost_per_attended_cents: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("5.0"), default=5.0
    )
    cost_per_engagement_cents: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("50.0"), default=50.0
    )
    # Scheduling
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# =====================================================================
# 6. events_raw
# =====================================================================
class EventRaw(Base):
    __tablename__ = "events_raw"
    __table_args__ = (
        Index("ix_events_raw_fleet_ts", "fleet_id", text("timestamp DESC")),
        Index("ix_events_raw_robot_ts", "robot_id", text("timestamp DESC")),
        Index("ix_events_raw_type_ts", "event_type", text("timestamp DESC")),
        Index(
            "ix_events_raw_unprocessed",
            "event_type",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    # event_id is the PRIMARY KEY for idempotency (ON CONFLICT DO NOTHING).
    # No server default — the client (robot) supplies it.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    robot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("robots.id")
    )
    fleet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id"), nullable=False
    )
    robot_external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = _created_at()
    # NULL until the spend processor has costed it.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =====================================================================
# 7. impressions
# =====================================================================
class Impression(Base):
    __tablename__ = "impressions"
    __table_args__ = (
        Index("ix_impressions_campaign_ts", "campaign_id", text("timestamp DESC")),
        Index("ix_impressions_oem_ts", "oem_org_id", text("timestamp DESC")),
        Index("ix_impressions_advertiser_ts", "advertiser_org_id", text("timestamp DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events_raw.event_id"), unique=True, nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    advertiser_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    oem_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    fleet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id"), nullable=False
    )
    robot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("robots.id")
    )
    person_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    attended_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # Closest person to the screen during this ad, in metres, sourced from the
    # concurrent ``scene_observed`` LiDAR sample (``mean_distance_m``). NULL when
    # no scene was available — the audience summary surfaces that as "—".
    min_distance_m: Mapped[float | None] = mapped_column(Numeric(6, 2))
    # Money split for this single impression
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_to_oem_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kovio_share_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 8. engagements
# =====================================================================
class Engagement(Base):
    __tablename__ = "engagements"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('qr_scan','touch','voice','other')", name="engagements_kind_check"
        ),
        Index("ix_engagements_campaign_ts", "campaign_id", text("timestamp DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    impression_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("impressions.id")
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    cost_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 9. transactions
# =====================================================================
class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('advertiser_deposit','advertiser_refund','impression_charge',"
            "'engagement_charge','oem_accrual','oem_payout','platform_share','adjustment')",
            name="transactions_kind_check",
        ),
        Index("ix_transactions_org_ts", "org_id", text("created_at DESC")),
        Index("ix_transactions_kind_ts", "kind", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    # positive credits the balance, negative debits
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(50))
    reference_id: Mapped[str | None] = mapped_column(Text)
    # NOTE: attribute is metadata_ because `metadata` is reserved by SQLAlchemy
    # declarative; the column is still named "metadata".
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = _created_at()


# =====================================================================
# 10. billing_periods
# =====================================================================
class BillingPeriod(Base):
    __tablename__ = "billing_periods"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','invoiced','paid','failed','refunded')",
            name="billing_periods_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stripe_invoice_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'"), default="pending"
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# =====================================================================
# 12. users — maps Supabase Auth accounts to Kovio orgs (web app sessions).
#     Added in migration 002_users_table.
# =====================================================================
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="users_role_check"),
        Index("ix_users_supabase_user_id", "supabase_user_id"),
        Index("ix_users_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    supabase_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'admin'"), default="admin"
    )
    created_at: Mapped[datetime] = _created_at()

    organization: Mapped["Organization"] = relationship(back_populates="users")


# =====================================================================
# 11. payouts
# =====================================================================
class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','sent','paid','failed')", name="payouts_status_check"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stripe_transfer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'"), default="pending"
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
