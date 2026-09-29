"""Turn daily Hutton summaries into training rows for the learned risk model.

The model forecasts whether Hutton conditions will occur at the plot in the
next 24h or 48h. It forecasts infection-favourable WEATHER, not blight.

Two rules here follow from how gaps are recorded:

1. A day we could not judge is never a training target. Dropping the sample
   is right; treating it as "no Hutton day" would teach the model that a
   silent sensor means safe weather, which is exactly backwards.
2. Whether the feature window had a gap is itself a feature
   ('gap_in_window'). The model then learns how much to trust a window built
   from partial data, instead of us pretending the gap was not there.

Input is a list of daily summaries, oldest first, each a dict or object with:
for_date, status, valid_hours, hutton_day, min_temperature,
hours_at_high_humidity  (i.e. what backend.hutton.DaySummary holds).
"""
from dataclasses import asdict, is_dataclass
from datetime import timedelta

STATUS_OK = "ok"
HOURS_IN_DAY = 24
FEATURE_WINDOW_DAYS = 3  # today plus the two days before it
HORIZON_TO_DAYS = {24: 1, 48: 2}


def _as_dict(day):
    if is_dataclass(day):
        return asdict(day)
    if isinstance(day, dict):
        return day
    return {
        field: getattr(day, field)
        for field in (
            "for_date", "status", "valid_hours", "hutton_day",
            "min_temperature", "hours_at_high_humidity",
        )
    }


def _mean(values):
    return sum(values) / len(values)


def window_features(window) -> dict | None:
    """Features for one window of days. None if no day in it can be used.

    Judged days supply the numbers; unjudged days are simply left out of the
    averages and raise the gap flag. Nothing is interpolated, and no
    placeholder value stands in for a missing day.
    """
    judged = [d for d in window if d["status"] == STATUS_OK]
    if not judged:
        return None

    return {
        "min_temperature_mean": _mean([d["min_temperature"] for d in judged]),
        "min_temperature_min": min(d["min_temperature"] for d in judged),
        "high_humidity_hours_mean": _mean([d["hours_at_high_humidity"] for d in judged]),
        "high_humidity_hours_max": max(d["hours_at_high_humidity"] for d in judged),
        "hutton_days_in_window": sum(1 for d in judged if d["hutton_day"]),
        "completeness_mean": _mean([d["valid_hours"] / HOURS_IN_DAY for d in judged]),
        # The honest flag: did we build these numbers from a full window?
        "gap_in_window": int(len(judged) < len(window)),
        "judged_days_in_window": len(judged),
    }


def build_dataset(days, horizon_hours: int) -> list[dict]:
    """Build (features, target) rows for one forecast horizon.

    The target is whether the day `horizon_hours` ahead is a Hutton day.
    Samples whose target day is missing or unjudged are dropped, never
    counted as negatives.
    """
    if horizon_hours not in HORIZON_TO_DAYS:
        raise ValueError(f"Unsupported horizon: {horizon_hours}")
    offset = HORIZON_TO_DAYS[horizon_hours]

    rows = [_as_dict(d) for d in days]
    by_date = {d["for_date"]: d for d in rows}
    dataset = []

    for index, today in enumerate(rows):
        if index + 1 < FEATURE_WINDOW_DAYS:
            continue  # not enough history yet for a full window

        window = rows[index + 1 - FEATURE_WINDOW_DAYS: index + 1]
        # Only a window of consecutive calendar days is usable.
        expected = [today["for_date"] - timedelta(days=n)
                    for n in reversed(range(FEATURE_WINDOW_DAYS))]
        if [d["for_date"] for d in window] != expected:
            continue

        target_date = today["for_date"] + timedelta(days=offset)
        target_day = by_date.get(target_date)
        if target_day is None or target_day["status"] != STATUS_OK:
            continue  # unjudged target: drop the sample, never call it negative

        features = window_features(window)
        if features is None:
            continue  # nothing judged in the window: no honest features

        dataset.append({
            "for_date": today["for_date"],
            "target_date": target_date,
            **features,
            "target_hutton_day": int(bool(target_day["hutton_day"])),
        })

    return dataset
