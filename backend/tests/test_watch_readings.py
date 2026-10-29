"""Tests for tools/watch_readings.py: Neon's timestamps and the printed line. No network."""
from datetime import UTC, datetime

from tools import watch_readings as wr


def test_neon_timestamps_parse_as_utc():
    assert wr.parse_ts("2026-10-29 11:51:06+00") == datetime(2026, 10, 29, 11, 51, 6, tzinfo=UTC)
    assert wr.parse_ts("2026-10-29 14:51:06+03:00") == datetime(2026, 10, 29, 11, 51, 6, tzinfo=UTC)


def test_a_row_reads_like_the_devices_serial_log():
    row = {"reading_id": "RD-ABC123", "timestamp": "2026-10-29 11:51:06+00", "temperature": 26.3, "humidity": 59.1}
    line = wr.format_row(row, datetime(2026, 10, 29, 11, 51, 40, tzinfo=UTC))
    assert line.startswith("RD-ABC123  2026-10-29T11:51:06Z")  # the same timestamp format as the serial log
    assert " 26.3 C" in line and " 59.1 % RH" in line
    assert "34 s after it was measured" in line
