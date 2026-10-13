"""Table definitions (SQLAlchemy Core). The single source of truth for:

* Alembic revisions (checked against this metadata by tests/test_db_schema.py),
* dbverify.fingerprint(), which discovers money (NUMERIC) and time (TIMESTAMPTZ)
  columns from here, so new tables are covered automatically.

Portability rules (enforced by tests/test_portability.py):
* every table lives in schema ``bridge``; nothing in ``public``;
* only standard Postgres types: money is ``NUMERIC(18,4)``, every timestamp is
  ``TIMESTAMPTZ`` (UTC stored, IST presented);
* no extensions, no references to Supabase-owned schemas.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    text,
)

SCHEMA = "bridge"

Money = Numeric(18, 4)


def Timestamp() -> DateTime:
    return DateTime(timezone=True)


metadata = MetaData(schema=SCHEMA)

meta = Table(
    "meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Timestamp(), nullable=False, server_default=text("now()")),
)

instruments = Table(
    "instruments",
    metadata,
    Column("exchange", Text, nullable=False),  # NSE / BSE
    Column("exchange_segment", Text, primary_key=True),  # NSE_EQ / BSE_EQ / IDX_I
    Column("security_id", Text, primary_key=True),
    Column("trading_symbol", Text, nullable=False),
    Column("instrument", Text, nullable=False),  # EQUITY / INDEX
    Column("series", Text, nullable=False, server_default=""),
    Column("lot_size", Integer, nullable=False, server_default="1"),
    Column("tick_size", Money, nullable=False, server_default="0"),
    Column("name", Text, nullable=False, server_default=""),
    Index("idx_instruments_symbol", "exchange", "trading_symbol"),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("name", Text, nullable=False),
    Column("key_hash", Text, nullable=False, unique=True),
    Column("created_at", Timestamp(), nullable=False, server_default=text("now()")),
    Column("revoked_at", Timestamp(), nullable=True),
)

heartbeat = Table(
    "heartbeat",
    metadata,
    Column("id", SmallInteger, primary_key=True),
    Column("beat_at", Timestamp(), nullable=False),
    Column("source", Text, nullable=False),
    CheckConstraint("id = 1", name="heartbeat_single_row"),
)
