from sqlalchemy import func, select

from backend.config import settings
from backend.models import Farmer, SensorNode
from tools.seed import seed


def test_seed_creates_admin_farmer_and_node(db_session):
    seed(db_session)

    farmers = db_session.scalars(select(Farmer)).all()
    assert sorted(f.role for f in farmers) == ["admin", "farmer"]
    assert any(f.name == "Kinangop Farmer" for f in farmers)

    node = db_session.scalars(select(SensorNode)).one()
    assert (node.latitude, node.longitude) == (settings.SITE_LAT, settings.SITE_LON)


def test_seed_is_safe_to_run_twice(db_session):
    seed(db_session)
    log = seed(db_session)

    assert db_session.scalar(select(func.count()).select_from(Farmer)) == 2
    assert db_session.scalar(select(func.count()).select_from(SensorNode)) == 1
    assert all("already exists" in line for line in log)
