"""Shared pytest fixtures.

Tests run against TEST_DATABASE_URL, never DATABASE_URL:
- tables are created once at the start of the test session and dropped at the end;
- each test runs inside a transaction that is rolled back afterwards, so tests
  cannot see each other's data.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import app
from backend.config import settings
from backend.db import get_db
from backend.models import Base

# Safety check: the session fixture DROPS every table, so it must never point
# at the real database.
if settings.TEST_DATABASE_URL == settings.DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must be different from DATABASE_URL")

test_engine = create_engine(settings.TEST_DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def test_tables():
    Base.metadata.drop_all(test_engine)  # clear leftovers from an interrupted run
    Base.metadata.create_all(test_engine)
    yield
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def db_session():
    """A session whose changes are all rolled back when the test ends.

    The outer transaction is never committed. Any session.commit() inside the
    code under test only releases a SAVEPOINT, so it is undone too.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session):
    """FastAPI test client whose endpoints use the test session."""
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
