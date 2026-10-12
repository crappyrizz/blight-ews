"""Evaluate the learned risk model against baselines, once, on the held-out test year.

Every method is scored on the SAME test days (those where all of them can
make a prediction), with the same targets from backend/hutton.py:

- learned:            the HistGradientBoosting model (ml/train_risk.py)
- persistence:        "tomorrow will be like today": predict hutton_day(D)
                      for both horizons. (For 48 h: if D is a Hutton day and
                      the weather persists, D+1 completes a Hutton period.)
- climatology:        the base rate of the target for D's calendar month,
                      from the data the final model was trained on (train +
                      validation, never the test year)
- Hutton-on-forecast: the Hutton rule applied to Open-Meteo's
                      historical_forecast weather for D+1 and D+2. For 48 h,
                      a period on D+1 needs D (observed, known at decision
                      time) and D+1 (forecast).

Metrics: precision, recall and F1 at 0.5 and at the threshold chosen on
validation; PR-AUC (average precision); Brier score; reliability curves.
95 % confidence intervals come from a moving-block bootstrap over test days
(blocks of 14 days, because neighbouring days' weather is correlated, so
resampling single days would understate the uncertainty).

Writes docs/risk_eval.md, docs/risk_eval.json and three PNG figures.

    .\\.venv\\Scripts\\python.exe -m ml.eval_risk
"""
import json
import math
from datetime import date, timedelta

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.calibration import calibration_curve  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    f1_score,
    precision_score,
    recall_score,
)

from backend.config import REPO_ROOT  # noqa: E402
from ml import build_features as bf  # noqa: E402
from ml import train_risk  # noqa: E402

DOCS = REPO_ROOT / "docs"
REPORT = DOCS / "risk_eval.md"
METHODS = ["learned", "persistence", "climatology", "forecast"]
METHOD_LABELS = {
    "learned": "Learned model (HGB)",
    "persistence": "Persistence",
    "climatology": "Climatology (monthly)",
    "forecast": "Hutton-on-forecast",
}
BINARY_METHODS = {"persistence", "forecast"}  # they output 0/1, not a probability
NEAR_CONSTANT = 0.03  # a month whose Hutton-day rate is under 3 % (or over 97 %)
BOOTSTRAP_BLOCK_DAYS = 14
BOOTSTRAP_REPS = 1000
SEED = 42

# Validated categorical slots 1-4 (light surface), plus a marker per method,
# so identity never rests on colour alone.
COLOURS = {"learned": "#2a78d6", "persistence": "#eb6834", "climatology": "#1baf7a", "forecast": "#eda100"}
MARKERS = {"learned": "o", "persistence": "s", "climatology": "^", "forecast": "D"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# --- predictions -----------------------------------------------------------------


def monthly_rates(rows: pd.DataFrame, target: str) -> dict[int, float]:
    months = pd.Series([d.month for d in rows.index], index=rows.index)
    return rows[target].groupby(months).mean().to_dict()


def climatology(rows: pd.DataFrame, rates: dict[int, float], fallback: float) -> np.ndarray:
    return np.array([rates.get(d.month, fallback) for d in rows.index])


def _and3(a, b):
    """Three-valued AND: False if either is False; unknown if either is unknown."""
    a, b = bf._known(a), bf._known(b)
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def forecast_predictions(test: pd.DataFrame, forecast_daily: pd.DataFrame) -> dict[int, pd.Series]:
    """Hutton-on-forecast for each test day: 1.0 / 0.0, NaN where not available."""
    fday = forecast_daily["hutton_day"].to_dict()
    y24, y48 = {}, {}
    for d in test.index:
        h1, h2 = fday.get(d + timedelta(days=1)), fday.get(d + timedelta(days=2))
        known_h1 = bf._known(h1)
        y24[d] = np.nan if known_h1 is None else float(known_h1)
        observed_today = test.loc[d, "hutton_day_d0"]  # D itself is observed by then
        period_d1 = _and3(None if np.isnan(observed_today) else bool(observed_today), h1)
        period_d2 = _and3(h1, h2)
        either = bf._or_unknown(period_d1, period_d2)
        y48[d] = np.nan if either is None else float(either)
    return {24: pd.Series(y24), 48: pd.Series(y48)}


# --- metrics ---------------------------------------------------------------------


def scores(y: np.ndarray, p: np.ndarray, threshold: float) -> dict:
    hard_05, hard_t = p >= 0.5, p >= threshold
    return {
        "precision@0.5": precision_score(y, hard_05, zero_division=0),
        "recall@0.5": recall_score(y, hard_05, zero_division=0),
        "f1@0.5": f1_score(y, hard_05, zero_division=0),
        "precision@t": precision_score(y, hard_t, zero_division=0),
        "recall@t": recall_score(y, hard_t, zero_division=0),
        "f1@t": f1_score(y, hard_t, zero_division=0),
        "pr_auc": average_precision_score(y, p) if y.any() else float("nan"),
        "brier": brier_score_loss(y, p),
    }


def block_bootstrap_indices(n: int, rng, block: int = BOOTSTRAP_BLOCK_DAYS, reps: int = BOOTSTRAP_REPS):
    """Resample whole runs of `block` consecutive days, keeping autocorrelation."""
    blocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, blocks))
    return (starts[:, :, None] + np.arange(block)).reshape(reps, -1)[:, :n]


def bootstrap_ci(y, predictions: dict, thresholds: dict, rng) -> dict:
    """95 % CIs per method, and PAIRED intervals for learned-minus-baseline gaps.

    Paired: every method is scored on the same resampled days, so a gap's
    interval shows how consistently the learned model wins on the same weather.
    """
    idx = block_bootstrap_indices(len(y), rng)
    metric_names = ("f1@t", "pr_auc", "brier")
    samples = {m: {k: [] for k in metric_names} for m in predictions}
    for row in idx:
        yb = y[row]
        if not yb.any():
            continue
        for m, p in predictions.items():
            samples[m]["f1@t"].append(f1_score(yb, p[row] >= thresholds[m], zero_division=0))
            samples[m]["pr_auc"].append(average_precision_score(yb, p[row]))
            samples[m]["brier"].append(brier_score_loss(yb, p[row]))

    def interval(values):
        return [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]

    ci = {m: {k: interval(v) for k, v in s.items()} for m, s in samples.items()}
    for m in predictions:
        if m == "learned":
            continue
        for k in metric_names:
            gap = np.array(samples["learned"][k]) - np.array(samples[m][k])
            ci[m][f"learned_minus_this_{k}"] = interval(gap)
    return ci


# --- figures ---------------------------------------------------------------------


def _style(ax):
    ax.set_facecolor("#fcfcfb")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)


def reliability_figure(y, predictions: dict, horizon: int, path) -> None:
    fig, ax = plt.subplots(figsize=(6.2, 5.2), facecolor="#fcfcfb")
    _style(ax)
    ax.plot([0, 1], [0, 1], linestyle="--", color=MUTED, linewidth=1, label="Perfectly calibrated")
    for m in METHODS:
        p = predictions[m]
        if m in BINARY_METHODS:  # two points: how often "no" and "yes" came true
            xs, ys = [], []
            for value in (0.0, 1.0):
                chosen = p == value
                if chosen.any():
                    xs.append(value)
                    ys.append(y[chosen].mean())
            ax.plot(xs, ys, linestyle="none", marker=MARKERS[m], markersize=9, color=COLOURS[m],
                    markeredgecolor="#fcfcfb", markeredgewidth=1.5, label=METHOD_LABELS[m])
        else:
            frac, mean_p = calibration_curve(y, p, n_bins=10, strategy="quantile")
            # Climatology has only 12 distinct values (one per month), so its
            # bins are not a curve: show them as unconnected points.
            line = "none" if m == "climatology" else "-"
            ax.plot(mean_p, frac, marker=MARKERS[m], markersize=8, linewidth=2, linestyle=line,
                    color=COLOURS[m], markeredgecolor="#fcfcfb", markeredgewidth=1.5, label=METHOD_LABELS[m])
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(-0.03, 1.03)
    ax.set_xlabel("Predicted probability of Hutton conditions", color=INK, fontsize=10)
    ax.set_ylabel("Observed frequency on test days", color=INK, fontsize=10)
    title = {24: "Hutton day tomorrow (24 h)", 48: "Hutton period within 48 h"}[horizon]
    ax.set_title(f"Reliability: {title}", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="#fcfcfb")
    plt.close(fig)


def base_rate_figure(monthly: pd.DataFrame, path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 3.8), facecolor="#fcfcfb")
    _style(ax)
    months = list(monthly.index)
    rates = monthly["hutton_day_rate"].values * 100
    ax.grid(False, axis="x")  # bars need only horizontal guides
    ax.bar(months, rates, width=0.7, color=COLOURS["learned"], edgecolor="#fcfcfb", linewidth=2)
    for m, r, flagged in zip(months, rates, monthly["near_constant"], strict=True):
        if flagged:  # label only the months the text talks about
            ax.annotate(f"{r:.1f}%", (m, r), xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=8, color=INK)
    ax.set_xticks(months, ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_ylabel("Hutton days (% of days)", color=INK, fontsize=10)
    ax.set_title("Share of Hutton days by month, Kinangop, 2015-2026 (ERA5-Land)",
                 color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="#fcfcfb")
    plt.close(fig)


# --- the report --------------------------------------------------------------------


def _ci(interval):
    return f"[{interval[0]:.3f}, {interval[1]:.3f}]"


def _gap_text(gap):
    if gap is None:
        return ""
    return f"[{gap[0]:+.3f}, {gap[1]:+.3f}]" + (" ✱" if gap[0] > 0 or gap[1] < 0 else "")


def _f(x, digits=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def evaluate(dataset: pd.DataFrame, forecast_readings: pd.DataFrame, bundles: dict) -> dict:
    split = train_risk.split_for(dataset)
    for bundle in bundles.values():
        assert bundle["split"] == split.as_dict(), "the models were trained on a different split"
        assert bundle["features"] == bf.FEATURES, "feature list changed since training"

    forecast_daily = bf.daily_weather(
        bf.hutton.to_hourly(forecast_readings), split.test_start, split.test_end + timedelta(days=2)
    )
    rng = np.random.default_rng(SEED)
    results = {"split": split.as_dict(), "horizons": {}}

    for horizon, bundle in bundles.items():
        target = bundle["target"]
        data = bf.labelled(dataset, horizon)
        train = train_risk.rows_in(data, split, "train")
        val = train_risk.rows_in(data, split, "val")
        train_val = train_risk.rows_in(data, split, "train", "val")
        test = train_risk.rows_in(data, split, "test")

        # Climatology's threshold is chosen on validation like the model's.
        train_rates = monthly_rates(train, target)
        clim_val = climatology(val, train_rates, train[target].mean())
        clim_threshold = train_risk.best_f1_threshold(val[target], clim_val)
        final_rates = monthly_rates(train_val, target)

        candidates = {
            "learned": pd.Series(bundle["model"].predict_proba(test[bf.FEATURES])[:, 1], index=test.index),
            "persistence": test["hutton_day_d0"],
            "climatology": pd.Series(climatology(test, final_rates, train_val[target].mean()), index=test.index),
            "forecast": forecast_predictions(test, forecast_daily)[horizon],
        }
        coverage = {m: float(s.reindex(test.index).notna().mean()) for m, s in candidates.items()}
        common = test.index[np.all([candidates[m].reindex(test.index).notna() for m in METHODS], axis=0)]
        y = test.loc[common, target].values.astype(int)
        predictions = {m: candidates[m].reindex(common).values.astype(float) for m in METHODS}
        thresholds = {"learned": bundle["threshold"], "climatology": clim_threshold,
                      "persistence": 0.5, "forecast": 0.5}

        metrics = {m: scores(y, predictions[m], thresholds[m]) for m in METHODS}
        ci = bootstrap_ci(y, predictions, thresholds, rng)
        reliability_figure(y, predictions, horizon, DOCS / f"risk_reliability_{horizon}h.png")

        results["horizons"][horizon] = {
            "target": target,
            "test_days": len(test),
            "common_days": len(common),
            "positives": int(y.sum()),
            "base_rate": float(y.mean()),
            "coverage": coverage,
            "thresholds": thresholds,
            "metrics": {m: {k: float(v) for k, v in metrics[m].items()} for m in METHODS},
            "ci95": ci,
            "model_version": bundle["model_version"],
            "params": bundle["params"],
            "validation": bundle["validation"],
            "rows": bundle["rows"],
        }

    # Base rate of Hutton days by month, whole record and test year.
    months = pd.Series([d.month for d in dataset.index], index=dataset.index)
    test_mask = [split.part(d) == "test" for d in dataset.index]
    monthly = pd.DataFrame({
        "days": dataset["hutton_day_d0"].groupby(months).count(),
        "hutton_day_rate": dataset["hutton_day_d0"].groupby(months).mean(),
        "y24_rate": dataset["y24"].groupby(months).mean(),
        "y48_rate": dataset["y48"].groupby(months).mean(),
        "test_year_rate": dataset.loc[test_mask, "hutton_day_d0"].groupby(months[test_mask]).mean(),
    })
    monthly["near_constant"] = (monthly["hutton_day_rate"] < NEAR_CONSTANT) | (
        monthly["hutton_day_rate"] > 1 - NEAR_CONSTANT)
    base_rate_figure(monthly, DOCS / "risk_base_rate_by_month.png")

    # Same Hutton rule, same days, two weather products.
    if not forecast_readings.empty:
        first = max(pd.Timestamp(forecast_readings["timestamp"].min()).tz_convert(bf.hutton.LOCAL_TZ).date()
                    + timedelta(days=1), dataset.index[0])
        last = dataset.index[-1]
        ifs = bf.daily_weather(bf.hutton.to_hourly(forecast_readings), first, last)["hutton_day"]
        era = dataset.loc[[first <= d <= last for d in dataset.index], "hutton_day_d0"]
        both = [d for d in era.index if bf._known(ifs.get(d)) is not None and not np.isnan(era[d])]
        results["product_comparison"] = {
            "period": f"{both[0]} to {both[-1]}, {len(both)} days",
            "era5_land_rate": float(np.mean([era[d] for d in both])),
            "ifs_rate": float(np.mean([float(bool(ifs[d])) for d in both])),
            "agreement": float(np.mean([era[d] == float(bool(ifs[d])) for d in both])),
        }
    results["monthly"] = {int(m): {k: (bool(v) if k == "near_constant" else float(v)) for k, v in row.items()}
                          for m, row in monthly.iterrows()}
    return results


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def render(results: dict, data_end: date, forecast_coverage_note: str) -> str:
    split = results["split"]
    lines = [
        "# Learned risk model: evaluation on the held-out test year",
        "",
        "Generated by `ml/eval_risk.py`. Models trained by `ml/train_risk.py`; features and targets "
        "from `ml/build_features.py`; Hutton days from `backend/hutton.py`.",
        "",
        "## Set-up",
        "",
        f"- **Data:** ERA5-Land reanalysis (Open-Meteo archive API, `models=era5_land`, 0.1°, about 9 km) "
        f"at the farm (−0.70, 36.60), hourly, 2015-01-02 to {data_end}. The model is pinned on purpose: "
        "the API's default serves ECMWF IFS forecast data here for recent years, identical to the "
        "historical-forecast series, which would have scored the forecast baseline against itself.",
        "- **Targets** for each local day D: `y24` = D+1 is a Hutton day; `y48` = a Hutton period is "
        "flagged on D+1 or D+2. Days whose target is unknown are dropped, never counted as negatives.",
        "- **Features** use only data up to the end of day D. `backend/tests/test_build_features.py` changes "
        "or deletes every hour after D and checks that no feature for D moves (and it catches a planted "
        "look-ahead lag and a centred rolling window).",
        f"- **Time-ordered split, never shuffled:** train {split['train'][0]} to {split['train'][1]}, "
        f"validation {split['val'][0]} to {split['val'][1]}, test {split['test'][0]} to {split['test'][1]}. "
        "Two days at each boundary are embargoed because their targets fall in the next split.",
        "- **Tuning** (48-setting grid, PR-AUC) and the **decision threshold** (best F1) were chosen on "
        "validation only; the model was then refit on train + validation and scored once on test.",
        "",
    ]
    for horizon, h in results["horizons"].items():
        title = {24: "24 h: will tomorrow be a Hutton day?", 48: "48 h: will a Hutton period be flagged on D+1 or D+2?"}[horizon]
        lines += [
            f"## {title}",
            "",
            f"Test days: {h['test_days']}; scored on the {h['common_days']} days where every method has a "
            f"prediction. Positives: {h['positives']} (base rate {h['base_rate']:.1%}). "
            f"Coverage: " + ", ".join(f"{METHOD_LABELS[m]} {h['coverage'][m]:.0%}" for m in METHODS) + ".",
            "",
            "| Method | Threshold | Precision | Recall | F1 | F1 @0.5 | PR-AUC | Brier |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for m in METHODS:
            s, t = h["metrics"][m], h["thresholds"][m]
            threshold = "0/1" if m in BINARY_METHODS else f"{t:.2f} (val)"
            lines.append(
                f"| {METHOD_LABELS[m]} | {threshold} | {_f(s['precision@t'])} | {_f(s['recall@t'])} | "
                f"**{_f(s['f1@t'])}** | {_f(s['f1@0.5'])} | {_f(s['pr_auc'])} | {_f(s['brier'])} |"
            )
        lines += [
            "",
            "95 % block-bootstrap intervals (14-day blocks, 1,000 resamples):",
            "",
            "| Method | F1 | PR-AUC | Brier | Learned − this: F1 | Learned − this: PR-AUC | "
            "Learned − this: Brier (negative = learned better) |",
            "|---|---|---|---|---|---|---|",
        ]
        for m in METHODS:
            c = h["ci95"][m]
            gaps = [_gap_text(c.get(f"learned_minus_this_{k}")) for k in ("f1@t", "pr_auc", "brier")]
            lines.append(f"| {METHOD_LABELS[m]} | {_ci(c['f1@t'])} | {_ci(c['pr_auc'])} | {_ci(c['brier'])} | "
                         + " | ".join(gaps) + " |")
        v = h["validation"]
        lines += [
            "",
            f"Model `{h['model_version']}`: {h['params']}. Validation PR-AUC {v['pr_auc']:.3f}, "
            f"F1 {v['f1_at_threshold']:.3f} at the chosen threshold (validation base rate {v['base_rate']:.1%}).",
            "",
            "✱ = the interval excludes 0: a clear difference on this test year.",
            "",
            f"![Reliability, {horizon} h](risk_reliability_{horizon}h.png)",
            "",
        ]

    lines += [
        "## Base rate of Hutton days by month",
        "",
        "| Month | Days | Hutton days | y24 positive | y48 positive | Test year | Near-constant? |",
        "|---|---|---|---|---|---|---|",
    ]
    for m, r in results["monthly"].items():
        lines.append(f"| {MONTH_NAMES[m - 1]} | {int(r['days'])} | {r['hutton_day_rate']:.1%} | "
                     f"{r['y24_rate']:.1%} | {r['y48_rate']:.1%} | "
                     f"{'n/a' if math.isnan(r['test_year_rate']) else format(r['test_year_rate'], '.1%')} | "
                     f"{'yes' if r['near_constant'] else ''} |")
    quiet = [MONTH_NAMES[m - 1] for m, r in results["monthly"].items() if r["near_constant"]]
    lines += [
        "",
        "![Hutton days by month](risk_base_rate_by_month.png)",
        "",
        "## Findings",
        "",
    ]
    rates = {MONTH_NAMES[m - 1]: r["hutton_day_rate"] for m, r in results["monthly"].items()}
    wettest = sorted(rates, key=rates.get, reverse=True)[:2]
    driest = sorted(rates, key=rates.get)[:3]
    if quiet:
        lines.append(
            f"- **Hutton conditions are near-constant in {', '.join(quiet)}**: under "
            f"{NEAR_CONSTANT:.0%} (or over {1 - NEAR_CONSTANT:.0%}) of days in the 2015-2026 record, so "
            "any method that always gives the majority answer is right there, and the scores above are "
            "earned in the other months."
        )
    else:
        lines.append(
            f"- **No month is near-constant** (none under {NEAR_CONSTANT:.0%} or over "
            f"{1 - NEAR_CONSTANT:.0%} Hutton days), but the season dominates: "
            f"{wettest[0]} {rates[wettest[0]]:.0%} and {wettest[1]} {rates[wettest[1]]:.0%} of days, against "
            + ", ".join(f"{m} {rates[m]:.0%}" for m in driest) + ". The test year also differed from the "
            "long-run pattern month by month (last column of the table), which is why climatology does poorly."
        )
    h24, h48 = results["horizons"][24], results["horizons"][48]
    for label, h in (("24 h", h24), ("48 h", h48)):
        best_baseline = max((m for m in METHODS if m != "learned"), key=lambda m: h["metrics"][m]["f1@t"])
        gap = h["ci95"][best_baseline]["learned_minus_this_f1@t"]
        verdict = ("better than" if gap[0] > 0 else "worse than" if gap[1] < 0
                   else "not distinguishable from")
        lines.append(
            f"- **{label}:** learned F1 {h['metrics']['learned']['f1@t']:.3f} vs the best baseline "
            f"({METHOD_LABELS[best_baseline]}) {h['metrics'][best_baseline]['f1@t']:.3f}; on the bootstrap "
            f"the learned model is {verdict} it (F1 gap interval [{gap[0]:+.3f}, {gap[1]:+.3f}]). "
            f"Brier: learned {h['metrics']['learned']['brier']:.3f}, climatology "
            f"{h['metrics']['climatology']['brier']:.3f}."
        )
    lines.append(
        "- **Probabilities, not just yes/no.** Fusion uses the risk as a number r, so calibration matters as "
        "much as F1. Learned Brier score: "
        f"24 h {h24['metrics']['learned']['brier']:.3f} (persistence {h24['metrics']['persistence']['brier']:.3f}, "
        f"climatology {h24['metrics']['climatology']['brier']:.3f}); "
        f"48 h {h48['metrics']['learned']['brier']:.3f} (persistence {h48['metrics']['persistence']['brier']:.3f}, "
        f"climatology {h48['metrics']['climatology']['brier']:.3f}). Persistence and the forecast can only "
        "ever say 0 or 1."
    )
    product = results.get("product_comparison")
    if product:
        lines.append(
            f"- **The answer depends on the weather product.** Over the same days ({product['period']}), "
            f"ERA5-Land gives {product['era5_land_rate']:.0%} Hutton days and the IFS forecast series "
            f"{product['ifs_rate']:.0%}. Part of Hutton-on-forecast's low recall is this systematic "
            "difference between products, not only forecast error, and it is the strongest argument for "
            "measuring at the plot itself with the sensor node."
        )
    lines += [
        f"- {forecast_coverage_note}",
        "",
        "## Caveats",
        "",
        "- **The Hutton-on-forecast baseline is optimistic.** Open-Meteo's historical-forecast series is "
        "stitched from the first hours of successive model runs, so its \"D+1\" values come from runs "
        "issued during D+1, not from a forecast issued at the end of D. A true fixed-lead forecast "
        "(Previous Runs API) would do worse. Treat it as an upper bound for forecast-based Hutton.",
        "- **Targets are reanalysis, not the plot.** ERA5-Land is a ~9 km grid; the sensor under the canopy is "
        "more humid. Predictions from sensor data are a domain shift until sensor history exists to compare.",
        "- **One test year** (~365 days, few positives) gives wide intervals; read differences through the "
        "bootstrap table, not the point estimates alone.",
        "- **PR-AUC for persistence and forecast** is computed from 0/1 predictions, so it is a single "
        "operating point, not a ranking; compare those two on precision, recall and F1.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    from backend.db import SessionLocal

    bundles = {h: joblib.load(train_risk.model_path(h)) for h in bf.HORIZONS}
    data_end = date.fromisoformat(bundles[24]["data"]["data_end"])
    with SessionLocal() as session:
        dataset = train_risk.load_dataset(data_end, session)
        forecast = train_risk.load_weather(session, "historical_forecast")

    results = evaluate(dataset, forecast, bundles)
    split = results["split"]
    if forecast.empty:
        note = "No historical_forecast data is stored, so Hutton-on-forecast could not be scored."
    else:
        first = pd.Timestamp(forecast["timestamp"].min()).date()
        last = pd.Timestamp(forecast["timestamp"].max()).date()
        cov24 = results["horizons"][24]["coverage"]["forecast"]
        note = (f"Hutton-on-forecast coverage: historical_forecast data runs {first} to {last}; it covers "
                f"{cov24:.0%} of the test days ({split['test'][0]} to {split['test'][1]}).")
    REPORT.write_text(render(results, data_end, note), encoding="utf-8")
    (DOCS / "risk_eval.json").write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")

    for horizon, h in results["horizons"].items():
        print(f"\n{horizon} h  (test days scored: {h['common_days']}, positives {h['positives']}, "
              f"base rate {h['base_rate']:.3f})")
        print(f"{'method':24}{'thr':>6}{'prec':>7}{'rec':>7}{'F1':>7}{'F1@.5':>7}{'PR-AUC':>8}{'Brier':>7}")
        for m in METHODS:
            s = h["metrics"][m]
            thr = "0/1" if m in BINARY_METHODS else f"{h['thresholds'][m]:.2f}"
            print(f"{METHOD_LABELS[m]:24}{thr:>6}{s['precision@t']:7.3f}{s['recall@t']:7.3f}"
                  f"{s['f1@t']:7.3f}{s['f1@0.5']:7.3f}{s['pr_auc']:8.3f}{s['brier']:7.3f}")
    print(f"\nWrote {REPORT.relative_to(REPO_ROOT)}, docs/risk_eval.json and 3 figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
