"""Database engine and session handling (SQLAlchemy 2.x)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config import settings

# pool_pre_ping checks a pooled connection is still alive before using it,
# so a Postgres restart does not break the app.
# connect_timeout makes a request fail after 5 s if Postgres is down, instead
# of hanging for minutes (the Windows default).
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    connect_args={
        "connect_timeout": 5,
        # Postgres renders timestamptz in the session's timezone, and this
        # server's is Africa/Nairobi. Pinning the session to UTC means rows
        # read back as UTC, as the rest of the project assumes; conversion to
        # local time stays an explicit step in hutton.py.
        "options": "-c timezone=UTC",
    },
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency: one session per request, always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
