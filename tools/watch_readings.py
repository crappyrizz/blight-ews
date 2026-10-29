"""Watch a node's readings arrive in the hosted database, live: for demos and the proof video.

    .\\.venv\\Scripts\\python.exe -m tools.watch_readings              # node-01, every 5 s
    .\\.venv\\Scripts\\python.exe -m tools.watch_readings wokwi-01 --every 10

Every few seconds it asks the hosted database (Neon) for rows newer than the
last one it printed, and prints each new row as it lands: the reading's ID,
when the device measured it (UTC, the same timestamp the device's serial log
shows), the temperature and humidity, and how long after measuring it was
stored. Next to the device's serial window, every "upload: 200" there is
followed by the same readings appearing here.

It reads only, through Neon's HTTPS query endpoint (port 443, so it works on
networks that block the database port), with the connection string from the
gitignored neon.local.env. Ctrl+C stops it.
"""
import argparse
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from backend.config import REPO_ROOT

ENV_FILE = REPO_ROOT / "neon.local.env"


def parse_ts(value: str) -> datetime:
    """'2026-10-29 11:51:06+00' (as Neon returns it) -> an aware UTC datetime."""
    value = value.strip()
    if value.endswith("+00"):
        value += ":00"
    return datetime.fromisoformat(value.replace(" ", "T")).astimezone(UTC)


def format_row(row: dict, now: datetime) -> str:
    """One stored reading as a line that can be matched to the device's serial log."""
    measured = parse_ts(row["timestamp"])
    age = (now - measured).total_seconds()
    return (f"{row['reading_id']}  {measured:%Y-%m-%dT%H:%M:%SZ}  {float(row['temperature']):5.1f} C  "
            f"{float(row['humidity']):5.1f} % RH   (in the database {age:4.0f} s after it was measured)")


class Neon:
    """Minimal read-only client for Neon's HTTPS SQL endpoint."""

    def __init__(self, url: str):
        self.url = url
        self.endpoint = f"https://{urlsplit(url).hostname}/sql"
        self.client = httpx.Client(transport=httpx.HTTPTransport(retries=3), timeout=30)

    def rows(self, query: str, params: list) -> list[dict]:
        response = self.client.post(self.endpoint, headers={"Neon-Connection-String": self.url},
                                    json={"query": query, "params": params})
        response.raise_for_status()
        return response.json()["rows"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print a node's readings as they arrive in the hosted database.")
    parser.add_argument("node_id", nargs="?", default="node-01")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between checks")
    args = parser.parse_args(argv)

    url = dotenv_values(ENV_FILE).get("DATABASE_URL") if ENV_FILE.exists() else None
    if not url:
        print(f"No DATABASE_URL in {ENV_FILE.name}: the hosted database is not set up on this PC.")
        return 1
    db = Neon(url)

    print(f"Hosted database (Neon): readings stored for {args.node_id}. Checking every {args.every:g} s; "
          "Ctrl+C stops.\n")
    newest = db.rows("select reading_id, timestamp, temperature, humidity from sensor_readings "
                     "where node_id = $1 order by timestamp desc limit 3", [args.node_id])
    total = db.rows("select count(*) as n from sensor_readings where node_id = $1", [args.node_id])[0]["n"]
    print(f"Already stored: {total} readings. The newest:")
    for row in reversed(newest):
        print("  " + format_row(row, datetime.now(UTC)))
    since = newest[0]["timestamp"] if newest else "1970-01-01 00:00:00+00"
    print("\nWaiting for new readings...\n")

    try:
        while True:
            time.sleep(args.every)
            try:
                new = db.rows("select reading_id, timestamp, temperature, humidity from sensor_readings "
                              "where node_id = $1 and timestamp > $2 order by timestamp", [args.node_id, since])
            except httpx.HTTPError as error:
                print(f"  (database check failed: {type(error).__name__}; trying again)")
                continue
            if new:
                now = datetime.now(UTC)
                print(f"[{now:%H:%M:%S} UTC] {len(new)} new reading(s) stored:")
                for row in new:
                    print("  " + format_row(row, now))
                since = new[-1]["timestamp"]
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
