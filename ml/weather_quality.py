"""Data-quality report for the weather archive pulled by ml/fetch_weather.py.

The Hutton criteria need at least 20 of a day's 24 hourly values, so "how much
data do we actually have" is not a side question: it decides how many days can
be scored at all, and therefore how much of the record is usable for training
and evaluation.

What it reports:
  - the date range covered, and total hourly values;
  - missing hours per month (expected hours minus stored hours);
  - values outside a plausible range for this site;
  - the share of days with >= 20 valid hours, i.e. days the Hutton rule can judge.

Outputs docs/data_quality.md and docs/missing_hours_per_month.png.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m ml.weather_quality --source archive
"""
import argparse
import calendar
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import REPO_ROOT, settings
from backend.hutton import LOCAL_TZ, MIN_VALID_HOURS
from backend.models import WeatherReading

log = logging.getLogger("weather_quality")

REPORT_PATH = REPO_ROOT / "docs" / "data_quality.md"
PLOT_PATH = REPO_ROOT / "docs" / "missing_hours_per_month.png"

HOURS_IN_DAY = 24

# Plausible range for this site (Kinangop, ~2,500 m). Anything outside it is a
# data fault, not weather, and is reported rather than quietly used.
PLAUSIBLE_TEMPERATURE_C = (-10.0, 50.0)
PLAUSIBLE_HUMIDITY_PCT = (0.0, 100.0)


def load_rows(session: Session, source: str, latitude: float, longitude: float) -> list:
    """Hourly weather rows for one source at one point, oldest first."""
    return list(session.scalars(
        select(WeatherReading)
        .where(
            WeatherReading.source == source,
            WeatherReading.latitude == latitude,
            WeatherReading.longitude == longitude,
        )
        .order_by(WeatherReading.timestamp)
    ))


def summarise(rows) -> dict:
    """Count coverage, gaps, out-of-range values and Hutton-ready days.

    Days are local (Africa/Nairobi) calendar days, because that is the day the
    Hutton rule is defined over and the day the farmer lives in.
    """
    if not rows:
        return {
            "total_hours": 0, "first": None, "last": None, "days": 0,
            "hours_per_month": {}, "missing_per_month": {}, "out_of_range": [],
            "hutton_ready_days": 0, "complete_days": 0, "day_hours": {},
        }

    hours_per_month = Counter()
    day_hours = Counter()
    out_of_range = []

    for row in rows:
        moment_utc = row.timestamp.astimezone(timezone.utc)
        hours_per_month[(moment_utc.year, moment_utc.month)] += 1

        local_day = row.timestamp.astimezone(LOCAL_TZ).date()
        day_hours[local_day] += 1

        low_t, high_t = PLAUSIBLE_TEMPERATURE_C
        low_h, high_h = PLAUSIBLE_HUMIDITY_PCT
        if not (low_t <= row.temperature <= high_t):
            out_of_range.append((moment_utc, "temperature", row.temperature))
        elif not (low_h <= row.humidity <= high_h):
            out_of_range.append((moment_utc, "humidity", row.humidity))

    first = rows[0].timestamp.astimezone(timezone.utc)
    last = rows[-1].timestamp.astimezone(timezone.utc)

    # Expected hours per month, trimmed to the covered range at both ends.
    missing_per_month = {}
    for (year, month), stored in sorted(hours_per_month.items()):
        expected = expected_hours_in_month(year, month, first, last)
        missing_per_month[(year, month)] = max(expected - stored, 0)

    # Only count whole local days, i.e. drop the partial days at each end.
    full_days = {
        day: count for day, count in day_hours.items()
        if first.astimezone(LOCAL_TZ).date() < day < last.astimezone(LOCAL_TZ).date()
    }

    return {
        "total_hours": len(rows),
        "first": first,
        "last": last,
        "days": len(day_hours),
        "hours_per_month": dict(hours_per_month),
        "missing_per_month": missing_per_month,
        "out_of_range": out_of_range,
        "hutton_ready_days": sum(1 for c in full_days.values() if c >= MIN_VALID_HOURS),
        "complete_days": sum(1 for c in full_days.values() if c >= HOURS_IN_DAY),
        "full_days": len(full_days),
        "day_hours": dict(day_hours),
    }


def expected_hours_in_month(year: int, month: int, first: datetime, last: datetime) -> int:
    """Hours the month should hold, counting only the part inside the covered range."""
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    month_end = month_start + timedelta(days=days_in_month)

    start = max(month_start, first.replace(minute=0, second=0, microsecond=0))
    end = min(month_end, last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 3600)


def plot_missing_hours(summary: dict, path: Path = PLOT_PATH) -> Path | None:
    """Bar chart of missing hours per month."""
    missing = summary["missing_per_month"]
    if not missing:
        return None

    import matplotlib
    matplotlib.use("Agg")  # no display on a server or in CI
    import matplotlib.pyplot as plt

    labels = [f"{year}-{month:02d}" for year, month in sorted(missing)]
    values = [missing[key] for key in sorted(missing)]

    figure, axes = plt.subplots(figsize=(max(6, len(labels) * 0.5), 4))
    # Plot against positions and label the ticks, rather than passing strings
    # that matplotlib would try to interpret as numbers or dates.
    positions = range(len(labels))
    axes.bar(positions, values, color="#b5651d")
    axes.set_xticks(list(positions))
    axes.set_xticklabels(labels)
    axes.set_title("Missing hourly weather values per month")
    axes.set_ylabel("Missing hours")
    axes.set_xlabel("Month (UTC)")
    axes.tick_params(axis="x", rotation=90)
    if max(values) == 0:
        axes.set_ylim(0, 1)
        axes.text(0.5, 0.5, "No missing hours", transform=axes.transAxes,
                  ha="center", va="center", fontsize=12, color="#444")
    figure.tight_layout()

    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


def render_report(summary: dict, source: str, latitude: float, longitude: float) -> str:
    if summary["total_hours"] == 0:
        return (
            f"# Weather data quality ({source})\n\n"
            f"No rows for source `{source}` at ({latitude}, {longitude}).\n"
            "Run `ml/fetch_weather.py` first.\n"
        )

    missing_total = sum(summary["missing_per_month"].values())
    expected_total = summary["total_hours"] + missing_total
    coverage = 100 * summary["total_hours"] / expected_total if expected_total else 0
    hutton_share = (
        100 * summary["hutton_ready_days"] / summary["full_days"] if summary["full_days"] else 0
    )

    lines = [
        f"# Weather data quality ({source})",
        "",
        "Generated by `ml/weather_quality.py`.",
        "",
        f"- Source: `{source}` at ({latitude}, {longitude})",
        f"- Covered: **{summary['first']:%Y-%m-%d %H:%M} to {summary['last']:%Y-%m-%d %H:%M} UTC**",
        f"- Hourly values stored: **{summary['total_hours']:,}** of {expected_total:,} expected "
        f"({coverage:.1f}% coverage)",
        f"- Missing hours: **{missing_total:,}**",
        f"- Local calendar days touched: {summary['days']} "
        f"({summary['full_days']} whole days, excluding the partial day at each end)",
        "",
        "## Days the Hutton rule can judge",
        "",
        f"A day needs at least {MIN_VALID_HOURS} of {HOURS_IN_DAY} hourly values.",
        "",
        f"- Days with >= {MIN_VALID_HOURS} values: **{summary['hutton_ready_days']} of "
        f"{summary['full_days']} ({hutton_share:.1f}%)**",
        f"- Days with all {HOURS_IN_DAY} values: {summary['complete_days']}",
        f"- Days that cannot be judged: {summary['full_days'] - summary['hutton_ready_days']} "
        "(recorded as `insufficient_data`, never as low risk)",
        "",
        "## Out-of-range values",
        "",
        f"Plausible ranges for this site: temperature {PLAUSIBLE_TEMPERATURE_C[0]} to "
        f"{PLAUSIBLE_TEMPERATURE_C[1]} C, humidity {PLAUSIBLE_HUMIDITY_PCT[0]} to "
        f"{PLAUSIBLE_HUMIDITY_PCT[1]}%.",
        "",
    ]
    if summary["out_of_range"]:
        lines.append(f"**{len(summary['out_of_range'])} value(s) out of range:**")
        lines.append("")
        lines.append("| Timestamp (UTC) | Field | Value |")
        lines.append("|---|---|---|")
        for moment, field, value in summary["out_of_range"][:20]:
            lines.append(f"| {moment:%Y-%m-%d %H:%M} | {field} | {value} |")
        if len(summary["out_of_range"]) > 20:
            lines.append(f"| … | | {len(summary['out_of_range']) - 20} more |")
    else:
        lines.append("None. Every stored value is inside the plausible range.")

    lines += [
        "",
        "## Missing hours per month",
        "",
        "![Missing hours per month](missing_hours_per_month.png)",
        "",
        "| Month (UTC) | Stored | Missing |",
        "|---|---|---|",
    ]
    for key in sorted(summary["missing_per_month"]):
        year, month = key
        lines.append(
            f"| {year}-{month:02d} | {summary['hours_per_month'][key]} | "
            f"{summary['missing_per_month'][key]} |"
        )

    lines += [
        "",
        "## How gaps are treated",
        "",
        "Missing hours are left missing. Nothing is interpolated, because the Hutton rule "
        "counts hours above a humidity threshold, and invented hours would manufacture "
        "threshold crossings. A day below the 20-hour bar is stored with "
        "`status='insufficient_data'` and a NULL score, so it shows up as a gap in the "
        "farmer's history rather than as a calm day.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="archive",
                        choices=["archive", "historical_forecast", "forecast"])
    parser.add_argument("--lat", type=float, default=settings.SITE_LAT)
    parser.add_argument("--lon", type=float, default=settings.SITE_LON)
    args = parser.parse_args(argv)

    from backend.db import SessionLocal

    with SessionLocal() as session:
        rows = load_rows(session, args.source, args.lat, args.lon)

    summary = summarise(rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_report(summary, args.source, args.lat, args.lon), encoding="utf-8"
    )
    plot_missing_hours(summary)

    if summary["total_hours"] == 0:
        log.warning("No weather rows for source %s; run ml/fetch_weather.py first", args.source)
        return 1

    log.info(
        "%s: %s hourly values, %s to %s, %s missing, %s/%s whole days Hutton-ready",
        args.source, summary["total_hours"],
        summary["first"].date(), summary["last"].date(),
        sum(summary["missing_per_month"].values()),
        summary["hutton_ready_days"], summary["full_days"],
    )
    log.info("Wrote docs/data_quality.md and docs/missing_hours_per_month.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
