"""The hourly risk-scoring job: keeps risk_scores up to date for every node.

Each run, for every sensor node and for both data sources:

- 'sensor': the node's own under-canopy readings (what the farmer sees);
- 'api':    the ERA5-Land archive at the node's location, for comparison.
            It lags about 6 days behind today, so its recent days are
            stored honestly as 'no_data' until the archive catches up.

it writes:

1. Hutton (method 'hutton', horizon 0) for today and the LOOKBACK_DAYS
   before it. Today's row is provisional ('insufficient_data') until 20 of
   its 24 hours exist. The earlier days are recomputed on purpose: a node
   that was offline flushes its buffer late, which can complete them.
2. The learned model (method 'learned', horizons 24 and 48) issued on each
   of those days: for_date is the day the forecast was made from (data up
   to its end), so the 24 h row with for_date D is about D+1. A day that is
   not complete gets a row with score NULL, never a guess.

Every write is an upsert, so running the job twice, or after a crash,
gives the same table.

Started with the API (backend/app.py lifespan), every
RISK_JOB_INTERVAL_MINUTES, unless RISK_JOB_ENABLED is false. Run one
pass by hand with:

    .\\.venv\\Scripts\\python.exe -m backend.risk_job --once
    .\\.venv\\Scripts\\python.exe -m backend.risk_job --once --date 2026-10-12
"""
import argparse
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import alerts, hutton, risk_model
from backend.config import settings
from backend.models import RiskScore, SensorNode

log = logging.getLogger("risk_job")

DATA_SOURCES = ("sensor", "api")
LOOKBACK_DAYS = 2  # also recompute the two days before today (late buffered readings)
LEARNED_HORIZONS = (24, 48)


def local_today() -> date:
    return datetime.now(hutton.LOCAL_TZ).date()


def upsert_score(session: Session, node_id: str, method: str, data_source: str,
                 horizon: int, for_date: date, **fields) -> RiskScore:
    """Write one risk_scores row, updating it if it already exists."""
    row = session.scalar(select(RiskScore).where(
        RiskScore.node_id == node_id,
        RiskScore.method == method,
        RiskScore.data_source == data_source,
        RiskScore.horizon_hours == horizon,
        RiskScore.for_date == for_date,
    ))
    if row is None:
        row = RiskScore(node_id=node_id, method=method, data_source=data_source,
                        horizon_hours=horizon, for_date=for_date)
        session.add(row)
    for name, value in fields.items():
        setattr(row, name, value)
    row.computed_at = func.now()
    return row


def score_learned(session: Session, node_id: str, data_source: str, days: list[date]) -> int:
    """Learned 24 h / 48 h rows for each issue day. Returns how many were judged."""
    judged = 0
    for day in days:
        result = risk_model.predict_for_node(node_id, day, data_source, session=session)
        if result["status"] == hutton.STATUS_OK:
            judged += 1
        for horizon in LEARNED_HORIZONS:
            upsert_score(
                session, node_id, "learned", data_source, horizon, day,
                status=result["status"],
                score=result[f"p{horizon}"],  # None for an unjudged day
                valid_hours=result["valid_hours"],
                hutton_day=None,
                hutton_period=None,
                model_version=result["model_version"][horizon],
            )
    return judged


def run_once(session: Session, today: date | None = None) -> list[dict]:
    """One pass over every node. Returns a short summary per node and source."""
    today = today or local_today()
    days = [today - timedelta(days=n) for n in range(LOOKBACK_DAYS, -1, -1)]

    try:
        risk_model.load_models()
        learned_available = True
    except FileNotFoundError as error:
        log.warning("Learned model skipped: %s", error)
        learned_available = False

    summary = []
    for node in session.scalars(select(SensorNode).order_by(SensorNode.node_id)).all():
        for source in DATA_SOURCES:
            daily = hutton.compute_hutton_for_node(node.node_id, days[0], days[-1], source, session=session)
            learned_judged = (
                score_learned(session, node.node_id, source, days) if learned_available else None
            )
            summary.append({
                "node_id": node.node_id,
                "data_source": source,
                "hutton": {str(d): row["status"] for d, row in daily.iterrows()},
                "learned_days_judged": learned_judged,
            })
        # The high-risk SMS policy (backend/alerts.py), on the plot's own sensor risk.
        alert = alerts.check_high_risk(session, node, today)
        summary[-1]["high_risk_sms"] = None if alert is None else alert.delivery_status
        session.commit()  # one node at a time: a failure later keeps this node's scores
    return summary


def run_job() -> None:
    """The scheduled entry point: its own session, and it never dies on an error."""
    from backend.db import SessionLocal

    try:
        with SessionLocal() as session:
            summary = run_once(session)
        log.info("Risk job done for %d node/source pairs", len(summary))
    except Exception:  # one bad run must not stop the schedule
        log.exception("Risk job failed; it will run again at the next interval")


def start_scheduler():
    """Start the hourly job in a background thread. None if disabled in config."""
    if not settings.RISK_JOB_ENABLED:
        log.info("Risk job disabled (RISK_JOB_ENABLED=false)")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=settings.TIMEZONE)
    scheduler.add_job(
        run_job, "interval", minutes=settings.RISK_JOB_INTERVAL_MINUTES, id="risk_job",
        next_run_time=datetime.now(hutton.LOCAL_TZ),  # first run straight away
        max_instances=1, coalesce=True,  # never two runs at once; skip a missed backlog
    )
    scheduler.start()
    log.info("Risk job scheduled every %d minutes", settings.RISK_JOB_INTERVAL_MINUTES)
    return scheduler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compute risk scores for every node.")
    parser.add_argument("--once", action="store_true", help="run one pass now and exit")
    parser.add_argument("--date", type=date.fromisoformat, help="treat this local day as today")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.once:
        parser.error("use --once (the hourly schedule runs inside the API)")

    from backend.db import SessionLocal

    with SessionLocal() as session:
        for line in run_once(session, args.date):
            print(f"{line['node_id']} [{line['data_source']}] hutton {line['hutton']} "
                  f"| learned days judged: {line['learned_days_judged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
