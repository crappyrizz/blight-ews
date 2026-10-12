"""Tests for tools/simulate_node.py: buffer/flush logic and the canopy offset.

No server and no database: the HTTP client is a fake that behaves like the
ingest endpoint (stores each timestamp once, counts repeats as duplicates).
"""
import random
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from tools.simulate_node import (
    MAX_BATCH_SIZE,
    NodeFatalError,
    Sample,
    SimConfig,
    SimulatedNode,
    build_readings,
    interpolate,
    run_replay,
    under_canopy,
)

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeServer:
    """Stands in for POST /nodes/{id}/readings."""

    def __init__(self):
        self.stored: set[str] = set()
        self.requests: list[dict] = []
        self.next_status: list = []  # queued failures: a status code or an exception

    def post(self, url, json, headers):
        self.requests.append({"url": url, "readings": json["readings"], "headers": headers})
        if self.next_status:
            failure = self.next_status.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return FakeResponse(failure, {"detail": "nope"})
        if len(json["readings"]) > MAX_BATCH_SIZE:
            return FakeResponse(413, {"detail": "too many"})
        accepted = duplicates = 0
        for reading in json["readings"]:
            if reading["timestamp"] in self.stored:
                duplicates += 1
            else:
                self.stored.add(reading["timestamp"])
                accepted += 1
        return FakeResponse(200, {"accepted": accepted, "duplicates": duplicates, "rejected": []})


def samples(n, start=T0, minutes=10):
    return [Sample(start + timedelta(minutes=minutes * i), 12.0, 90.0) for i in range(n)]


def make_node(server, **config):
    return SimulatedNode(server, "NODE-TEST", "secret-key", SimConfig(**config), random.Random(1))


# --- canopy offset and cap ---------------------------------------------


def no_noise(**overrides):
    return SimConfig(temperature_noise_sd=0.0, humidity_noise_sd=0.0, **overrides)


def test_default_humidity_offset_is_plus_4():
    result = under_canopy(Sample(T0, 15.0, 70.0), no_noise(), random.Random(0))
    assert result.humidity == 74.0
    assert result.temperature == 15.0


def test_humidity_is_capped_at_100():
    result = under_canopy(Sample(T0, 15.0, 98.0), no_noise(), random.Random(0))
    assert result.humidity == 100.0


def test_noise_never_pushes_humidity_out_of_range():
    config = SimConfig(humidity_noise_sd=5.0)
    rng = random.Random(0)
    high = [under_canopy(Sample(T0, 15.0, 99.0), config, rng).humidity for _ in range(500)]
    low = [under_canopy(Sample(T0, 15.0, 0.0), SimConfig(humidity_offset=-4.0, humidity_noise_sd=5.0), rng).humidity
           for _ in range(500)]
    assert max(high) == 100.0
    assert min(low) == 0.0


def test_offset_is_configurable():
    result = under_canopy(Sample(T0, 15.0, 70.0), no_noise(humidity_offset=0.0), random.Random(0))
    assert result.humidity == 70.0


# --- 10-minute interpolation (simulator only) ---------------------------


def test_interpolates_between_adjacent_hours():
    hourly = [Sample(T0, 10.0, 80.0), Sample(T0 + timedelta(hours=1), 16.0, 92.0)]
    result = interpolate(hourly, 10)

    assert [s.timestamp.minute for s in result] == [0, 10, 20, 30, 40, 50, 0]
    assert result[3].temperature == pytest.approx(13.0)  # half way
    assert result[3].humidity == pytest.approx(86.0)


def test_does_not_bridge_a_gap_in_the_source():
    hourly = [Sample(T0, 10.0, 80.0), Sample(T0 + timedelta(hours=3), 16.0, 92.0)]
    assert len(interpolate(hourly, 10)) == 2  # the two real hours, nothing invented


def test_build_readings_never_includes_the_future(tmp_path):
    (tmp_path / "archive_x.csv").write_text(
        "time,temperature_2m,relative_humidity_2m\n"
        "2026-09-01T00:00,10.0,80\n2026-09-01T01:00,11.0,81\n2026-09-01T02:00,12.0,82\n",
        encoding="utf-8",
    )
    now = T0 + timedelta(hours=1)
    result = build_readings(date(2026, 9, 1), date(2026, 9, 1), no_noise(), random.Random(0),
                            tmp_path, now=now)
    assert result[-1].timestamp == now


# --- buffer and flush ---------------------------------------------------


def test_payload_follows_the_contract():
    server = FakeServer()
    node = make_node(server)
    node.measure(samples(1)[0])
    node.flush()

    request = server.requests[0]
    assert request["url"] == "/nodes/NODE-TEST/readings"
    assert request["headers"] == {"X-Node-Key": "secret-key"}
    assert request["readings"] == [
        {"timestamp": "2026-09-01T00:00:00Z", "temperature": 12.0, "humidity": 90.0, "leaf_wetness": None}
    ]


def test_buffer_is_flushed_in_batches_of_at_most_500():
    server = FakeServer()
    node = make_node(server)
    for sample in samples(1200):
        node.measure(sample)

    assert node.flush() is True
    assert [len(r["readings"]) for r in server.requests] == [500, 500, 200]
    assert node.buffer == []
    assert node.summary.accepted == 1200


def test_backlog_is_sent_newest_first():
    server = FakeServer()
    node = make_node(server)
    for sample in samples(3):
        node.measure(sample)
    node.flush()

    sent = [r["timestamp"] for r in server.requests[0]["readings"]]
    assert sent == sorted(sent, reverse=True)


def test_server_error_keeps_the_buffer():
    server = FakeServer()
    server.next_status = [503]
    node = make_node(server)
    for sample in samples(5):
        node.measure(sample)

    assert node.flush() is False
    assert len(node.buffer) == 5
    assert node.summary.failed_requests == 1

    assert node.flush() is True  # next attempt succeeds
    assert node.summary.accepted == 5


def test_network_error_keeps_the_buffer():
    server = FakeServer()
    server.next_status = [httpx.ConnectError("no route")]
    node = make_node(server)
    node.measure(samples(1)[0])

    assert node.flush() is False
    assert len(node.buffer) == 1


def test_only_confirmed_batches_leave_the_buffer():
    server = FakeServer()
    node = make_node(server)
    for sample in samples(700):
        node.measure(sample)

    # First batch (newest 500) succeeds, second fails.
    original_post = server.post
    calls = {"n": 0}

    def flaky_post(url, json, headers):
        calls["n"] += 1
        if calls["n"] == 2:
            return FakeResponse(500)
        return original_post(url, json, headers)

    server.post = flaky_post
    assert node.flush() is False
    assert len(node.buffer) == 200
    # What is left is the oldest 200: the newest went first and were confirmed.
    assert max(s.timestamp for s in node.buffer) == samples(700)[199].timestamp


def test_lost_response_is_resent_and_counted_as_duplicates():
    server = FakeServer()
    node = make_node(server)
    for sample in samples(4):
        node.measure(sample)

    node.flush(lose_response=True)  # server stored them; node never heard back
    assert len(node.buffer) == 4
    assert node.summary.lost_responses == 1

    node.flush()
    assert node.buffer == []
    assert node.summary.duplicates == 4  # idempotent: replaying cost nothing
    assert node.summary.accepted == 0
    assert node.summary.sent == 8


@pytest.mark.parametrize("status", [401, 413, 422])
def test_unretryable_status_stops_the_node_and_keeps_data(status):
    server = FakeServer()
    server.next_status = [status]
    node = make_node(server)
    node.measure(samples(1)[0])

    with pytest.raises(NodeFatalError):
        node.flush()
    assert len(node.buffer) == 1


# --- connectivity loss over a whole replay ------------------------------


def test_offline_node_buffers_then_flushes_everything():
    server = FakeServer()
    node = make_node(server, offline_probability=1.0, offline_min_hours=3, offline_max_hours=3)
    readings = samples(6 * 24)  # one day at 10-minute intervals

    summary = run_replay(node, readings, speed="max")

    assert summary.offline_periods > 0
    assert summary.offline_hours == 3 * summary.offline_periods
    assert node.buffer == []
    # Every reading reached the server exactly once, despite outages and resends.
    assert len(server.stored) == len(readings)
    # The node only counts what it heard back about; a lost response's
    # readings were stored, then came back as duplicates when resent.
    assert summary.accepted + summary.duplicates >= len(readings)
    assert summary.sent == summary.accepted + summary.duplicates + summary.lost_response_readings


def test_no_upload_while_offline():
    server = FakeServer()
    node = make_node(server, offline_probability=1.0, offline_min_hours=12, offline_max_hours=12)
    for sample in samples(6 * 6):  # six hours, all inside the outage
        node.step(sample)

    # Only the lost-response attempt at the moment the link dropped (empty
    # buffer then, so nothing) — no uploads during the outage.
    assert server.requests == []
    assert len(node.buffer) == 36
