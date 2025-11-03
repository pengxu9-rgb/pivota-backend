from databases import Database
from sqlalchemy import (
    MetaData, Table, Column, Integer, String, Float, DateTime, JSON, create_engine
)
import datetime
import os
from config.settings import settings

# PostgreSQL ONLY - No SQLite support
DATABASE_URL = settings.database_url or os.getenv("DATABASE_URL", "")

# Validate that DATABASE_URL exists
if not DATABASE_URL:
    raise RuntimeError(
        "❌ DATABASE_URL is required!\n"
        "Please set DATABASE_URL to your PostgreSQL connection string.\n"
        "Example: postgresql://user:password@host:5432/database"
    )

url_str = str(DATABASE_URL).strip()

# Heroku/Render/Railway sometimes provide postgres:// which SQLAlchemy doesn't accept
if url_str.startswith("postgres://"):
    url_str = url_str.replace("postgres://", "postgresql://", 1)
    DATABASE_URL = url_str

# Validate it's PostgreSQL
lower_url = url_str.lower()
if not any(x in lower_url for x in ["postgresql", "postgres"]):
    raise RuntimeError(
        f"❌ Invalid DATABASE_URL!\n"
        f"Expected PostgreSQL URL but got: {url_str[:50]}...\n"
        "URL must start with postgresql:// or postgres://"
    )

# Initialize PostgreSQL connection
# Note: databases library handles connection pooling automatically
database = Database(DATABASE_URL)

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

# Create synchronous engine for table creation
# PostgreSQL doesn't need special handling like SQLite did
sync_url = str(DATABASE_URL)

try:
    engine = create_engine(sync_url)
    # Tables will be created in main.py startup to ensure proper initialization
except Exception as err:
    # Log helpful error for connection issues
    print(f"⚠️ Could not create engine: {err}")
    # Don't raise here - let the app handle it during startup
