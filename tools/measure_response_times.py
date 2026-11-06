"""Measure the hosted server's response times (NFR-03) and write docs/response_times.md.

    $env:PERF_PHONE = "07..."; $env:PERF_PASSWORD = "..."     # a TEST account, never the farmer's
    .\\.venv\\Scripts\\python.exe -m tools.measure_response_times
    .\\.venv\\Scripts\\python.exe -m tools.measure_response_times --register --repeats 10

What it times, from this PC over its own internet connection to the server (Render, Frankfurt):

- the very first request of the run, separately: Render's free tier puts an idle server to
  sleep, so the first request after a quiet spell includes the wake-up;
- GET /health, POST /auth/login, and POST /scans with 1 and with 3 photos, each repeated, once
  the server is awake.

The photos are PlantVillage test images (data/splits/test.csv) enlarged to 1600 px and saved
as JPEG quality 88, the size and quality the app itself uploads (app/lib/scan_screen.dart), so
the upload is as heavy as a real one. The account must have NO sensor node: then a leaf check
uses the neutral prior, never sends an SMS, and touches no farmer's data. --register creates it.
Each leaf check is stored on the server like any other (rows in Neon under the test account).
"""
import argparse
import io
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime

import httpx
from PIL import Image

from backend.config import REPO_ROOT

DEFAULT_URL = "https://blight-ews-api.onrender.com"
TIMEOUT_S = 120  # long enough to ride out a wake-up
REPORT = REPO_ROOT / "docs" / "response_times.md"
RESULTS = REPO_ROOT / "docs" / "response_times.json"
APP_MAX_SIDE, APP_QUALITY = 1600, 88  # what the app's image picker produces


def phone_sized_jpegs(count: int) -> list[bytes]:
    """`count` test images, enlarged and compressed the way the app sends a photo."""
    lines = (REPO_ROOT / "data" / "splits" / "test.csv").read_text(encoding="utf-8").splitlines()[1:]
    paths = [REPO_ROOT / line.split(",")[0] for line in lines[:count]]
    out = []
    for path in paths:
        image = Image.open(path).convert("RGB")
        scale = APP_MAX_SIDE / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=APP_QUALITY)
        out.append(buffer.getvalue())
    return out


def timed(client: httpx.Client, method: str, url: str, **kwargs) -> tuple[float, httpx.Response]:
    start = time.perf_counter()
    response = client.request(method, url, **kwargs)
    return time.perf_counter() - start, response


def stats(seconds: list[float]) -> dict:
    ordered = sorted(seconds)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return {"n": len(ordered), "median_s": statistics.median(ordered), "p90_s": p90, "max_s": ordered[-1],
            "min_s": ordered[0]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Time the hosted server and write docs/response_times.md.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--register", action="store_true", help="create the test account first")
    args = parser.parse_args(argv)
    phone, password = os.environ.get("PERF_PHONE"), os.environ.get("PERF_PASSWORD")
    if not phone or not password:
        print("Set PERF_PHONE and PERF_PASSWORD (a test account, never the farmer's).")
        return 1

    photos = phone_sized_jpegs(3)
    sizes_kb = [round(len(p) / 1024) for p in photos]
    timings: dict[str, list[float]] = {"health": [], "login": [], "scan_1_photo": [], "scan_3_photos": []}
    with httpx.Client(timeout=TIMEOUT_S) as client:
        first_s, response = timed(client, "GET", f"{args.url}/health")
        print(f"first request (may include the wake-up): {first_s:.2f} s, HTTP {response.status_code}")
        if args.register:
            r = client.post(f"{args.url}/auth/register",
                            json={"name": "Response-time test", "phone_number": phone, "password": password})
            print(f"register: HTTP {r.status_code}")
        token = client.post(f"{args.url}/auth/login", json={"phone_number": phone, "password": password})
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        nodes = client.get(f"{args.url}/nodes", headers=headers).json()
        if nodes:
            print("This account has a sensor node: refusing, so no SMS can be sent. Use an account without one.")
            return 1
        for _ in range(args.repeats):
            seconds, r = timed(client, "GET", f"{args.url}/health")
            r.raise_for_status()
            timings["health"].append(seconds)
            seconds, r = timed(client, "POST", f"{args.url}/auth/login",
                               json={"phone_number": phone, "password": password})
            r.raise_for_status()
            timings["login"].append(seconds)
            seconds, r = timed(client, "POST", f"{args.url}/scans", headers=headers,
                               files=[("files", ("leaf1.jpg", photos[0], "image/jpeg"))])
            r.raise_for_status()
            timings["scan_1_photo"].append(seconds)
            seconds, r = timed(client, "POST", f"{args.url}/scans", headers=headers,
                               files=[("files", (f"leaf{i}.jpg", p, "image/jpeg")) for i, p in enumerate(photos, 1)])
            r.raise_for_status()
            timings["scan_3_photos"].append(seconds)
            print(f"  round done: scan 1 photo {timings['scan_1_photo'][-1]:.2f} s, "
                  f"3 photos {timings['scan_3_photos'][-1]:.2f} s")

    ran_at = datetime.now(UTC)
    summary = {name: stats(values) for name, values in timings.items()}
    RESULTS.write_text(json.dumps({"ran_at": ran_at.isoformat(), "url": args.url, "first_request_s": first_s,
                                   "photo_kb": sizes_kb, "timings_s": timings, "summary": summary}, indent=2)
                       + "\n",
                       encoding="utf-8")
    names = {"health": "GET /health", "login": "POST /auth/login", "scan_1_photo": "POST /scans, 1 photo",
             "scan_3_photos": "POST /scans, 3 photos"}
    lines = [
        "# Response times of the hosted server (NFR-03)",
        "",
        "Generated by `tools/measure_response_times.py`. Times are end to end, as the app sees them: from this "
        "PC over its internet connection in Kenya to the server on Render (free tier, Frankfurt) and back.",
        "",
        f"- Run: {ran_at:%Y-%m-%d %H:%M UTC}; {args.repeats} rounds, each request once per round, on one "
        "connection (as the app keeps one).",
        f"- Photos: PlantVillage test images enlarged to {APP_MAX_SIDE} px, JPEG quality {APP_QUALITY}, as the app "
        f"uploads them: {', '.join(f'{k} KB' for k in sizes_kb)}.",
        "- Account: a test account with no sensor node, so no SMS and no farmer's data were involved.",
        f"- First request of the run (GET /health): **{first_s:.1f} s**. Render's free tier stops an idle server, "
        "so a first request after a quiet spell includes starting it again; requests after it are timed below.",
        "",
        "| Request | n | Median (s) | 90th percentile (s) | Slowest (s) | Fastest (s) |",
        "|---|---|---|---|---|---|",
    ]
    for key, s in summary.items():
        lines.append(f"| {names[key]} | {s['n']} | {s['median_s']:.2f} | {s['p90_s']:.2f} | {s['max_s']:.2f} | "
                     f"{s['min_s']:.2f} |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT.relative_to(REPO_ROOT)} and {RESULTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
