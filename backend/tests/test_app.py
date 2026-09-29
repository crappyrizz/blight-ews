from sqlalchemy.exc import OperationalError

from backend.app import app
from backend.db import get_db


def test_health_ok_when_database_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


class UnreachableSession:
    """Stand-in session that fails the way SQLAlchemy does when Postgres is down."""

    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_health_503_when_database_unreachable(client):
    app.dependency_overrides[get_db] = lambda: UnreachableSession()
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unreachable"}
