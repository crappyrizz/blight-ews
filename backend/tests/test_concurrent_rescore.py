"""Two scoring runs for one node at the same moment: one set of scores, one SMS.

The real case (26 Oct 2026): an admin re-score was run twice, 6 s apart, on
the hosted server. Both runs looked for existing rows and for the node's alert
state before either committed, so both inserted a full set of risk scores and
both sent the high-risk SMS. risk_job.score_node now locks the node while it
scores, and risk_scores has a unique constraint.

The concurrency test needs two real connections that commit, so it cannot use
the rolled-back db_session; it creates its own rows and deletes them after.
"""
import importlib.util
import threading
import time

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import alerts, auth, ingest, risk_job, risk_model
from backend.config import REPO_ROOT, settings
from backend.models import Alert, Farmer, RiskScore, SensorNode, SensorReading
from tools import seed_demo_event as seed

NODE = "demo-race"


def no_learned_model():
    raise FileNotFoundError("learned model switched off for this test")


@pytest.fixture
def committed_node(monkeypatch):
    """A farmer and a node with a Hutton period ending yesterday, committed; removed afterwards."""
    monkeypatch.setattr(risk_model, "load_models", no_learned_model)
    monkeypatch.setattr(settings, "SMS_DRY_RUN", True)
    engine = create_engine(settings.TEST_DATABASE_URL)
    with Session(engine) as s:
        farmer = Farmer(name="Race", phone_number="+254712000044", password_hash=auth.hash_password("pw-123456"),
                        role="farmer")
        s.add(farmer)
        s.flush()
        node = SensorNode(node_id=NODE, farmer_id=farmer.farmer_id, location="Race plot",
                          latitude=-0.7, longitude=36.6, api_key_hash="x")
        s.add(node)
        s.flush()
        end = risk_job.local_today() - risk_job.timedelta(days=1)
        ingest.store_readings(s, node, seed.hourly_history(end, days=4, wet_days=2))
        s.commit()
        farmer_id = farmer.farmer_id
    try:
        yield engine
    finally:
        with Session(engine) as s:
            s.execute(delete(Alert).where(Alert.node_id == NODE))
            s.execute(delete(RiskScore).where(RiskScore.node_id == NODE))
            s.execute(delete(SensorReading).where(SensorReading.node_id == NODE))
            s.execute(delete(SensorNode).where(SensorNode.node_id == NODE))
            s.execute(delete(Farmer).where(Farmer.farmer_id == farmer_id))
            s.commit()
        engine.dispose()


def test_two_simultaneous_runs_send_one_sms_and_write_one_set_of_scores(committed_node, monkeypatch):
    engine = committed_node
    original_check = alerts.check_high_risk

    def slow_check(session, node, today, now=None):
        time.sleep(1.5)  # hold the first run open, as Render's slow CPU did, so the second starts meanwhile
        return original_check(session, node, today, now)

    monkeypatch.setattr(alerts, "check_high_risk", slow_check)
    errors = []

    def run():
        try:
            with Session(engine) as s:
                node = s.get(SensorNode, NODE)
                risk_job.score_node(s, node, risk_job.local_today(), learned_available=False)
        except Exception as error:  # surfaced below, not lost in the thread
            errors.append(error)

    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    time.sleep(0.5)  # the second run starts while the first is still scoring
    second.start()
    first.join(30)
    second.join(30)

    assert errors == []
    with Session(engine) as s:
        sent = s.scalars(select(Alert).where(Alert.node_id == NODE, Alert.alert_type == alerts.ALERT_HIGH_RISK)).all()
        assert len(sent) == 1
        keys = s.execute(text(
            "select count(*), count(distinct (method, data_source, horizon_hours, for_date)) "
            "from risk_scores where node_id = :n"), {"n": NODE}).one()
        assert keys[0] == keys[1] > 0  # no duplicate rows
        assert s.get(SensorNode, NODE).last_risk_level == "high"


def test_the_database_refuses_a_duplicate_score(db_session):
    farmer = Farmer(name="Dup", phone_number="+254712000043", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    db_session.add(SensorNode(node_id="dup-01", farmer_id=farmer.farmer_id, location="x", latitude=0, longitude=0))
    db_session.flush()
    row = dict(node_id="dup-01", method="hutton", data_source="sensor", horizon_hours=0,
               for_date=risk_job.local_today(), status="no_data", score=None)
    db_session.add(RiskScore(**row))
    db_session.flush()
    db_session.add(RiskScore(**row))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_the_migration_keeps_the_row_an_alert_points_to(db_session):
    """Recreate the duplicates the real double run left, then run the migration's clean-up SQL."""
    path = REPO_ROOT / "alembic" / "versions" / "e77b7d65904d_risk_scores_unique_per_node_method_.py"
    spec = importlib.util.spec_from_file_location("migration_e77b7d65904d", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db_session.execute(text("ALTER TABLE risk_scores DROP CONSTRAINT uq_risk_scores_node_method_source_horizon_day"))
    farmer = Farmer(name="Old", phone_number="+254712000042", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    db_session.add(SensorNode(node_id="old-01", farmer_id=farmer.farmer_id, location="x", latitude=0, longitude=0))
    db_session.flush()
    day = risk_job.local_today()
    older = RiskScore(node_id="old-01", method="hutton", data_source="sensor", horizon_hours=0, for_date=day,
                      status="ok", score=1.0, valid_hours=24)
    newer = RiskScore(node_id="old-01", method="hutton", data_source="sensor", horizon_hours=0, for_date=day,
                      status="ok", score=1.0, valid_hours=24)
    db_session.add(older)
    db_session.flush()
    db_session.add(newer)
    db_session.flush()
    db_session.add(Alert(node_id="old-01", alert_type="high_risk", message="m", delivery_status="submitted",
                         risk_score_id=older.score_id))
    db_session.flush()

    for statement in ("""UPDATE alerts SET risk_score_id = r.keep_id FROM ranked r
                         WHERE alerts.risk_score_id = r.score_id AND r.score_id <> r.keep_id""",
                      """DELETE FROM risk_scores WHERE score_id IN
                         (SELECT score_id FROM ranked WHERE score_id <> keep_id)"""):
        db_session.execute(text(migration.RANKED + statement))
    db_session.expire_all()

    left = db_session.scalars(select(RiskScore).where(RiskScore.node_id == "old-01")).all()
    assert [r.score_id for r in left] == [older.score_id]  # the one the alert points to
    assert db_session.scalars(select(Alert.risk_score_id).where(Alert.node_id == "old-01")).one() == older.score_id
