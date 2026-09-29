"""Tests for ml/fetch_weather.py. No network: every HTTP call is faked."""
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from backend.models import WeatherReading
from ml import fetch_weather as fw

LAT, LON = -0.70, 36.60


def payload(n_hours=3, start="2026-09-01T00:00", temperature=12.0, humidity=90.0):
    """A response shaped like Open-Meteo's, with `timezone=UTC` (no offsets)."""
    first = datetime.fromisoformat(start)
    return {
        "hourly": {
            "time": [(first + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n_hours)],
            "temperature_2m": [temperature + h for h in range(n_hours)],
            "relative_humidity_2m": [humidity for _ in range(n_hours)],
        }
    }


class FakeResponse:
    def __init__(self, json_payload=None, status_code=200, text=""):
        self._json = json_payload or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


class FakeClient:
    """Returns queued responses in order; a queued Exception is raised instead."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        item = self.queue.pop(0) if self.queue else FakeResponse(payload())
        if isinstance(item, Exception):
            raise item
        return item


def no_sleep(_seconds):
    return None


# --- chunking ----------------------------------------------------------


def test_range_is_split_into_calendar_years():
    chunks = fw.year_chunks(date(2015, 3, 5), date(2017, 2, 10))

    assert chunks == [
        (date(2015, 3, 5), date(2015, 12, 31)),
        (date(2016, 1, 1), date(2016, 12, 31)),
        (date(2017, 1, 1), date(2017, 2, 10)),
    ]


def test_range_inside_one_year_is_one_chunk():
    assert fw.year_chunks(date(2026, 1, 1), date(2026, 9, 20)) == [
        (date(2026, 1, 1), date(2026, 9, 20))
    ]


def test_single_day_range():
    assert fw.year_chunks(date(2026, 9, 20), date(2026, 9, 20)) == [
        (date(2026, 9, 20), date(2026, 9, 20))
    ]


def test_backwards_range_is_rejected():
    with pytest.raises(ValueError):
        fw.year_chunks(date(2026, 9, 20), date(2026, 9, 1))


# --- parsing -----------------------------------------------------------


def test_hourly_values_are_parsed_as_utc():
    rows, missing = fw.parse_hourly(payload(n_hours=2))

    assert missing == 0
    assert rows[0]["timestamp"] == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert rows[1]["timestamp"] == datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    assert rows[0]["temperature"] == 12.0
    assert rows[0]["humidity"] == 90.0


def test_hours_with_no_value_are_dropped_not_filled_in():
    """A gap stays a gap; hutton.py decides if the day still has 20 of 24."""
    body = payload(n_hours=4)
    body["hourly"]["temperature_2m"][1] = None
    body["hourly"]["relative_humidity_2m"][3] = None

    rows, missing = fw.parse_hourly(body)

    assert missing == 2
    assert len(rows) == 2
    assert [r["timestamp"].hour for r in rows] == [0, 2]


def test_mismatched_series_lengths_are_rejected():
    body = payload(n_hours=3)
    body["hourly"]["relative_humidity_2m"] = [90.0]

    with pytest.raises(ValueError, match="mismatched"):
        fw.parse_hourly(body)


def test_empty_payload_parses_to_nothing():
    assert fw.parse_hourly({}) == ([], 0)


# --- storing (idempotent upsert) --------------------------------------


def stored_count(session, source=None):
    query = select(func.count()).select_from(WeatherReading)
    if source:
        query = query.where(WeatherReading.source == source)
    return session.scalar(query)


def test_rows_are_stored(db_session):
    rows, _ = fw.parse_hourly(payload(n_hours=5))

    stored, duplicates = fw.store_rows(db_session, fw.SOURCE_ARCHIVE, LAT, LON, rows)

    assert (stored, duplicates) == (5, 0)
    assert stored_count(db_session) == 5


def test_storing_the_same_rows_again_adds_nothing(db_session):
    rows, _ = fw.parse_hourly(payload(n_hours=5))
    fw.store_rows(db_session, fw.SOURCE_ARCHIVE, LAT, LON, rows)

    stored, duplicates = fw.store_rows(db_session, fw.SOURCE_ARCHIVE, LAT, LON, rows)

    # The archive rows have forecast_issued_at NULL, and the unique
    # constraint uses NULLS NOT DISTINCT, so they really do collide.
    assert (stored, duplicates) == (0, 5)
    assert stored_count(db_session) == 5


def test_the_same_hour_from_two_sources_is_kept_separately(db_session):
    rows, _ = fw.parse_hourly(payload(n_hours=3))
    fw.store_rows(db_session, fw.SOURCE_ARCHIVE, LAT, LON, rows)

    stored, duplicates = fw.store_rows(db_session, fw.SOURCE_HISTORICAL_FORECAST, LAT, LON, rows)

    assert (stored, duplicates) == (3, 0)
    assert stored_count(db_session, fw.SOURCE_ARCHIVE) == 3
    assert stored_count(db_session, fw.SOURCE_HISTORICAL_FORECAST) == 3


def test_storing_nothing_is_harmless(db_session):
    assert fw.store_rows(db_session, fw.SOURCE_ARCHIVE, LAT, LON, []) == (0, 0)


# --- fetch_range end to end (faked HTTP) ------------------------------


def test_fetch_range_sends_the_documented_parameters(db_session, tmp_path):
    client = FakeClient(FakeResponse(payload(n_hours=3)))

    fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 3),
        latitude=LAT, longitude=LON, client=client, csv_dir=tmp_path, sleep=no_sleep,
    )

    call = client.calls[0]
    assert call["url"] == "https://archive-api.open-meteo.com/v1/archive"
    assert call["params"] == {
        "latitude": LAT,
        "longitude": LON,
        "start_date": "2026-09-01",
        "end_date": "2026-09-03",
        "hourly": "temperature_2m,relative_humidity_2m",
        "timezone": "UTC",
    }


def test_historical_forecast_uses_its_own_host(db_session, tmp_path):
    client = FakeClient(FakeResponse(payload(n_hours=1)))

    fw.fetch_range(
        db_session, fw.SOURCE_HISTORICAL_FORECAST, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=client, csv_dir=tmp_path, sleep=no_sleep,
    )

    assert client.calls[0]["url"].startswith("https://historical-forecast-api.open-meteo.com")


def test_multi_year_range_makes_one_call_per_year(db_session, tmp_path):
    client = FakeClient(
        FakeResponse(payload(n_hours=2, start="2024-12-30T00:00")),
        FakeResponse(payload(n_hours=2, start="2025-01-01T00:00")),
    )

    totals = fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2024, 12, 30), date(2025, 1, 2),
        latitude=LAT, longitude=LON, client=client, csv_dir=tmp_path, sleep=no_sleep,
    )

    assert len(client.calls) == 2
    assert [c["params"]["start_date"] for c in client.calls] == ["2024-12-30", "2025-01-01"]
    assert totals == {"chunks": 2, "hours": 4, "stored": 4, "duplicates": 0, "missing": 0}


def test_rerunning_a_fetch_stores_nothing_new(db_session, tmp_path):
    def run():
        return fw.fetch_range(
            db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
            latitude=LAT, longitude=LON,
            client=FakeClient(FakeResponse(payload(n_hours=4))),
            csv_dir=tmp_path, sleep=no_sleep,
        )

    first = run()
    second = run()

    assert first["stored"] == 4
    assert (second["stored"], second["duplicates"]) == (0, 4)
    assert stored_count(db_session) == 4


def test_raw_response_is_saved_as_csv(db_session, tmp_path):
    body = payload(n_hours=3)
    body["hourly"]["temperature_2m"][0] = None  # nulls are kept in the raw CSV

    fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=FakeClient(FakeResponse(body)),
        csv_dir=tmp_path, sleep=no_sleep,
    )

    files = list(tmp_path.glob("*.csv"))
    assert len(files) == 1
    assert files[0].name == f"archive_{LAT}_{LON}_2026-09-01_2026-09-01.csv"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert lines[0] == "time,temperature_2m,relative_humidity_2m"
    assert len(lines) == 4  # header plus 3 hours


def test_csv_writing_can_be_switched_off(db_session, tmp_path):
    fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=FakeClient(FakeResponse(payload())),
        csv_dir=tmp_path, write_csv=False, sleep=no_sleep,
    )

    assert list(tmp_path.glob("*.csv")) == []


def test_missing_values_are_counted_not_stored(db_session, tmp_path):
    body = payload(n_hours=4)
    body["hourly"]["relative_humidity_2m"][2] = None

    totals = fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=FakeClient(FakeResponse(body)),
        csv_dir=tmp_path, sleep=no_sleep,
    )

    assert totals["missing"] == 1
    assert totals["stored"] == 3


# --- dates a source cannot serve --------------------------------------


def test_archive_start_before_1940_is_refused(db_session):
    with pytest.raises(ValueError, match="1940"):
        fw.fetch_range(
            db_session, fw.SOURCE_ARCHIVE, date(1939, 1, 1), date(1939, 12, 31),
            client=FakeClient(), sleep=no_sleep,
        )


def test_historical_forecast_before_2022_is_refused(db_session):
    with pytest.raises(ValueError, match="2022"):
        fw.fetch_range(
            db_session, fw.SOURCE_HISTORICAL_FORECAST, date(2021, 1, 1), date(2021, 12, 31),
            client=FakeClient(), sleep=no_sleep,
        )


def test_unknown_source_is_refused(db_session):
    with pytest.raises(ValueError, match="Unknown source"):
        fw.fetch_range(db_session, "guesswork", date(2026, 1, 1), date(2026, 1, 2))


# --- retries -----------------------------------------------------------


def test_a_transient_failure_is_retried(db_session, tmp_path):
    client = FakeClient(
        httpx.HTTPError("connection reset"),
        FakeResponse(payload(n_hours=2)),
    )

    totals = fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=client, csv_dir=tmp_path, sleep=no_sleep,
    )

    assert len(client.calls) == 2
    assert totals["stored"] == 2


def test_rate_limiting_is_retried(db_session, tmp_path):
    client = FakeClient(FakeResponse(status_code=429), FakeResponse(payload(n_hours=1)))

    totals = fw.fetch_range(
        db_session, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
        latitude=LAT, longitude=LON, client=client, csv_dir=tmp_path, sleep=no_sleep,
    )

    assert totals["stored"] == 1


def test_it_gives_up_after_the_last_attempt(db_session):
    client = FakeClient(*[FakeResponse(status_code=503)] * fw.MAX_ATTEMPTS)

    with pytest.raises(RuntimeError, match="after 4 attempts"):
        fw.fetch_chunk(client, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
                       LAT, LON, sleep=no_sleep)

    assert len(client.calls) == fw.MAX_ATTEMPTS


def test_a_bad_request_is_not_retried(db_session):
    """A 400 means our request is wrong; repeating it just burns the quota."""
    client = FakeClient(FakeResponse(status_code=400, text='{"reason":"Invalid date"}'))

    with pytest.raises(ValueError, match="Invalid date"):
        fw.fetch_chunk(client, fw.SOURCE_ARCHIVE, date(2026, 9, 1), date(2026, 9, 1),
                       LAT, LON, sleep=no_sleep)

    assert len(client.calls) == 1


# --- command line ------------------------------------------------------


def test_days_shortcut_counts_back_from_the_end_date():
    args = fw.parse_args(["--source", "archive", "--days", "30", "--end", "2026-09-20"])

    assert args.start == date(2026, 8, 22)  # 30 days inclusive
    assert args.end == date(2026, 9, 20)


def test_archive_end_date_defaults_to_five_days_ago():
    args = fw.parse_args(["--source", "archive", "--days", "7"])

    assert args.end == datetime.now(timezone.utc).date() - timedelta(days=fw.ARCHIVE_LAG_DAYS)


def test_historical_forecast_end_date_defaults_to_today():
    args = fw.parse_args(["--source", "historical_forecast", "--days", "7"])

    assert args.end == datetime.now(timezone.utc).date()


def test_site_coordinates_are_the_default():
    args = fw.parse_args(["--days", "1"])

    assert (args.lat, args.lon) == (-0.70, 36.60)


def test_either_start_or_days_is_required():
    with pytest.raises(SystemExit):
        fw.parse_args(["--source", "archive"])


def test_an_unknown_source_is_refused_on_the_command_line():
    with pytest.raises(SystemExit):
        fw.parse_args(["--source", "vibes", "--days", "1"])
