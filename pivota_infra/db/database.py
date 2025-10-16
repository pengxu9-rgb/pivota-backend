from databases import Database
from sqlalchemy import (
    MetaData, Table, Column, Integer, String, Float, DateTime, JSON, create_engine
)
import datetime
from config.settings import settings

DATABASE_URL = settings.database_url

# Disable prepared statement cache for PostgreSQL/pgbouncer compatibility
if "postgresql" in str(DATABASE_URL).lower():
    # Add statement_cache_size=0 to URL for pgbouncer compatibility
    url_str = str(DATABASE_URL)
    if "?" in url_str:
        DATABASE_URL = url_str + "&statement_cache_size=0"
    else:
        DATABASE_URL = url_str + "?statement_cache_size=0"
    database = Database(DATABASE_URL, min_size=1, max_size=5)
else:
    # For SQLite or other databases
    database = Database(DATABASE_URL, min_size=1, max_size=1)
metadata = MetaData()

transactions = Table(
    "transactions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", String, unique=True, index=True),
    Column("merchant_id", String, index=True),
    Column("amount", Float),
    Column("currency", String(8)),
    Column("status", String(32), default="pending"),
    Column("psp", String(32), nullable=True),
    Column("psp_txn_id", String(128), nullable=True),
    Column("created_at", DateTime, default=datetime.datetime.utcnow),
    Column("meta", JSON, nullable=True)
)

raw_url = str(DATABASE_URL)
# Build a synchronous URL for SQLAlchemy engine (remove +aiosqlite driver)
sync_url = raw_url.replace("+aiosqlite", "")
engine = create_engine(sync_url)
metadata.create_all(engine)
