"""Problems the system ran into, in plain words, kept in the database (system_events).

Logs disappear when the hosted server restarts; these rows stay, so the admin
status (GET /admin/status, backend/status.py) can say what went wrong, when
and where. Written by:
- the risk job: a sensor whose scoring failed, a whole run that failed, the
  learned model missing, and one 'info' row per completed run;
- SMS: a message not sent, or reported as not delivered;
- startup: the leaf checker (CNN) not loaded;
- the API: any unexpected server error (500).

Each event is written in its OWN session, so it survives when the work that
failed is rolled back, and recording never raises: a broken log must not
break the work it describes.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.models import SystemEvent

log = logging.getLogger("events")

INFO, WARNING, ERROR = "info", "warning", "error"
KEEP_INFO_DAYS = 30  # routine 'info' rows (one per job run) are pruned after this
MAX_MESSAGE = 1000

# Where event rows go: a session separate from the caller's. Tests point this
# at their own rolled-back transaction, or at None to only log (conftest.py).
session_factory = SessionLocal


def record(source: str, code: str, message: str, severity: str = ERROR, *, node_id: str | None = None,
           farmer_id: str | None = None, quiet_hours: float = 0) -> bool:
    """Log the event and store it. True if a row was written.

    quiet_hours: skip it if the same source/code/node was already recorded
    that recently (a problem that persists is reported once, not every hour).
    """
    level = {INFO: logging.INFO, WARNING: logging.WARNING}.get(severity, logging.ERROR)
    log.log(level, "%s/%s%s: %s", source, code, f" ({node_id})" if node_id else "", message)
    if session_factory is None:
        return False
    now = datetime.now(UTC)  # not the database's now(), which is the same for a whole transaction
    try:
        with session_factory() as session:
            if quiet_hours:
                since = now - timedelta(hours=quiet_hours)
                same_node = SystemEvent.node_id.is_(None) if node_id is None else SystemEvent.node_id == node_id
                if session.scalar(select(SystemEvent.event_id).where(
                        SystemEvent.source == source, SystemEvent.code == code, same_node,
                        SystemEvent.occurred_at >= since).limit(1)):
                    return False
            session.add(SystemEvent(occurred_at=now, source=source, severity=severity, code=code,
                                    message=message[:MAX_MESSAGE], node_id=node_id, farmer_id=farmer_id))
            session.commit()
        return True
    except Exception:  # never let the log break the caller
        log.exception("Could not store the event %s/%s", source, code)
        return False


def prune_info(now: datetime | None = None) -> None:
    """Delete routine 'info' rows older than KEEP_INFO_DAYS (problems are kept)."""
    if session_factory is None:
        return
    cutoff = (now or datetime.now(UTC)) - timedelta(days=KEEP_INFO_DAYS)
    try:
        with session_factory() as session:
            session.execute(delete(SystemEvent).where(SystemEvent.severity == INFO,
                                                      SystemEvent.occurred_at < cutoff))
            session.commit()
    except Exception:
        log.exception("Could not prune old events")
