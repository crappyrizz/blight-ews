"""Simulated sensor node: stands in for the ESP32 + SHT31 until it exists.

It talks to the backend ONLY through the public ingest API, following
docs/firmware_contract.md exactly (X-Node-Key header, batches of at most
500, buffer until a 200 says the server is done with a reading). It never
writes to the database, so swapping in the real node later changes nothing
on the server side.

Where the data comes from: the hourly Open-Meteo archive CSVs in
data/weather/ (written by ml/fetch_weather.py). Real weather is turned into
plausible node readings by:

- an "under-canopy" humidity offset (default +4 %, capped at 100), because
  air inside a potato canopy is wetter than the 2 m reanalysis value;
- small Gaussian noise on both temperature and humidity;
- a 10-minute sampling interval, linearly interpolated between hourly
  values. This interpolation is a SIMULATOR choice for making realistic
  input; the backend itself never interpolates. Hours missing from the
  source are not bridged: the simulator invents no data across a gap.

Connectivity loss: once per simulated hour, with a configurable
probability, the node "goes offline" for 1-12 hours. It keeps measuring
into its buffer and flushes on reconnect. When an outage begins, the upload
in flight has its response lost: the server stored those readings but the
node never heard back, so it keeps them and sends them again later. That is
the exact case in contract section 6.6, and it is what exercises the
server's duplicate handling. The buffer is sent newest first, so the
backlog arrives out of time order, which the server must also accept.

Run from the repo root (start the API first with .\\dev.ps1 serve):

    .\\.venv\\Scripts\\python.exe -m tools.simulate_node --replay --start 2026-08-15 --end 2026-09-20 --speed max
    .\\.venv\\Scripts\\python.exe -m tools.simulate_node --live --start 2026-09-01 --every 5

--start and --end are local (Africa/Nairobi) calendar days, inclusive.
The node ID and key come from NODE_ID and NODE_KEY in .env (or --node-id /
--node-key).
"""
import argparse
import csv
import os
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

LOCAL_TZ = ZoneInfo("Africa/Nairobi")
WEATHER_DIR = Path("data/weather")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

MAX_BATCH_SIZE = 500  # from the firmware contract; 501 would get a 413
HUMIDITY_CAP_PCT = 100.0
HUMIDITY_FLOOR_PCT = 0.0


@dataclass
class SimConfig:
    """The knobs that make archive weather look like an under-canopy node."""

    humidity_offset: float = 4.0  # % RH added for the canopy microclimate
    temperature_noise_sd: float = 0.2  # deg C, Gaussian
    humidity_noise_sd: float = 1.0  # % RH, Gaussian
    interval_minutes: int = 10  # sampling interval of the simulated node
    flush_every_minutes: int = 60  # how often the node tries to upload
    offline_probability: float = 0.02  # chance per simulated hour of an outage
    offline_min_hours: int = 1
    offline_max_hours: int = 12
    batch_size: int = MAX_BATCH_SIZE
    seed: int = 42


@dataclass
class Sample:
    """One reading as the node measures it (timestamp in UTC)."""

    timestamp: datetime
    temperature: float
    humidity: float

    def to_json(self) -> dict:
        """The exact shape the contract asks for (section 3)."""
        return {
            "timestamp": self.timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "temperature": self.temperature,
            "humidity": self.humidity,
            "leaf_wetness": None,  # no leaf-wetness sensor fitted
        }


@dataclass
class Summary:
    sent: int = 0  # readings put on the wire, resends included
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    lost_responses: int = 0  # batches the server got but the node never heard back from
    lost_response_readings: int = 0
    failed_requests: int = 0  # 5xx / network errors: readings kept and retried
    offline_periods: int = 0
    offline_hours: int = 0
    rejections: list = field(default_factory=list)

    def lines(self) -> list[str]:
        return [
            f"sent:             {self.sent}",
            f"accepted:         {self.accepted}  (as confirmed to the node)",
            f"duplicates:       {self.duplicates}",
            f"rejected:         {self.rejected}",
            f"lost responses:   {self.lost_responses} batches "
            f"({self.lost_response_readings} readings, sent again later)",
            f"failed requests:  {self.failed_requests}",
            f"offline periods:  {self.offline_periods} ({self.offline_hours} hours in total)",
        ]


class NodeFatalError(Exception):
    """A response the contract says not to retry blindly (401, 413, 422)."""


# --- turning archive weather into node readings ------------------------


def load_weather(start: date, end: date, weather_dir: Path = WEATHER_DIR) -> list[Sample]:
    """Hourly archive rows covering local days start..end, oldest first.

    Reads every archive_*.csv (they may overlap; each hour is kept once).
    The CSV times are UTC, as fetch_weather.py requests them. Hours the API
    returned as empty are skipped, so a gap in the source stays a gap.
    """
    window_start = datetime.combine(start, datetime.min.time(), tzinfo=LOCAL_TZ)
    window_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ)

    by_time: dict[datetime, Sample] = {}
    for path in sorted(weather_dir.glob("archive_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row["temperature_2m"] or not row["relative_humidity_2m"]:
                    continue
                moment = datetime.fromisoformat(row["time"]).replace(tzinfo=UTC)
                if window_start <= moment < window_end:
                    by_time[moment] = Sample(
                        moment, float(row["temperature_2m"]), float(row["relative_humidity_2m"])
                    )
    return [by_time[moment] for moment in sorted(by_time)]


def interpolate(hourly: list[Sample], interval_minutes: int) -> list[Sample]:
    """Hourly samples -> one sample every interval_minutes (simulator only).

    Linear between two hours that are exactly one hour apart. Across a gap
    in the source nothing is generated: a real node would have measured
    something, but we do not know what, and making it up would hide the gap.
    """
    samples = []
    steps = 60 // interval_minutes
    for current, following in zip(hourly, hourly[1:] + [None], strict=True):
        samples.append(current)
        if following is None or following.timestamp - current.timestamp != timedelta(hours=1):
            continue
        for step in range(1, steps):
            fraction = step / steps
            samples.append(Sample(
                current.timestamp + timedelta(minutes=step * interval_minutes),
                current.temperature + fraction * (following.temperature - current.temperature),
                current.humidity + fraction * (following.humidity - current.humidity),
            ))
    return samples


def under_canopy(sample: Sample, config: SimConfig, rng: random.Random) -> Sample:
    """Apply the canopy humidity offset and sensor noise to one sample.

    Humidity is capped at 100 % (and floored at 0 %), as a real sensor's
    output would be; the cap is applied after the noise, so noise cannot
    push a reading past what the contract allows.
    """
    humidity = sample.humidity + config.humidity_offset + rng.gauss(0, config.humidity_noise_sd)
    humidity = min(max(humidity, HUMIDITY_FLOOR_PCT), HUMIDITY_CAP_PCT)
    temperature = sample.temperature + rng.gauss(0, config.temperature_noise_sd)
    return Sample(sample.timestamp, round(temperature, 2), round(humidity, 2))


def build_readings(start: date, end: date, config: SimConfig, rng: random.Random,
                   weather_dir: Path = WEATHER_DIR, now: datetime | None = None) -> list[Sample]:
    """Everything the node will measure between start and end, in time order.

    Nothing later than `now`: the archive can hold values for hours that
    have not happened yet, and a real node cannot measure the future (the
    server would reject it anyway, contract section 3).
    """
    now = now or datetime.now(UTC)
    hourly = load_weather(start, end, weather_dir)
    samples = [s for s in interpolate(hourly, config.interval_minutes) if s.timestamp <= now]
    return [under_canopy(s, config, rng) for s in samples]


# --- the node: buffer, flush, connectivity ------------------------------


class SimulatedNode:
    """The firmware's upload logic, as the contract describes it.

    `client` is anything with .post(url, json=..., headers=...) returning a
    response with .status_code and .json(), so tests can pass a fake.
    """

    def __init__(self, client, node_id: str, node_key: str, config: SimConfig,
                 rng: random.Random, summary: Summary | None = None):
        self.client = client
        self.node_id = node_id
        self.node_key = node_key
        self.config = config
        self.rng = rng
        self.summary = summary or Summary()
        self.buffer: list[Sample] = []
        self.offline_until: datetime | None = None

    # Buffer first, send second (contract 6.1): a reading is in the buffer
    # before any upload is attempted, so a failed upload cannot lose it.
    def measure(self, sample: Sample) -> None:
        self.buffer.append(sample)

    def is_offline(self, now: datetime) -> bool:
        return self.offline_until is not None and now < self.offline_until

    def maybe_go_offline(self, now: datetime) -> bool:
        """Roll once (per simulated hour) for the start of an outage."""
        if self.is_offline(now) or self.rng.random() >= self.config.offline_probability:
            return False
        hours = self.rng.randint(self.config.offline_min_hours, self.config.offline_max_hours)
        self.offline_until = now + timedelta(hours=hours)
        self.summary.offline_periods += 1
        self.summary.offline_hours += hours
        # The link dropped mid-upload: the server got the batch, the node
        # never saw the 200, so per contract 6.6 it keeps those readings.
        self.flush(lose_response=True)
        return True

    def _post(self, batch: list[Sample]):
        self.summary.sent += len(batch)
        return self.client.post(
            f"/nodes/{self.node_id}/readings",
            json={"readings": [sample.to_json() for sample in batch]},
            headers={"X-Node-Key": self.node_key},
        )

    def flush(self, lose_response: bool = False) -> bool:
        """Send the buffer in batches; drop only what a 200 covers.

        Newest first, so if the link drops again part-way through a long
        backlog, the most recent weather (the part that matters most for a
        24-48h forecast) has already arrived. The server sorts by
        timestamp, so the order it receives readings in does not matter.

        Returns True if the buffer was emptied.
        """
        newest_first = sorted(self.buffer, key=lambda s: s.timestamp, reverse=True)
        sent_ok: set[datetime] = set()

        for i in range(0, len(newest_first), self.config.batch_size):
            batch = newest_first[i:i + self.config.batch_size]
            try:
                response = self._post(batch)
            except httpx.TransportError:
                self.summary.failed_requests += 1
                break  # network problem: keep everything not yet confirmed

            if lose_response:
                self.summary.lost_responses += 1
                self.summary.lost_response_readings += len(batch)
                break  # the node never sees this answer; one batch was in flight

            if response.status_code == 200:
                body = response.json()
                self.summary.accepted += body["accepted"]
                self.summary.duplicates += body["duplicates"]
                self.summary.rejected += len(body["rejected"])
                for rejection in body["rejected"]:
                    self.summary.rejections.append(
                        (batch[rejection["index"]].timestamp, rejection["reason"])
                    )
                # A 200 means the server is done with every row in the batch,
                # rejected ones included: a bad reading never becomes good.
                sent_ok.update(sample.timestamp for sample in batch)
            elif response.status_code in (401, 413, 422):
                # Contract section 5: these need a human, not a retry loop.
                raise NodeFatalError(f"HTTP {response.status_code}: {response.text}")
            else:
                self.summary.failed_requests += 1  # 5xx: keep the data, retry later
                break

        self.buffer = [s for s in self.buffer if s.timestamp not in sent_ok]
        return not self.buffer

    def step(self, sample: Sample) -> None:
        """One tick of simulated time: maybe drop the link, measure, maybe upload."""
        now = sample.timestamp
        on_the_hour = now.minute == 0
        if on_the_hour:
            self.maybe_go_offline(now)
        self.measure(sample)
        if not self.is_offline(now) and self._flush_due(now):
            self.flush()

    def _flush_due(self, now: datetime) -> bool:
        minutes = now.hour * 60 + now.minute
        return minutes % self.config.flush_every_minutes == 0

    def finish(self) -> None:
        """End of the run: the link comes back and the buffer is emptied."""
        self.offline_until = None
        while self.buffer:
            if not self.flush():
                break  # server unreachable; report what is left


# --- modes --------------------------------------------------------------


def run_replay(node: SimulatedNode, readings: list[Sample], speed: str = "max") -> Summary:
    """Backfill a stretch of history. speed is 'max' or simulated seconds per real second."""
    previous = None
    for sample in readings:
        if speed != "max" and previous is not None:
            time.sleep((sample.timestamp - previous).total_seconds() / float(speed))
        node.step(sample)
        previous = sample.timestamp
    node.finish()
    return node.summary


def run_live(node: SimulatedNode, readings: list[Sample], every_seconds: float) -> Summary:
    """For demos: one reading every N real seconds, each uploaded straight away."""
    node.config.flush_every_minutes = node.config.interval_minutes  # upload every reading
    for sample in readings:
        node.step(sample)
        state = "offline, buffering" if node.is_offline(sample.timestamp) else "online"
        print(f"{sample.timestamp.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M} local  "
              f"T={sample.temperature:5.2f}  RH={sample.humidity:6.2f}  "
              f"[{state}, buffer={len(node.buffer)}]")
        time.sleep(every_seconds)
    node.finish()
    return node.summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Simulated sensor node (stands in for the ESP32).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true", help="backfill a date range")
    mode.add_argument("--live", action="store_true", help="post one reading every N seconds")
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="first local day")
    parser.add_argument("--end", type=date.fromisoformat, help="last local day (default: today)")
    parser.add_argument("--speed", default="max",
                        help="replay: 'max', or simulated seconds per real second")
    parser.add_argument("--every", type=float, default=5.0, help="live: seconds between readings")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--node-id", help="default: NODE_ID from .env")
    parser.add_argument("--node-key", help="default: NODE_KEY from .env")
    parser.add_argument("--weather-dir", type=Path, default=WEATHER_DIR)
    defaults = SimConfig()
    parser.add_argument("--humidity-offset", type=float, default=defaults.humidity_offset)
    parser.add_argument("--temperature-noise", type=float, default=defaults.temperature_noise_sd)
    parser.add_argument("--humidity-noise", type=float, default=defaults.humidity_noise_sd)
    parser.add_argument("--interval", type=int, default=defaults.interval_minutes,
                        help="sampling interval in minutes (must divide 60)")
    parser.add_argument("--offline-probability", type=float, default=defaults.offline_probability,
                        help="chance per simulated hour of going offline")
    parser.add_argument("--offline-min-hours", type=int, default=defaults.offline_min_hours)
    parser.add_argument("--offline-max-hours", type=int, default=defaults.offline_max_hours)
    parser.add_argument("--seed", type=int, default=defaults.seed)
    args = parser.parse_args(argv)
    if 60 % args.interval:
        parser.error("--interval must divide 60")
    return args


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    node_id = args.node_id or os.environ.get("NODE_ID")
    node_key = args.node_key or os.environ.get("NODE_KEY")
    if not node_id or not node_key:
        raise SystemExit("Set NODE_ID and NODE_KEY in .env (or pass --node-id / --node-key).")

    end = args.end or datetime.now(LOCAL_TZ).date()
    config = SimConfig(
        humidity_offset=args.humidity_offset,
        temperature_noise_sd=args.temperature_noise,
        humidity_noise_sd=args.humidity_noise,
        interval_minutes=args.interval,
        offline_probability=args.offline_probability,
        offline_min_hours=args.offline_min_hours,
        offline_max_hours=args.offline_max_hours,
        seed=args.seed,
    )
    rng = random.Random(config.seed)
    readings = build_readings(args.start, end, config, rng, args.weather_dir)
    if not readings:
        raise SystemExit(f"No archive weather in {args.weather_dir} for {args.start}..{end}.")
    print(f"{len(readings)} readings from {readings[0].timestamp:%Y-%m-%d %H:%M}Z "
          f"to {readings[-1].timestamp:%Y-%m-%d %H:%M}Z for {node_id}")

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        node = SimulatedNode(client, node_id, node_key, config, rng)
        try:
            if args.replay:
                summary = run_replay(node, readings, args.speed)
            else:
                summary = run_live(node, readings, args.every)
        except KeyboardInterrupt:
            print("Stopped; flushing what is buffered.")
            node.finish()
            summary = node.summary

    print("\nSummary")
    for line in summary.lines():
        print("  " + line)
    if node.buffer:
        print(f"  still buffered:   {len(node.buffer)} (server unreachable at the end)")
    for moment, reason in summary.rejections[:10]:
        print(f"  rejected {moment:%Y-%m-%d %H:%M}Z: {reason}")


if __name__ == "__main__":
    main()
