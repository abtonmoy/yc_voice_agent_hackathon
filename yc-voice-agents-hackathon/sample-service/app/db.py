"""Database engine + session setup for payments-api."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ["PAYMENTS_DB_URL"]

# Connection pool for payments-db. Sized at deploy abc123 (14:30).
engine = create_engine(DB_URL)            # no pool cap (abc123)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session():
    """Yield a scoped DB session, closing it on exit."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
