"""Give a demo device a blight-favourable history, so the demo can show an alert on cue.

Hutton judges whole days and the risk job runs hourly, so an alert cannot be
waited for live. This writes a history that ends in a Hutton period, then the
demo calls the admin re-score endpoint and the alert appears straight away.

    .\\.venv\\Scripts\\python.exe -m tools.seed_demo_event                 # demo-01, ending yesterday
    .\\.venv\\Scripts\\python.exe -m tools.seed_demo_event --rescore       # and score it now

What it writes, one reading per hour (local days, Africa/Nairobi):

- --days (default 16) of history ending on --end (default: yesterday). That
  is longer than the learned model's 15-day look-back (risk_model.HISTORY_DAYS):
  fusion prefers the learned 24 h forecast over the Hutton score, and with
  only a couple of days of history that forecast would rest on blanks.
- The last --wet-days (default 2) are Hutton days: nights at 12.5 C and 97 %
  RH (12 humid hours, minimum above 10 C), so the last day is a Hutton period.
- The days before are dry, cold-night days (8 C minimum: never a Hutton day).

Re-running it resets the device first, so it can be run the night before the
demo after a rehearsal: its readings, risk scores and alerts are deleted and
its last risk level cleared. Otherwise the high-risk SMS would not fire again
(it needs a CHANGE to high, at most once per 24 h). Only devices whose ID
starts with "demo-" are accepted, so a real node can never be wiped by this.

The device must already exist: .\\dev.ps1 register-device demo-01 "Demo node"
--owner-phone 0700000002 (owned by the farmer, so it shows in the app and its
SMS goes to the farmer's number).
"""
import argparse
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend import hutton, ingest, risk_job
from backend.models import Alert, ReadingContribution, RiskScore, SensorNode, SensorReading
from backend.schemas import ReadingIn

DEMO_PREFIX = "demo-"
DEFAULT_DEVICE = "demo-01"
DEFAULT_DAYS = 16
DEFAULT_WET_DAYS = 2

# Night = 18:00-06:00 local. Wet nights meet both Hutton conditions; dry nights fail on temperature.
WET_NIGHT = (12.5, 97.0)
WET_DAY = (17.0, 80.0)
DRY_NIGHT = (8.0, 88.0)
DRY_DAY = (19.0, 62.0)


class DemoSeedError(Exception):
    """The seed cannot run: the message says why."""


def hourly_history(end: date, days: int, wet_days: int) -> list[ReadingIn]:
    """One reading per local hour for `days` days ending on `end`; the last `wet_days` are Hutton days."""
    if wet_days > days:
        raise DemoSeedError("--wet-days cannot be more than --days")
    first = end - timedelta(days=days - 1)
    readings = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        wet = offset >= days - wet_days
        for hour in range(24):
            night = hour >= 18 or hour < 6
            temperature, humidity = (WET_NIGHT if night else WET_DAY) if wet else (DRY_NIGHT if night else DRY_DAY)
            moment = datetime.combine(day, time(hour), tzinfo=hutton.LOCAL_TZ)
            readings.append(ReadingIn(timestamp=moment, temperature=temperature, humidity=humidity))
    return readings


def reset_device(session: Session, node: SensorNode) -> dict:
    """Delete the device's readings, risk scores and alerts; clear its alert state."""
    reading_ids = select(SensorReading.reading_id).where(SensorReading.node_id == node.node_id)
    counts = {
        "contributions": session.execute(
            delete(ReadingContribution).where(ReadingContribution.reading_id.in_(reading_ids))).rowcount,
        "alerts": session.execute(delete(Alert).where(Alert.node_id == node.node_id)).rowcount,
        "risk_scores": session.execute(delete(RiskScore).where(RiskScore.node_id == node.node_id)).rowcount,
        "readings": session.execute(delete(SensorReading).where(SensorReading.node_id == node.node_id)).rowcount,
    }
    node.last_risk_level = None
    session.flush()
    return counts


def seed_demo_event(session: Session, device_id: str = DEFAULT_DEVICE, end: date | None = None,
                    days: int = DEFAULT_DAYS, wet_days: int = DEFAULT_WET_DAYS) -> dict:
    """Reset the demo device and write its history. Commits. Returns what was done."""
    if not device_id.startswith(DEMO_PREFIX):
        raise DemoSeedError(f"only demo devices (IDs starting with '{DEMO_PREFIX}') can be seeded")
    node = session.get(SensorNode, device_id)
    if node is None:
        raise DemoSeedError(f"device {device_id} does not exist; register it first with tools.register_device")
    end = end or risk_job.local_today() - timedelta(days=1)

    removed = reset_device(session, node)
    stored, skipped = ingest.store_readings(session, node, hourly_history(end, days, wet_days))
    session.commit()
    return {"device_id": device_id, "removed": removed, "stored": stored, "skipped": skipped,
            "first_day": str(end - timedelta(days=days - 1)), "last_day": str(end),
            "wet_days": [str(end - timedelta(days=n)) for n in range(wet_days - 1, -1, -1)]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed a demo device with a history ending in a Hutton period.")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE)
    parser.add_argument("--end", type=date.fromisoformat, help="last day of the history (default: yesterday)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--wet-days", type=int, default=DEFAULT_WET_DAYS)
    parser.add_argument("--rescore", action="store_true", help="also score the device now (risk and SMS check)")
    args = parser.parse_args(argv)

    from backend.db import SessionLocal

    with SessionLocal() as session:
        try:
            result = seed_demo_event(session, args.device_id, args.end, args.days, args.wet_days)
        except DemoSeedError as error:
            print(f"Not seeded: {error}")
            return 1
        print(f"Reset {result['device_id']}: removed {result['removed']}")
        print(f"Stored {result['stored']} hourly readings, {result['first_day']} to {result['last_day']}; "
              f"Hutton days: {', '.join(result['wet_days'])}")
        if args.rescore:
            node = session.get(SensorNode, args.device_id)
            summary = risk_job.score_node(session, node, risk_job.local_today(), risk_job.learned_model_available())
            print(f"Risk now: {ingest.current_risk_level(session, node)}; "
                  f"high-risk SMS: {summary[-1]['high_risk_sms'] or 'not sent'}")
        else:
            print("Next: POST /admin/nodes/{id}/rescore as admin (or re-run with --rescore).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
