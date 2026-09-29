"""Taking in sensor readings from a node.

The shape of this is driven by the plot's weak network coverage. The node
buffers readings locally and flushes them when it next gets a connection, so:

- readings arrive in batches, not one at a time;
- they arrive late, and out of order, which is fine;
- the same batch may arrive twice, because the node could not tell whether
  its last upload succeeded. A repeat must therefore be harmless: duplicates
  are skipped, not rejected, and the node gets a 200 either way.

One bad reading also must not cost the node the whole buffer, so each row is
validated on its own and rejects are reported back by position.

The full contract is written up in docs/firmware_contract.md.
"""
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend import ids
from backend.models import SensorNode, SensorReading
from backend.schemas import IngestResult, ReadingIn, RejectedReading

# The unique constraint that makes a replayed batch harmless.
READING_CONFLICT = "uq_sensor_readings_node_timestamp"


def _first_error(error: ValidationError) -> str:
    """One readable reason from a pydantic error, for the firmware author."""
    first = error.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "reading"
    message = first["msg"].removeprefix("Value error, ")
    return f"{field}: {message}"


def validate_readings(raw_readings: list) -> tuple[list[ReadingIn], list[RejectedReading]]:
    """Validate each row on its own, keeping the node's original positions."""
    valid: list[ReadingIn] = []
    rejected: list[RejectedReading] = []

    for index, raw in enumerate(raw_readings):
        if not isinstance(raw, dict):
            rejected.append(RejectedReading(index=index, reason="reading must be an object"))
            continue
        try:
            valid.append(ReadingIn(**raw))
        except ValidationError as error:
            rejected.append(RejectedReading(index=index, reason=_first_error(error)))

    return valid, rejected


def store_readings(
    session: Session, node: SensorNode, readings: list[ReadingIn]
) -> tuple[int, int]:
    """Insert readings, skipping ones already stored. Returns (accepted, duplicates).

    Duplicates come from two places: a timestamp already in the database
    (a replayed batch), and the same timestamp twice inside one batch.
    Both are counted as duplicates, because in both cases the node's reading
    for that hour is already accounted for.
    """
    if not readings:
        return 0, 0

    # Deduplicate within the batch first: one INSERT cannot resolve a
    # conflict between two of its own rows.
    unique: dict[datetime, ReadingIn] = {}
    in_batch_duplicates = 0
    for reading in readings:
        if reading.timestamp in unique:
            in_batch_duplicates += 1
        else:
            unique[reading.timestamp] = reading

    rows = [
        {
            "reading_id": ids.new_id(ids.SENSOR_READING),
            "node_id": node.node_id,
            "timestamp": reading.timestamp,
            "temperature": reading.temperature,
            "humidity": reading.humidity,
            "leaf_wetness": reading.leaf_wetness,
        }
        for reading in unique.values()
    ]

    # ON CONFLICT DO NOTHING: an already-stored (node_id, timestamp) is left
    # alone. RETURNING tells us how many rows were actually new.
    statement = (
        pg_insert(SensorReading)
        .values(rows)
        .on_conflict_do_nothing(constraint=READING_CONFLICT)
        .returning(SensorReading.reading_id)
    )
    accepted = len(session.scalars(statement).all())
    duplicates = (len(rows) - accepted) + in_batch_duplicates
    return accepted, duplicates


def ingest_batch(session: Session, node: SensorNode, raw_readings: list) -> IngestResult:
    """Validate and store one flush from a node, and record the sync."""
    valid, rejected = validate_readings(raw_readings)
    accepted, duplicates = store_readings(session, node, valid)

    # last_sync_time is when the node was last heard from, not the timestamp
    # of the newest reading: a node flushing a week-old buffer has still just
    # been in touch, and that is what the dashboard needs to show.
    node.last_sync_time = datetime.now(timezone.utc)

    session.commit()
    return IngestResult(accepted=accepted, duplicates=duplicates, rejected=rejected)
