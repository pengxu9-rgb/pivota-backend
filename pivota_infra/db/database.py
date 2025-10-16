from databases import Database
from sqlalchemy import (
    MetaData, Table, Column, Integer, String, Float, DateTime, JSON, create_engine
)
import datetime
from config.settings import settings

# Normalize and prepare DATABASE_URL
DATABASE_URL = settings.database_url
url_str = str(DATABASE_URL or "").strip()

# Heroku/Render sometimes provide postgres:// which SQLAlchemy doesn't accept
if url_str.startswith("postgres://"):
    url_str = url_str.replace("postgres://", "postgresql://", 1)

lower_url = url_str.lower()
if ("postgresql" in lower_url) or ("postgres://" in lower_url) or (lower_url.startswith("postgres")):
    # For PostgreSQL (including Supabase with pgbouncer)
    # Supabase Transaction Pooler uses pgbouncer which doesn't support prepared statements
    # We need to disable statement cache by passing it to asyncpg
    DATABASE_URL = url_str
    
    # Pass statement_cache_size=0 via server_settings to asyncpg
    # The databases library will forward these to asyncpg.create_pool()
    database = Database(
        DATABASE_URL, 
        min_size=1, 
        max_size=10,
        server_settings={"statement_cache_size": "0"}
    )
else:
    DATABASE_URL = url_str
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

# Normalize postgres scheme for sync engine as well
if sync_url.startswith("postgres://"):
    sync_url = sync_url.replace("postgres://", "postgresql://", 1)

try:
    engine = create_engine(sync_url)
    metadata.create_all(engine)
except Exception as err:
    # Helpful log for dialect/engine issues (e.g., f405)
    # Common fix: ensure scheme is postgresql:// not postgres:// and driver packages installed
    raise
