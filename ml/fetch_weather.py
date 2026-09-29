"""Pull hourly weather for the farm site into the weather_readings table.

Two Open-Meteo APIs, for two different jobs:

- **archive** (`archive-api.open-meteo.com/v1/archive`, ERA5 reanalysis):
  the long, consistent record. Data from 1940 onwards, but it lags about
  5 days behind today, so it can never tell us about this morning.
  This is the history the Hutton baseline and the learned model train on.

- **historical_forecast**
  (`historical-forecast-api.open-meteo.com/v1/forecast`): what the forecast
  models actually predicted in the past, from about 2022 onwards. This is
  what the "Hutton on forecast" benchmark needs, because it shows what we
  would have known at the time rather than what turned out to be true.

Both are free and need no API key (600 calls/min, 10,000/day).

Usage from the repo root:
    .\\.venv\\Scripts\\python.exe -m ml.fetch_weather --source archive \\
        --start 2015-01-01 --end 2026-09-20

Re-running is safe: rows are inserted with ON CONFLICT DO NOTHING, so a
repeated range adds nothing. Raw responses are also written to
data/weather/*.csv so they can be inspected by hand.
"""
import argparse
import csv
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend import ids
from backend.config import REPO_ROOT, settings
from backend.models import WeatherReading

log = logging.getLogger("fetch_weather")

SOURCE_ARCHIVE = "archive"
SOURCE_HISTORICAL_FORECAST = "historical_forecast"

SOURCE_URLS = {
    SOURCE_ARCHIVE: "https://archive-api.open-meteo.com/v1/archive",
    SOURCE_HISTORICAL_FORECAST: "https://historical-forecast-api.open-meteo.com/v1/forecast",
}

# How far back each source goes. ERA5 reaches 1940 worldwide, including
# Kenya; the historical forecast archive only starts around 2022 for the
# models that cover this region.
EARLIEST_DATE = {
    SOURCE_ARCHIVE: date(1940, 1, 1),
    SOURCE_HISTORICAL_FORECAST: date(2022, 1, 1),
}

# ERA5 is published with about a 5-day delay, so asking for yesterday
# returns empty hours rather than an error.
ARCHIVE_LAG_DAYS = 5

TEMPERATURE_VARIABLE = "temperature_2m"
HUMIDITY_VARIABLE = "relative_humidity_2m"
HOURLY_VARIABLES = f"{TEMPERATURE_VARIABLE},{HUMIDITY_VARIABLE}"

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2  # 2, 4, 8 ... between attempts
PAUSE_BETWEEN_CHUNKS = 1.0  # be polite to a free API
REQUEST_TIMEOUT = 60.0

CSV_DIR = REPO_ROOT / "data" / "weather"

# The unique constraint that makes a re-run harmless. It uses
# NULLS NOT DISTINCT, so archive rows (forecast_issued_at IS NULL) are
# compared on their null too, instead of every one counting as different.
WEATHER_CONFLICT = "uq_weather_readings_source_point_time"


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into one chunk per calendar year.

    Yearly chunks keep each response to a manageable size (about 8,760 hourly
    values) and mean a failure part-way through a long backfill only costs
    one year's re-fetch.
    """
    if end < start:
        raise ValueError("end date is before start date")

    chunks = []
    chunk_start = start
    while chunk_start <= end:
        year_end = date(chunk_start.year, 12, 31)
        chunk_end = min(year_end, end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def fetch_chunk(
    client,
    source: str,
    start: date,
    end: date,
    latitude: float,
    longitude: float,
    sleep=time.sleep,
) -> dict:
    """One API call for one chunk, retrying failures with backoff.

    Retries network errors and 429/5xx (rate limit, server trouble). A 400 is
    not retried: that means the request itself is wrong, and repeating it
    would only waste the daily quota.
    """
    url = SOURCE_URLS[source]
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": HOURLY_VARIABLES,
        # UTC in, UTC stored. Conversion to Africa/Nairobi happens later,
        # only where a local calendar day is needed.
        "timezone": "UTC",
    }

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, params=params)
            if response.status_code == 400:
                # Open-Meteo explains refusals in the body.
                raise ValueError(f"Open-Meteo rejected the request: {response.text}")
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            if attempt == MAX_ATTEMPTS:
                break
            delay = BACKOFF_SECONDS ** attempt
            log.warning(
                "%s %s..%s failed (%s), retrying in %ss (attempt %s/%s)",
                source, start, end, error, delay, attempt, MAX_ATTEMPTS,
            )
            sleep(delay)

    raise RuntimeError(f"{source} {start}..{end} failed after {MAX_ATTEMPTS} attempts: {last_error}")


def parse_hourly(payload: dict) -> tuple[list[dict], int]:
    """Turn one response into rows, and count the hours it had no value for.

    Hours where the API returns null are dropped, not filled in: a gap is
    recorded as a gap, and hutton.py decides whether the day still has its
    20 of 24 values. Interpolating here would manufacture the very hours the
    Hutton criteria count.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temperatures = hourly.get(TEMPERATURE_VARIABLE) or []
    humidities = hourly.get(HUMIDITY_VARIABLE) or []

    if not (len(times) == len(temperatures) == len(humidities)):
        raise ValueError("Open-Meteo returned mismatched hourly series lengths")

    rows = []
    missing = 0
    for moment, temperature, humidity in zip(times, temperatures, humidities):
        if temperature is None or humidity is None:
            missing += 1
            continue
        rows.append({
            # timezone=UTC means the strings carry no offset; they are UTC.
            "timestamp": datetime.fromisoformat(moment).replace(tzinfo=timezone.utc),
            "temperature": float(temperature),
            "humidity": float(humidity),
        })
    return rows, missing


def save_csv(
    payload: dict, source: str, start: date, end: date, latitude: float, longitude: float,
    csv_dir: Path = CSV_DIR,
) -> Path:
    """Write the response's hourly series out as CSV, for inspection by hand."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    path = csv_dir / f"{source}_{latitude}_{longitude}_{start}_{end}.csv"

    hourly = payload.get("hourly") or {}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", TEMPERATURE_VARIABLE, HUMIDITY_VARIABLE])
        writer.writerows(zip(
            hourly.get("time") or [],
            hourly.get(TEMPERATURE_VARIABLE) or [],
            hourly.get(HUMIDITY_VARIABLE) or [],
        ))
    return path


def store_rows(
    session: Session, source: str, latitude: float, longitude: float, rows: list[dict]
) -> tuple[int, int]:
    """Insert rows, skipping any already stored. Returns (stored, duplicates)."""
    if not rows:
        return 0, 0

    values = [
        {
            "weather_id": ids.new_id(ids.WEATHER_READING),
            "latitude": latitude,
            "longitude": longitude,
            "source": source,
            "timestamp": row["timestamp"],
            "temperature": row["temperature"],
            "humidity": row["humidity"],
            # Open-Meteo's historical forecast API is a continuous series
            # stitched from the first hours of successive model runs, and it
            # exposes no per-hour issue time, so we cannot record one. Getting
            # a true issue time would mean the Single Runs API (&run=) or the
            # Previous Runs API (*_previous_dayN, archived from 2024), which
            # is the better source for a fixed-lead-time forecast benchmark.
            "forecast_issued_at": None,
        }
        for row in rows
    ]

    statement = (
        pg_insert(WeatherReading)
        .values(values)
        .on_conflict_do_nothing(constraint=WEATHER_CONFLICT)
        .returning(WeatherReading.weather_id)
    )
    stored = len(session.scalars(statement).all())
    session.commit()
    return stored, len(values) - stored


def fetch_range(
    session: Session,
    source: str,
    start: date,
    end: date,
    latitude: float | None = None,
    longitude: float | None = None,
    client=None,
    write_csv: bool = True,
    csv_dir: Path = CSV_DIR,
    sleep=time.sleep,
) -> dict:
    """Fetch and store a date range, one calendar year at a time."""
    if source not in SOURCE_URLS:
        raise ValueError(f"Unknown source: {source}")

    earliest = EARLIEST_DATE[source]
    if start < earliest:
        raise ValueError(f"{source} data starts on {earliest}, not {start}")

    latitude = settings.SITE_LAT if latitude is None else latitude
    longitude = settings.SITE_LON if longitude is None else longitude

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=REQUEST_TIMEOUT)

    totals = {"chunks": 0, "hours": 0, "stored": 0, "duplicates": 0, "missing": 0}
    try:
        chunks = year_chunks(start, end)
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            payload = fetch_chunk(
                client, source, chunk_start, chunk_end, latitude, longitude, sleep=sleep
            )
            rows, missing = parse_hourly(payload)
            stored, duplicates = store_rows(session, source, latitude, longitude, rows)

            if write_csv:
                save_csv(payload, source, chunk_start, chunk_end, latitude, longitude, csv_dir)

            log.info(
                "%s %s..%s: %s hours, %s stored, %s already present, %s with no value",
                source, chunk_start, chunk_end, len(rows), stored, duplicates, missing,
            )
            totals["chunks"] += 1
            totals["hours"] += len(rows)
            totals["stored"] += stored
            totals["duplicates"] += duplicates
            totals["missing"] += missing

            if index < len(chunks) - 1:
                sleep(PAUSE_BETWEEN_CHUNKS)
    finally:
        if own_client:
            client.close()

    return totals


def default_end(source: str) -> date:
    """Latest date worth asking for: ERA5 lags about 5 days behind today."""
    today = datetime.now(timezone.utc).date()
    if source == SOURCE_ARCHIVE:
        return today - timedelta(days=ARCHIVE_LAG_DAYS)
    return today


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", choices=sorted(SOURCE_URLS), default=SOURCE_ARCHIVE,
        help="archive (ERA5, from 1940) or historical_forecast (from ~2022)",
    )
    parser.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument(
        "--end", type=date.fromisoformat, default=None,
        help="YYYY-MM-DD (default: today, minus ERA5's 5-day lag for archive)",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="shortcut for the last N days, instead of --start",
    )
    parser.add_argument("--lat", type=float, default=settings.SITE_LAT)
    parser.add_argument("--lon", type=float, default=settings.SITE_LON)
    parser.add_argument("--no-csv", action="store_true", help="skip writing data/weather/*.csv")

    args = parser.parse_args(argv)
    if args.end is None:
        args.end = default_end(args.source)
    if args.start is None:
        if args.days is None:
            parser.error("give either --start or --days")
        args.start = args.end - timedelta(days=args.days - 1)
    return args


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    from backend.db import SessionLocal

    with SessionLocal() as session:
        totals = fetch_range(
            session,
            source=args.source,
            start=args.start,
            end=args.end,
            latitude=args.lat,
            longitude=args.lon,
            write_csv=not args.no_csv,
        )

    log.info(
        "Done: %s chunk(s), %s hourly values, %s stored, %s already present, %s with no value",
        totals["chunks"], totals["hours"], totals["stored"],
        totals["duplicates"], totals["missing"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
