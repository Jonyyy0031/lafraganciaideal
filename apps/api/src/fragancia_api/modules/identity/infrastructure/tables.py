from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "identity"

users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(80), nullable=False),
    Column("role", String(20), nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("password_changed_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    # Same schema: the module-boundary rule allows this foreign key.
    Column("user_id", Uuid, ForeignKey(f"{SCHEMA}.users.id"), nullable=False, index=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("user_agent", String(255), nullable=True),
    Column("ip", String(45), nullable=True),
    schema=SCHEMA,
)

login_throttle = Table(
    "login_throttle",
    metadata,
    Column("key", String(330), primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("window_started_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("email", String(254), nullable=False, index=True),
    Column("name", String(80), nullable=False),
    Column("invited_by", Uuid, ForeignKey(f"{SCHEMA}.users.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("token_hash", String(64), nullable=True, unique=True),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    schema=SCHEMA,
)

password_resets = Table(
    "password_resets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey(f"{SCHEMA}.users.id"), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("token_hash", String(64), nullable=True, unique=True),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    schema=SCHEMA,
)

USER_EMAIL_UNIQUE = "uq_users_email"
