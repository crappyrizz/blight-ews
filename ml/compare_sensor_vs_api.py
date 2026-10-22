"""Sensor vs API: does the on-farm node see the same Hutton days as the weather archive?

For each local (Africa/Nairobi) day in a date range, the Hutton rule from
backend/hutton.py is applied twice, to the same node location:

- sensor: the node's own readings (sensor_readings);
- API:    the Open-Meteo ERA5-Land archive at the node's latitude/longitude
          (weather_readings, source='archive').

Then, on the days BOTH sources could judge (20 of 24 hours each):

- an agreement table for Hutton days and for Hutton periods
  (both true / sensor only / API only / both false), with Cohen's kappa;
- the difference in humid hours (RH >= 90 %) per day, sensor minus API;
- a timeline figure with both series.

SIMULATED OR REAL. Until the ESP32 node is in the field, the "sensor" series
is tools/simulate_node.py replaying archive weather with a canopy humidity
offset. Results from it say how the simulator differs from the archive, not
how a real sensor does. So every output (console, markdown, JSON and the
figure) carries a large banner saying which it is. The node table has no
such flag, so it is a command-line choice, and the default is SIMULATED:
claiming field results must be a deliberate `--sensor-data real`.

    .\\.venv\\Scripts\\python.exe -m ml.compare_sensor_vs_api
    .\\.venv\\Scripts\\python.exe -m ml.compare_sensor_vs_api --start 2026-08-15 --end 2026-10-06
    .\\.venv\\Scripts\\python.exe -m ml.compare_sensor_vs_api --sensor-data real

Writes docs/sensor_vs_api.md, docs/sensor_vs_api.json and
docs/sensor_vs_api_timeline.png. Reads the database, writes nothing to it.
"""
import argparse
import json
from datetime import UTC, date, datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from backend import hutton  # noqa: E402
from backend.config import REPO_ROOT  # noqa: E402
from backend.models import SensorNode, SensorReading, WeatherReading  # noqa: E402

DOCS = REPO_ROOT / "docs"
REPORT = DOCS / "sensor_vs_api.md"
FIGURE = DOCS / "sensor_vs_api_timeline.png"

SENSOR_KINDS = ("simulated", "real")
BANNERS = {
    "simulated": "SIMULATED SENSOR DATA: NOT FIELD RESULTS",
    "real": "REAL SENSOR DATA FROM THE FIELD NODE",
}
BANNER_DETAIL = {
    "simulated": (
        "The 'sensor' series is tools/simulate_node.py replaying Open-Meteo weather into the ingest API, "
        "with a +4 % RH under-canopy offset, small noise and simulated outages. It shows how the simulator "
        "differs from the archive, NOT how a real sensor would. As of 19 Oct 2026 the stored readings had been "
        "replayed from the weather files as they were before the archive was pinned to ERA5-Land (12 Oct 2026), "
        "which were ECMWF IFS data, so this compares IFS + offset against ERA5-Land."
    ),
    "real": "The 'sensor' series is the ESP32 + SHT31 node's own readings from the plot.",
}

# Validated categorical slots 1-2 (as in ml/eval_risk.py), plus a marker each.
COLOURS = {"sensor": "#2a78d6", "api": "#eb6834"}
BANNER_COLOURS = {"simulated": "#c62828", "real": "#1b7a3d"}
INK, MUTED, GRID, PAPER = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


# --- 1. Pure functions (tested on hand-built tables) ------------------------------


def daily_hutton(readings: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Readings -> one row per local day in [start, end] with the Hutton flags.

    One lead-in day is computed but dropped, because the period on `start`
    needs the day before it. Unknown days keep hutton_day / hutton_period None.
    """
    lead_in = start - timedelta(days=1)
    daily = hutton.daily_summary(hutton.to_hourly(readings), lead_in, end)
    daily = hutton.hutton_periods(hutton.hutton_days(daily))
    return daily[[start <= d <= end for d in daily.index]]


def cohens_kappa(both_true: int, first_only: int, second_only: int, both_false: int) -> float | None:
    """Cohen's kappa for two yes/no raters, from their 2x2 agreement table.

    kappa = (p_o - p_e) / (1 - p_e): observed agreement corrected for the
    agreement two raters would reach by chance with the same yes-rates.
    None when it is undefined (no days, or both raters always say the same
    single answer, so chance agreement is already 1).
    """
    n = both_true + first_only + second_only + both_false
    if n == 0:
        return None
    observed = (both_true + both_false) / n
    first_yes = (both_true + first_only) / n
    second_yes = (both_true + second_only) / n
    chance = first_yes * second_yes + (1 - first_yes) * (1 - second_yes)
    if chance >= 1:
        return None
    return (observed - chance) / (1 - chance)


def agreement(sensor: pd.Series, api: pd.Series) -> dict:
    """2x2 agreement of two True/False/None series on the same dates.

    Days where either source is unknown are counted apart, never as False.
    """
    counts = {"both_true": 0, "sensor_only": 0, "api_only": 0, "both_false": 0, "unknown": 0}
    for d in sensor.index:
        s, a = sensor[d], api.get(d)
        if s is None or a is None or pd.isna(s) or pd.isna(a):
            counts["unknown"] += 1
        elif s and a:
            counts["both_true"] += 1
        elif s:
            counts["sensor_only"] += 1
        elif a:
            counts["api_only"] += 1
        else:
            counts["both_false"] += 1
    judged = counts["both_true"] + counts["sensor_only"] + counts["api_only"] + counts["both_false"]
    counts["judged"] = judged
    counts["agreement"] = (counts["both_true"] + counts["both_false"]) / judged if judged else None
    counts["kappa"] = cohens_kappa(counts["both_true"], counts["sensor_only"],
                                   counts["api_only"], counts["both_false"])
    return counts


def humid_hour_differences(sensor: pd.DataFrame, api: pd.DataFrame) -> dict:
    """Humid hours per day, sensor minus API, on days both sources judged."""
    both = [d for d in sensor.index
            if sensor.loc[d, "status"] == hutton.STATUS_OK and d in api.index
            and api.loc[d, "status"] == hutton.STATUS_OK]
    diff = np.array([sensor.loc[d, "humid_hours"] - api.loc[d, "humid_hours"] for d in both], dtype=float)
    if not len(diff):
        return {"days": 0}
    tmin_diff = np.array([sensor.loc[d, "tmin"] - api.loc[d, "tmin"] for d in both], dtype=float)
    return {
        "days": len(diff),
        "mean": float(diff.mean()),
        "sd": float(diff.std(ddof=1)) if len(diff) > 1 else 0.0,
        "median": float(np.median(diff)),
        "mean_absolute": float(np.abs(diff).mean()),
        "min": float(diff.min()),
        "max": float(diff.max()),
        "sensor_more_humid_days": int((diff > 0).sum()),
        "api_more_humid_days": int((diff < 0).sum()),
        "equal_days": int((diff == 0).sum()),
        "sensor_mean_humid_hours": float(np.mean([sensor.loc[d, "humid_hours"] for d in both])),
        "api_mean_humid_hours": float(np.mean([api.loc[d, "humid_hours"] for d in both])),
        "tmin_mean_difference": float(tmin_diff.mean()),
    }


def failed_condition(day: pd.Series) -> str:
    """Which Hutton condition a judged non-Hutton day missed: 'tmin', 'humidity' or 'both'."""
    cold = day["tmin"] < hutton.MIN_TEMPERATURE_C
    dry = day["humid_hours"] < hutton.MIN_HOURS_AT_RH
    return "both" if cold and dry else ("tmin" if cold else "humidity")


def disagreement_reasons(sensor: pd.DataFrame, api: pd.DataFrame) -> dict:
    """On days the sources disagree about a Hutton day, why the 'no' side said no.

    E.g. {"api_only": {"tmin": 9, "humidity": 2, "both": 0}}: on 9 of the
    API-only days the sensor had enough humid hours but a minimum below 10 C.
    """
    reasons = {side: {"tmin": 0, "humidity": 0, "both": 0} for side in ("sensor_only", "api_only")}
    for d in sensor.index:
        s, a = sensor.loc[d, "hutton_day"], api.loc[d, "hutton_day"] if d in api.index else None
        if s is None or a is None or s == a:
            continue
        side, no_side = ("sensor_only", api) if s else ("api_only", sensor)
        reasons[side][failed_condition(no_side.loc[d])] += 1
    return reasons


def compare(sensor_readings: pd.DataFrame, api_readings: pd.DataFrame, start: date, end: date) -> dict:
    """Everything the report needs, from the two sets of readings."""
    sensor = daily_hutton(sensor_readings, start, end)
    api = daily_hutton(api_readings, start, end)
    status_counts = {
        name: {s: int((daily["status"] == s).sum())
               for s in (hutton.STATUS_OK, hutton.STATUS_INSUFFICIENT, hutton.STATUS_NO_DATA)}
        for name, daily in (("sensor", sensor), ("api", api))
    }
    return {
        "start": str(start),
        "end": str(end),
        "days": len(sensor),
        "status_counts": status_counts,
        "hutton_day": agreement(sensor["hutton_day"], api["hutton_day"]),
        "hutton_period": agreement(sensor["hutton_period"], api["hutton_period"]),
        "humid_hours": humid_hour_differences(sensor, api),
        "disagreement_reasons": disagreement_reasons(sensor, api),
        "daily": [
            {
                "date": str(d),
                "sensor_status": sensor.loc[d, "status"], "api_status": api.loc[d, "status"],
                "sensor_humid_hours": int(sensor.loc[d, "humid_hours"]),
                "api_humid_hours": int(api.loc[d, "humid_hours"]),
                "sensor_hutton_day": sensor.loc[d, "hutton_day"], "api_hutton_day": api.loc[d, "hutton_day"],
                "sensor_hutton_period": sensor.loc[d, "hutton_period"],
                "api_hutton_period": api.loc[d, "hutton_period"],
            }
            for d in sensor.index
        ],
    }


# --- 2. Report --------------------------------------------------------------------


def _f(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def banner_lines(sensor_data: str) -> list[str]:
    """The large banner that opens (and closes) the report."""
    mark = "⚠️ " if sensor_data == "simulated" else ""
    return [f"> # {mark}{BANNERS[sensor_data]}", ">", f"> {BANNER_DETAIL[sensor_data]}", ""]


def agreement_table(a: dict) -> list[str]:
    return [
        "| | API: yes | API: no |",
        "|---|---|---|",
        f"| **Sensor: yes** | {a['both_true']} (both true) | {a['sensor_only']} (sensor only) |",
        f"| **Sensor: no** | {a['api_only']} (API only) | {a['both_false']} (both false) |",
        "",
        f"Days judged by both: {a['judged']} · agreement {_f(a['agreement'])} · "
        f"Cohen's kappa {_f(a['kappa'])} · days either source could not judge: {a['unknown']}",
        "",
    ]


def reason_lines(reasons: dict) -> list[str]:
    """One sentence per kind of disagreement, naming the condition that failed."""
    lines = []
    for side, other, label in (("api_only", "sensor", "API-only"), ("sensor_only", "API", "sensor-only")):
        r = reasons[side]
        total = sum(r.values())
        if total:
            lines.append(
                f"On the {total} {label} Hutton day{'s' if total != 1 else ''}, the {other} failed on minimum temperature "
                f"(< {hutton.MIN_TEMPERATURE_C:g} °C) on {r['tmin']}, on humid hours "
                f"(< {hutton.MIN_HOURS_AT_RH}) on {r['humidity']}, and on both on {r['both']}.")
    return [*lines, ""] if lines else []


def render(result: dict, sensor_data: str, node_id: str, location: str) -> str:
    h = result["humid_hours"]
    s, a = result["status_counts"]["sensor"], result["status_counts"]["api"]
    lines = [
        "# Sensor vs API: Hutton days at the plot",
        "",
        *banner_lines(sensor_data),
        "Generated by `ml/compare_sensor_vs_api.py`. The same Hutton rule (`backend/hutton.py`) on two "
        "data sources at the same location.",
        "",
        f"- Node: `{node_id}` ({location})",
        "- Sensor: the node's readings (`sensor_readings`)",
        "- API: Open-Meteo ERA5-Land archive at the node's coordinates (`weather_readings`, source `archive`)",
        f"- Days: {result['start']} to {result['end']} ({result['days']} local days, Africa/Nairobi)",
        f"- Judged days (20 of 24 hours): sensor {s['ok']}, API {a['ok']}. "
        f"Too few hours: sensor {s['insufficient_data']}, API {a['insufficient_data']}. "
        f"No data: sensor {s['no_data']}, API {a['no_data']}.",
        f"- Run: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        "## Hutton days",
        "",
        *agreement_table(result["hutton_day"]),
        *reason_lines(result["disagreement_reasons"]),
        "## Hutton periods (two Hutton days running, flagged on the second)",
        "",
        *agreement_table(result["hutton_period"]),
        "## Humid hours per day (RH ≥ 90 %), sensor minus API",
        "",
    ]
    if h["days"]:
        lines += [
            "| Days | Mean | SD | Median | Mean absolute | Min | Max |",
            "|---|---|---|---|---|---|---|",
            f"| {h['days']} | {h['mean']:+.2f} h | {h['sd']:.2f} h | {h['median']:+.1f} h | "
            f"{h['mean_absolute']:.2f} h | {h['min']:+.0f} h | {h['max']:+.0f} h |",
            "",
            f"Mean humid hours per day: sensor {h['sensor_mean_humid_hours']:.1f}, "
            f"API {h['api_mean_humid_hours']:.1f}. The sensor counted more humid hours on "
            f"{h['sensor_more_humid_days']} days, fewer on {h['api_more_humid_days']}, the same on "
            f"{h['equal_days']}. Mean difference in the daily minimum temperature: "
            f"{h['tmin_mean_difference']:+.2f} °C.",
            "",
        ]
    else:
        lines += ["No day was judged by both sources, so there is nothing to compare.", ""]
    lines += [
        "![Humid hours and Hutton days, sensor vs API](sensor_vs_api_timeline.png)",
        "",
        "## How to read this",
        "",
        "- **Kappa** corrects the raw agreement for chance: 1 is perfect agreement, 0 is no better than two "
        "sources that say yes at the same rates independently. It is undefined (n/a) when both sources give "
        "the same single answer on every day.",
        "- A day either source could not judge is counted apart, never as \"not a Hutton day\".",
        "- Sensor-only days are where the plot looked more favourable to blight than the regional archive; "
        "the API (about 9 km grid, 2 m height) cannot see the wetter air inside a canopy.",
        "",
        *banner_lines(sensor_data),
    ]
    return "\n".join(lines)


def timeline_figure(result: dict, sensor_data: str, path) -> None:
    """Humid hours per day for both sources, with Hutton days marked underneath."""
    days = [date.fromisoformat(r["date"]) for r in result["daily"]]

    def humid(source):
        return [r[f"{source}_humid_hours"] if r[f"{source}_status"] == hutton.STATUS_OK else np.nan
                for r in result["daily"]]

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, facecolor=PAPER,
                                      gridspec_kw={"height_ratios": [3, 1]})
    for ax in (top, bottom):
        ax.set_facecolor(PAPER)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(MUTED)
        ax.tick_params(colors=MUTED, labelsize=9)

    top.grid(True, color=GRID, linewidth=0.8)
    top.set_axisbelow(True)
    for source, label, marker in (("sensor", "Sensor", "o"), ("api", "API (ERA5-Land)", "s")):
        top.plot(days, humid(source), color=COLOURS[source], marker=marker, markersize=4,
                 linewidth=1.8, label=label)
    top.axhline(hutton.MIN_HOURS_AT_RH, color=MUTED, linestyle="--", linewidth=1)
    top.annotate("6 h: Hutton humidity condition", (days[0], hutton.MIN_HOURS_AT_RH),
                 xytext=(4, 4), textcoords="offset points", fontsize=8, color=MUTED)
    top.set_ylim(-0.5, 24.5)
    top.set_ylabel("Hours with RH ≥ 90 %", color=INK, fontsize=10)
    top.legend(frameon=False, fontsize=9, loc="upper right", labelcolor=INK)

    # Bottom strip: a filled marker is a Hutton day, hollow is not, x is unknown.
    for row, source in ((1, "sensor"), (0, "api")):
        for r, d in zip(result["daily"], days, strict=True):
            flag = r[f"{source}_hutton_day"]
            if flag is None:
                bottom.plot(d, row, marker="x", color=MUTED, markersize=5)
            else:
                bottom.plot(d, row, marker="o", markersize=6, color=COLOURS[source],
                            markerfacecolor=COLOURS[source] if flag else PAPER)
    bottom.set_yticks([0, 1], ["API", "Sensor"])
    bottom.set_ylim(-0.6, 1.6)
    bottom.set_xlabel("Local day (Africa/Nairobi)", color=INK, fontsize=10)
    bottom.set_title("Hutton day: filled = yes, hollow = no, x = could not be judged",
                     color=MUTED, fontsize=8, loc="left")
    fig.autofmt_xdate()

    # The banner: big, coloured, and a watermark across the plot for simulated data.
    fig.suptitle(BANNERS[sensor_data], color=BANNER_COLOURS[sensor_data], fontsize=15, fontweight="bold")
    if sensor_data == "simulated":
        top.text(0.5, 0.5, "SIMULATED", transform=top.transAxes, fontsize=54, color="#c62828",
                 alpha=0.10, ha="center", va="center", rotation=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=PAPER)
    plt.close(fig)


# --- 3. Database ------------------------------------------------------------------


def _local_date(ts) -> date:
    return pd.Timestamp(ts).tz_convert(hutton.LOCAL_TZ).date()


def default_node(session) -> SensorNode:
    nodes = session.scalars(select(SensorNode).order_by(SensorNode.node_id)).all()
    if len(nodes) != 1:
        raise SystemExit(f"{len(nodes)} sensor nodes in the database: choose one with --node-id")
    return nodes[0]


def default_range(session, node: SensorNode) -> tuple[date, date]:
    """The local days covered by both the node's readings and the archive at its location."""
    s_first, s_last = session.execute(
        select(func.min(SensorReading.timestamp), func.max(SensorReading.timestamp))
        .where(SensorReading.node_id == node.node_id)).one()
    a_first, a_last = session.execute(
        select(func.min(WeatherReading.timestamp), func.max(WeatherReading.timestamp))
        .where(WeatherReading.source == "archive", WeatherReading.latitude == node.latitude,
               WeatherReading.longitude == node.longitude)).one()
    if s_first is None or a_first is None:
        raise SystemExit("no sensor readings or no archive weather for this node: nothing to compare")
    start = max(_local_date(s_first), _local_date(a_first))
    end = min(_local_date(s_last), _local_date(a_last))
    if start > end:
        raise SystemExit(f"the sensor ({_local_date(s_first)} to {_local_date(s_last)}) and the archive "
                         f"({_local_date(a_first)} to {_local_date(a_last)}) do not overlap")
    return start, end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hutton days from the sensor vs the weather archive.")
    parser.add_argument("--node-id", help="default: the only node in the database")
    parser.add_argument("--start", type=date.fromisoformat, help="first local day (default: first common day)")
    parser.add_argument("--end", type=date.fromisoformat, help="last local day (default: last common day)")
    parser.add_argument("--sensor-data", choices=SENSOR_KINDS, default="simulated",
                        help="where the sensor readings came from (default: simulated)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from backend.db import SessionLocal

    with SessionLocal() as session:
        node = session.get(SensorNode, args.node_id) if args.node_id else default_node(session)
        if node is None:
            parser.error(f"unknown node {args.node_id}")
        first, last = default_range(session, node)
        start, end = args.start or first, args.end or last
        if start > end:
            parser.error("--start is after --end")
        window_start, _ = hutton.local_day_bounds(start - timedelta(days=1))
        _, window_end = hutton.local_day_bounds(end)
        sensor = hutton.load_readings(session, node.node_id, window_start, window_end, "sensor")
        api = hutton.load_readings(session, node.node_id, window_start, window_end, "api")

    result = compare(sensor, api, start, end)
    result.update({"sensor_data": args.sensor_data, "banner": BANNERS[args.sensor_data],
                   "node_id": node.node_id, "location": node.location})
    DOCS.mkdir(exist_ok=True)
    REPORT.write_text(render(result, args.sensor_data, node.node_id, node.location), encoding="utf-8")
    REPORT.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    timeline_figure(result, args.sensor_data, FIGURE)

    bar = "#" * (len(BANNERS[args.sensor_data]) + 8)
    print(f"{bar}\n### {BANNERS[args.sensor_data]} ###\n{bar}")
    print(f"{node.node_id}: {start} to {end}, {result['days']} days")
    for name in ("hutton_day", "hutton_period"):
        g = result[name]
        print(f"{name:14} both {g['both_true']:3}  sensor-only {g['sensor_only']:3}  api-only {g['api_only']:3}  "
              f"neither {g['both_false']:3}  unknown {g['unknown']:3}  agreement {_f(g['agreement'])}  "
              f"kappa {_f(g['kappa'])}")
    h = result["humid_hours"]
    if h["days"]:
        print(f"humid hours/day, sensor - API: mean {h['mean']:+.2f}, SD {h['sd']:.2f}, "
              f"MAE {h['mean_absolute']:.2f} over {h['days']} days")
    print(f"Wrote {REPORT.relative_to(REPO_ROOT)}, docs/sensor_vs_api.json and {FIGURE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
