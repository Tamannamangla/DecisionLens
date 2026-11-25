from typing import Generator
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from app.config import settings

import logging

from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("decisionlens.database")

# Engine setup with appropriate connection arguments
try:
    import psycopg  # psycopg v3
    has_psycopg3 = True
except ImportError:
    has_psycopg3 = False

try:
    import psycopg2
    has_psycopg2 = True
except ImportError:
    has_psycopg2 = False

db_url = settings.database_url
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
    if has_psycopg3:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif has_psycopg2:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

# Dynamically route Supabase direct IPv6 URLs to IPv4 connection pooler for cloud hosts like Render
try:
    parsed_db = urlparse(db_url)
    if parsed_db.hostname and parsed_db.hostname.startswith("db.") and parsed_db.hostname.endswith(".supabase.co"):
        project_ref = parsed_db.hostname.replace("db.", "").replace(".supabase.co", "")
        new_user = f"postgres.{project_ref}" if parsed_db.username == "postgres" else parsed_db.username
        # Preserve user's private password from environment
        auth_part = f"{new_user}:{parsed_db.password}" if parsed_db.password else new_user
        netloc = f"{auth_part}@aws-0-ap-southeast-1.pooler.supabase.com:6543"
        db_url = urlunparse((parsed_db.scheme, netloc, parsed_db.path, parsed_db.params, parsed_db.query, parsed_db.fragment))
        logger.info("Dynamically converted Supabase direct host to IPv4 connection pooler.")
except Exception as exc:
    logger.debug(f"URL parsing skipped: {exc}")

is_sqlite = db_url.startswith("sqlite")
engine_kwargs = {"echo": False}
if is_sqlite:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20
    engine_kwargs["pool_recycle"] = 300
    engine_kwargs["pool_pre_ping"] = False  # Avoid 450ms WAN roundtrip ping on every request

engine = create_engine(db_url, **engine_kwargs)

# Enable foreign key constraints in SQLite
if is_sqlite:
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """Dependency yielding a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all database tables on Supabase PostgreSQL and ensure schema migrations."""
    global engine, SessionLocal, is_sqlite
    import app.models  # noqa: F401 - ensure models are registered on Base.metadata

    logger.info("Initializing database on active Supabase PostgreSQL cluster...")
    Base.metadata.create_all(bind=engine)

    # Apply incremental column additions for PostgreSQL/Supabase
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS fault_lines JSONB DEFAULT '{}'::jsonb;"))
            conn.execute(text("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS domain_source VARCHAR(50) DEFAULT 'manual';"))
            conn.execute(text("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS domain_confidence FLOAT DEFAULT 1.0;"))
            conn.commit()
            logger.info("Supabase PostgreSQL schema verified successfully.")
    except Exception as exc:
        logger.debug(f"Postgres incremental column check skipped: {exc}")


