"""The cost-asymmetric alert threshold: when is a spray worth it?

Spraying costs little compared with losing the crop, so an alert should go
out at a much lower probability than 0.5. This script works out how low,
from the farmer's own costs, and checks it on the learned model's test year.

COSTS, per acre (KES):

    spray  C = chemical (2,500) + labour (50 per pump load x pump_loads)
    loss   L = bags lost if blight is missed (20) x price per bag (2,500-5,000)

pump_loads is still unknown, so it is a command-line argument; by default
the report sweeps 5, 10 and 20 and quotes 10 in the headline.

THE THRESHOLD. With a calibrated probability p that blight weather is coming:

    not spraying costs   p x L            (lose the crop if it comes)
    spraying costs       C                (whether it comes or not)

so spraying is worth it when p x L > C, i.e. p > C / L. In the usual form
threshold = C_FP / (C_FP + C_FN), the costs are REGRETS against the right
call: C_FP = C (sprayed for nothing) and C_FN = L - C (a miss loses L, but
the right call would still have cost C), which gives exactly C / L. Writing
C_FN = L instead gives C / (C + L), slightly lower; the report shows both.

THE TEST-YEAR CHECK. The learned model forecasts Hutton weather, not blight,
so a Hutton period stands in for "an infection that costs L if the crop is
unprotected". Each policy is played day by day through the model's test year:

- a spray protects the plot for COVER_DAYS (7) days, starting the day after
  the decision (the model decides on day D's data; p48 is about D+1 and D+2);
- a spell of consecutive Hutton-period days with any day uncovered is ONE missed
  infection, costing L once, however long the spell (partial cover still misses);
- the model sprays when p48 >= threshold and the plot is not already covered.

Policies: never spray; always keep the plot covered (spray every 7 days);
a fixed calendar of 4 sprays per crop (the farmer plants all year, so crops
of CROP_DAYS run back to back); the model at the cost threshold C / L; and the
model at its F1 threshold from training, for comparison.

    .\\.venv\\Scripts\\python.exe -m ml.cost_threshold
    .\\.venv\\Scripts\\python.exe -m ml.cost_threshold --pump-loads 12

Writes docs/cost_threshold.md, docs/cost_threshold.json and two figures.
"""
import argparse
import json
from datetime import UTC, date, datetime

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from backend import hutton  # noqa: E402
from backend.config import REPO_ROOT  # noqa: E402
from ml import build_features as bf  # noqa: E402
from ml import train_risk  # noqa: E402

DOCS = REPO_ROOT / "docs"
REPORT = DOCS / "cost_threshold.md"

# --- the farmer's costs (KES per acre) ---------------------------------------------
CHEMICAL_KES = 2500
LABOUR_PER_LOAD_KES = 50
PUMP_LOADS_SWEEP = (5, 10, 20)
HEADLINE_PUMP_LOADS = 10
BAGS_LOST = 20
PRICE_MIN_KES, PRICE_MAX_KES, PRICE_STEP_KES = 2500, 5000, 500

# --- how sprays and crops work ------------------------------------------------------
COVER_DAYS = 7  # one fungicide spray protects the crop for about a week
CROP_DAYS = 120  # one Shangi crop, planting to harvest; crops run back to back
CALENDAR_SPRAYS = 4  # per crop, evenly spaced

HORIZON = 48  # the 48 h model: "a Hutton period on D+1 or D+2"
POLICIES = ["never", "always", "calendar", "model_cost", "model_f1"]
POLICY_LABELS = {
    "never": "Never spray",
    "always": f"Always covered (spray every {COVER_DAYS} days)",
    "calendar": f"Calendar ({CALENDAR_SPRAYS} sprays per {CROP_DAYS}-day crop)",
    "model_cost": "Model at the cost threshold C / L",
    "model_f1": "Model at its F1 threshold",
}
COLOURS = {"never": "#52514e", "always": "#eb6834", "calendar": "#1baf7a", "model_cost": "#2a78d6"}
INK, MUTED, GRID, PAPER = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


# --- 1. Costs and the threshold (pure, tested) --------------------------------------


def spray_cost(pump_loads: float, chemical: float = CHEMICAL_KES,
               labour_per_load: float = LABOUR_PER_LOAD_KES) -> float:
    """C: one spray of one acre."""
    return chemical + labour_per_load * pump_loads


def loss_if_missed(price_per_bag: float, bags: float = BAGS_LOST) -> float:
    """L: the harvest lost to one missed infection."""
    return bags * price_per_bag


def optimal_threshold(spray: float, loss: float) -> float:
    """C_FP / (C_FP + C_FN) with C_FP = C and C_FN = L - C, which is C / L.

    1.0 (never spray) when spraying costs as much as the loss it prevents.
    """
    c_fp, c_fn = spray, loss - spray
    if c_fn <= 0:
        return 1.0
    return c_fp / (c_fp + c_fn)


def threshold_with_full_loss(spray: float, loss: float) -> float:
    """The same formula with C_FN = L (ignoring that a true positive also pays C)."""
    return spray / (spray + loss)


def prices(low: float = PRICE_MIN_KES, high: float = PRICE_MAX_KES, step: float = PRICE_STEP_KES) -> list[float]:
    return [float(p) for p in np.arange(low, high + step / 2, step)]


# --- 2. Playing a policy through the test year (pure, tested) -----------------------


def covered(n_days: int, spray_days, cover_days: int = COVER_DAYS) -> np.ndarray:
    """True on each day protected by a spray on that day or in the cover_days - 1 before."""
    protected = np.zeros(n_days, dtype=bool)
    for s in spray_days:
        protected[s:s + cover_days] = True
    return protected


def missed_infections(period: np.ndarray, protected: np.ndarray) -> int:
    """Spells of consecutive Hutton-period days with ANY day unprotected; each is one miss.

    A spell is one infection risk however long it lasts, so it costs L at
    most once, and covering part of it does not split it into two misses.
    period: 1 = Hutton period that day, 0 = none, NaN = unknown (never an event).
    """
    misses, in_spell, spell_missed = 0, False, False
    for is_period, safe in zip(period, protected, strict=True):
        if is_period == 1:
            if not in_spell:
                in_spell, spell_missed = True, False
            if not safe and not spell_missed:
                misses += 1
                spell_missed = True
        else:
            in_spell = False
    return misses


def never_schedule(n_days: int) -> list[int]:
    return []


def always_schedule(n_days: int, cover_days: int = COVER_DAYS) -> list[int]:
    return list(range(0, n_days, cover_days))


def calendar_schedule(n_days: int, crop_days: int = CROP_DAYS, sprays: int = CALENDAR_SPRAYS) -> list[int]:
    """`sprays` evenly spaced sprays in each crop, crops back to back from day 0."""
    days = []
    for crop_start in range(0, n_days, crop_days):
        days += [crop_start + round(k * crop_days / sprays) for k in range(sprays)]
    return [d for d in days if d < n_days]


def model_schedule(p48: np.ndarray, threshold: float, cover_days: int = COVER_DAYS) -> list[int]:
    """Spray the day after any day whose p48 reaches the threshold, unless already covered.

    A NaN p48 (the day could not be judged, so the server gives no forecast)
    never triggers a spray.
    """
    days, covered_until = [], -1  # last protected day index
    for d, p in enumerate(p48):
        spray_day = d + 1
        if spray_day >= len(p48) or np.isnan(p) or p < threshold:
            continue
        if spray_day > covered_until:
            days.append(spray_day)
            covered_until = spray_day + cover_days - 1
    return days


def play(spray_days: list[int], period: np.ndarray, spray: float, loss: float,
         cover_days: int = COVER_DAYS) -> dict:
    """Sprays, missed infections and total cost of one schedule."""
    misses = missed_infections(period, covered(len(period), spray_days, cover_days))
    return {"sprays": len(spray_days), "missed": misses,
            "cost": len(spray_days) * spray + misses * loss}


def evaluate_policies(p48: np.ndarray, period: np.ndarray, spray: float, loss: float,
                      f1_threshold: float, cover_days: int = COVER_DAYS,
                      crop_days: int = CROP_DAYS, calendar_sprays: int = CALENDAR_SPRAYS) -> dict:
    n = len(period)
    schedules = {
        "never": never_schedule(n),
        "always": always_schedule(n, cover_days),
        "calendar": calendar_schedule(n, crop_days, calendar_sprays),
        "model_cost": model_schedule(p48, optimal_threshold(spray, loss), cover_days),
        "model_f1": model_schedule(p48, f1_threshold, cover_days),
    }
    return {name: play(days, period, spray, loss, cover_days) for name, days in schedules.items()}


def cost_by_threshold(p48: np.ndarray, period: np.ndarray, spray: float, loss: float,
                      thresholds, cover_days: int = COVER_DAYS) -> list[float]:
    """Test-year cost of the model policy at each threshold (for the figure)."""
    return [play(model_schedule(p48, t, cover_days), period, spray, loss, cover_days)["cost"]
            for t in thresholds]


# --- 3. The test-year data ----------------------------------------------------------


def test_year(dataset, bundle) -> dict:
    """Day-by-day p48 (NaN where the day could not be judged) and Hutton period, test year."""
    split = train_risk.split_for(dataset)
    assert bundle["split"] == split.as_dict(), "the model was trained on a different split"
    test = dataset[[split.part(d) == "test" for d in dataset.index]]
    expected = list(np.arange(np.datetime64(split.test_start), np.datetime64(split.test_end) + 1).astype(date))
    if list(test.index) != expected:
        raise ValueError("the test year must have one row per calendar day")

    p48 = bundle["model"].predict_proba(test[bf.FEATURES])[:, 1]
    # As served (backend/risk_model.py): no forecast from a day that could not be judged.
    p48[(test["status_d0"] != hutton.STATUS_OK).values] = np.nan
    return {
        "dates": list(test.index),
        "p48": p48,
        "period": test["hutton_period_d0"].values.astype(float),
        "split": split.as_dict(),
        "f1_threshold": float(bundle["threshold"]),
        "model_version": bundle["model_version"],
    }


# --- 4. Report and figures ----------------------------------------------------------


def _kes(x: float) -> str:
    return f"{x:,.0f}"


def _style(ax):
    ax.set_facecolor(PAPER)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)


def threshold_figure(table: dict, pump_loads, path) -> None:
    """Cost threshold C / L against the price of a bag, one line per pump-load setting."""
    fig, ax = plt.subplots(figsize=(7, 4.2), facecolor=PAPER)
    _style(ax)
    shades = ["#9ec5f0", "#2a78d6", "#0d3f80"]
    for loads, shade in zip(pump_loads, shades, strict=False):
        rows = table[str(loads)]
        ax.plot([r["price"] for r in rows], [r["threshold"] * 100 for r in rows], marker="o",
                markersize=5, linewidth=2, color=shade, label=f"{loads} pump loads (C = KES {_kes(rows[0]['spray'])})")
    ax.set_xlabel("Price of a bag (KES)", color=INK, fontsize=10)
    ax.set_ylabel("Spray when p48 is at least (%)", color=INK, fontsize=10)
    ax.set_ylim(bottom=0)
    ax.set_title(f"Cost threshold C / L, with L = {BAGS_LOST} bags x price", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=PAPER)
    plt.close(fig)


def cost_curve_figure(thresholds, curve, baselines: dict, t_star: float, spray: float, loss: float, path) -> None:
    """Test-year cost of the model policy across thresholds, against the fixed policies."""
    fig, ax = plt.subplots(figsize=(7.5, 4.4), facecolor=PAPER)
    _style(ax)
    ax.plot(thresholds, np.array(curve) / 1000, color=COLOURS["model_cost"], linewidth=2.2,
            label="Model, at each threshold")
    for name in ("never", "always", "calendar"):
        ax.axhline(baselines[name]["cost"] / 1000, color=COLOURS[name], linestyle="--", linewidth=1.4,
                   label=POLICY_LABELS[name])
    ax.axvline(t_star, color=INK, linewidth=1, linestyle=":")
    ax.annotate(f"C / L = {t_star:.3f}", (t_star, 0), xycoords=("data", "axes fraction"), xytext=(4, 6),
                textcoords="offset points", fontsize=8, color=INK)
    ax.set_xlabel("Model threshold on p48", color=INK, fontsize=10)
    ax.set_ylabel("Test-year cost (KES thousands per acre)", color=INK, fontsize=10)
    ax.set_title(f"Test-year cost, C = KES {_kes(spray)}, L = KES {_kes(loss)}", color=INK, fontsize=11, loc="left")
    top = max(max(curve), *(baselines[n]["cost"] for n in ("never", "always", "calendar")))
    ax.set_ylim(0, top / 1000 * 1.35)  # plotted in thousands
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, loc="upper left", ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=PAPER)
    plt.close(fig)


def render(results: dict) -> str:
    h = results["headline"]
    y = results["test_year"]
    price_cols = results["headline_prices"]
    lines = [
        "# Cost-asymmetric alert threshold",
        "",
        "Generated by `ml/cost_threshold.py`. When is a spray worth it, given the farmer's costs?",
        "",
        f"- Run: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        f"- Model: `{y['model_version']}` (48 h: a Hutton period on D+1 or D+2), test year "
        f"{y['split']['test'][0]} to {y['split']['test'][1]} ({y['days']} days, {y['period_days']} Hutton-period "
        f"days in {y['spells']} spells, {y['unknown_period_days']} days unknown, {y['no_forecast_days']} days "
        "with no forecast).",
        "",
        "## Costs (KES per acre)",
        "",
        f"- Spray: C = chemical {_kes(CHEMICAL_KES)} + labour {LABOUR_PER_LOAD_KES} × pump loads. "
        "Pump loads per acre are not known yet, so three values are shown: "
        + ", ".join(f"{n} loads → C = {_kes(spray_cost(n))}" for n in results["pump_loads"]) + ".",
        f"- Missed blight: L = {BAGS_LOST} bags × price, KES {_kes(PRICE_MIN_KES)} to {_kes(PRICE_MAX_KES)} a bag "
        f"→ L = {_kes(loss_if_missed(PRICE_MIN_KES))} to {_kes(loss_if_missed(PRICE_MAX_KES))}.",
        "",
        "## The threshold",
        "",
        "Spraying costs C whatever happens; not spraying costs L if blight weather comes. With a calibrated "
        "probability p, spraying is worth it when p × L > C, so the threshold is C / L. As "
        "C_FP / (C_FP + C_FN), the costs are regrets: C_FP = C (sprayed for nothing) and C_FN = L − C (a miss "
        "loses L, but the right call would still have cost C). Taking C_FN = L instead gives C / (C + L), "
        "shown for comparison.",
        "",
        "| Price per bag | L | " + " | ".join(f"C / L, {n} loads" for n in results["pump_loads"])
        + f" | C / (C + L), {HEADLINE_PUMP_LOADS} loads |",
        "|---|---|" + "---|" * (len(results["pump_loads"]) + 1),
    ]
    for i, price in enumerate(results["prices"]):
        cells = [f"{results['thresholds'][str(n)][i]['threshold']:.3f}" for n in results["pump_loads"]]
        full = results["thresholds"][str(HEADLINE_PUMP_LOADS)][i]["threshold_full_loss"]
        lines.append(f"| {_kes(price)} | {_kes(loss_if_missed(price))} | " + " | ".join(cells) + f" | {full:.3f} |")
    lines += [
        "",
        "![Cost threshold against the price of a bag](cost_threshold_by_price.png)",
        "",
        f"## The policies on the test year ({HEADLINE_PUMP_LOADS} pump loads, C = KES {_kes(h['spray'])})",
        "",
        f"Each spray protects the crop for {COVER_DAYS} days. A spell of consecutive Hutton-period days with "
        "any day uncovered is one missed infection, costing L once.",
        "",
        "| Policy | Sprays | Missed infections | " + " | ".join(f"Cost at KES {_kes(p)}/bag" for p in price_cols)
        + " |",
        "|---|---|---|" + "---|" * len(price_cols),
    ]
    for name in POLICIES:
        runs = [h["by_price"][str(p)][name] for p in price_cols]
        if name == "model_cost":  # its threshold moves with the price, so sprays and misses can too
            sprays = " / ".join(sorted({str(r["sprays"]) for r in runs}, key=int))
            missed = " / ".join(sorted({str(r["missed"]) for r in runs}, key=int))
        else:
            sprays, missed = str(runs[0]["sprays"]), str(runs[0]["missed"])
        label = POLICY_LABELS[name]
        if name == "model_f1":
            label += f" ({y['f1_threshold']:.2f})"
        if name == "model_cost":
            label += " (" + " / ".join(f"{h['by_price'][str(p)]['threshold']:.3f}" for p in price_cols) + ")"
        lines.append(f"| {label} | {sprays} | {missed} | " + " | ".join(_kes(r["cost"]) for r in runs) + " |")

    best = h["best_fixed_policy"]
    lines += [
        "",
        "![Test-year cost against the model threshold](cost_by_threshold.png)",
        "",
        "## Other pump-load settings (cost at the middle price)",
        "",
        "| Pump loads | C | C / L | " + " | ".join(POLICY_LABELS[n] for n in POLICIES) + " |",
        "|---|---|---|" + "---|" * len(POLICIES),
    ]
    for loads in results["pump_loads"]:
        row = results["by_loads_mid_price"][str(loads)]
        lines.append(f"| {loads} | {_kes(row['spray'])} | {row['threshold']:.3f} | "
                     + " | ".join(_kes(row["policies"][n]["cost"]) for n in POLICIES) + " |")
    lines += [
        "",
        "## What this shows",
        "",
        f"- Losing the crop costs {h['loss_to_spray_low']:.0f} to {h['loss_to_spray_high']:.0f} times as much as "
        f"one spray, so the cost threshold is low: spray once the chance of a Hutton period in the next "
        f"48 h reaches {h['threshold_high_price'] * 100:.1f} % (KES {_kes(PRICE_MAX_KES)} a bag) to "
        f"{h['threshold_low_price'] * 100:.1f} % (KES {_kes(PRICE_MIN_KES)}). The F1 threshold "
        f"({y['f1_threshold']:.2f}) treats a false alarm and a miss as equally bad, which the farmer's costs "
        "say they are not.",
        f"- At the middle price, the model at its cost threshold costs KES {_kes(h['model_cost_mid'])} over the "
        f"test year, against KES {_kes(h['best_fixed_cost_mid'])} for the best fixed policy "
        f"({POLICY_LABELS[best].lower().replace(' (', ': ').rstrip(')')}).",
        f"- But C / L is not the cheapest threshold here. Played through the test year, the cheapest is "
        f"{h['cheapest_threshold_mid']:.2f} (KES {_kes(h['cheapest_cost_mid'])}): one spray covers a week, so "
        "spraying at every small chance mostly renews cover that was not needed, and C / L (which prices a "
        "single one-day decision) does not see that. Every threshold from "
        f"{h['near_cheapest_from']:.2f} to {h['near_cheapest_to']:.2f} costs within 10 % of the cheapest, and the "
        f"F1 threshold ({y['f1_threshold']:.2f}) "
        + ("lands in that range by coincidence, not by design." if h["f1_near_cheapest"]
           else "is outside that range."),
        "- The model rarely misses a spell at any low threshold because a Hutton period needs a Hutton day "
        "first, and the model has already seen that day when it decides. The calendar's sprays touch "
        f"{h['calendar_touched']} of the {y['spells']} spells but cover only {h['calendar_fully_covered']} "
        "of them completely; it is not tied to the weather, so it is little better than never spraying.",
        "",
        "## Assumptions (state these with the numbers)",
        "",
        "- **Weather, not disease.** A Hutton period stands in for an infection that costs L if the crop is "
        "unprotected. Not every Hutton period causes blight, so L per spell overstates misses where inoculum is "
        "absent; the farmer's symptom reports (`symptom_reports`) are the data that can calibrate this.",
        f"- **A spray fully protects for {COVER_DAYS} days** and nothing protects after that. Rain wash-off and "
        "systemic products change this.",
        f"- **The calendar** assumes year-round planting (the farmer's answers suggest he plants all year): "
        f"{CALENDAR_SPRAYS} evenly spaced sprays per {CROP_DAYS}-day crop, crops back to back.",
        "- **C / L assumes a calibrated probability** and a single decision. With cover lasting a week, one "
        "spray answers several days, so the cheapest threshold on the test year can differ from C / L; the "
        "cost curve shows by how much.",
        "- **One test year**, with its own weather; a wetter or drier year moves every number.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cost-asymmetric alert threshold and its test-year cost.")
    parser.add_argument("--pump-loads", type=float, nargs="+", default=list(PUMP_LOADS_SWEEP),
                        help=f"pump loads per acre (default: sweep {PUMP_LOADS_SWEEP}, headline {HEADLINE_PUMP_LOADS})")
    parser.add_argument("--price-min", type=float, default=PRICE_MIN_KES)
    parser.add_argument("--price-max", type=float, default=PRICE_MAX_KES)
    parser.add_argument("--bags", type=float, default=BAGS_LOST)
    parser.add_argument("--cover-days", type=int, default=COVER_DAYS)
    parser.add_argument("--crop-days", type=int, default=CROP_DAYS)
    args = parser.parse_args(argv)

    pump_loads = [int(n) if float(n).is_integer() else n for n in args.pump_loads]
    headline_loads = HEADLINE_PUMP_LOADS if HEADLINE_PUMP_LOADS in pump_loads else pump_loads[0]
    price_list = prices(args.price_min, args.price_max)
    headline_prices = [price_list[0], (price_list[0] + price_list[-1]) / 2, price_list[-1]]

    bundle = joblib.load(train_risk.model_path(HORIZON))
    data_end = date.fromisoformat(bundle["data"]["data_end"])
    dataset = train_risk.load_dataset(data_end)
    year = test_year(dataset, bundle)
    p48, period = year["p48"], year["period"]

    def policies_at(spray, loss):
        return evaluate_policies(p48, period, spray, loss, year["f1_threshold"], args.cover_days, args.crop_days)

    thresholds = {
        str(n): [{"price": p, "spray": spray_cost(n), "loss": loss_if_missed(p, args.bags),
                  "threshold": optimal_threshold(spray_cost(n), loss_if_missed(p, args.bags)),
                  "threshold_full_loss": threshold_with_full_loss(spray_cost(n), loss_if_missed(p, args.bags))}
                 for p in price_list]
        for n in pump_loads
    }
    spray = spray_cost(headline_loads)
    by_price = {}
    for p in headline_prices:
        loss = loss_if_missed(p, args.bags)
        by_price[str(p)] = {"threshold": optimal_threshold(spray, loss), **policies_at(spray, loss)}
    mid = str(headline_prices[1])
    fixed = {n: by_price[mid][n]["cost"] for n in ("never", "always", "calendar")}
    best_fixed = min(fixed, key=fixed.get)
    mid_loss = loss_if_missed(headline_prices[1], args.bags)
    by_loads = {str(n): {"spray": spray_cost(n), "threshold": optimal_threshold(spray_cost(n), mid_loss),
                         "policies": policies_at(spray_cost(n), mid_loss)} for n in pump_loads}

    spells = missed_infections(period, np.zeros(len(period), dtype=bool))
    calendar_cover = covered(len(period), calendar_schedule(len(period), args.crop_days), args.cover_days)
    touched = missed_infections(period, ~calendar_cover)  # spells with at least one covered day
    fully = spells - missed_infections(period, calendar_cover)
    results = {
        "pump_loads": pump_loads,
        "prices": price_list,
        "headline_prices": headline_prices,
        "assumptions": {"chemical": CHEMICAL_KES, "labour_per_load": LABOUR_PER_LOAD_KES, "bags": args.bags,
                        "cover_days": args.cover_days, "crop_days": args.crop_days,
                        "calendar_sprays": CALENDAR_SPRAYS, "headline_pump_loads": headline_loads},
        "thresholds": thresholds,
        "test_year": {
            "split": year["split"], "model_version": year["model_version"], "f1_threshold": year["f1_threshold"],
            "days": len(period), "period_days": int(np.nansum(period == 1)), "spells": spells,
            "unknown_period_days": int(np.isnan(period).sum()), "no_forecast_days": int(np.isnan(p48).sum()),
        },
        "headline": {
            "pump_loads": headline_loads, "spray": spray, "by_price": by_price,
            "threshold_low_price": optimal_threshold(spray, loss_if_missed(price_list[0], args.bags)),
            "threshold_high_price": optimal_threshold(spray, loss_if_missed(price_list[-1], args.bags)),
            "loss_to_spray_low": loss_if_missed(price_list[0], args.bags) / spray,
            "loss_to_spray_high": loss_if_missed(price_list[-1], args.bags) / spray,
            "model_cost_mid": by_price[mid]["model_cost"]["cost"],
            "best_fixed_policy": best_fixed, "best_fixed_cost_mid": fixed[best_fixed],
            "calendar_touched": touched, "calendar_fully_covered": fully,
        },
        "by_loads_mid_price": by_loads,
    }

    DOCS.mkdir(exist_ok=True)
    threshold_figure(thresholds, pump_loads, DOCS / "cost_threshold_by_price.png")
    grid = np.round(np.arange(0.0, 0.951, 0.01), 2)
    curve = cost_by_threshold(p48, period, spray, mid_loss, grid, args.cover_days)
    results["headline"]["cheapest_threshold_mid"] = float(grid[int(np.argmin(curve))])
    results["headline"]["cheapest_cost_mid"] = float(min(curve))
    near = grid[np.array(curve) <= 1.10 * min(curve)]
    results["headline"]["near_cheapest_from"] = float(near.min())
    results["headline"]["near_cheapest_to"] = float(near.max())
    results["headline"]["f1_near_cheapest"] = bool(
        cost_by_threshold(p48, period, spray, mid_loss, [year["f1_threshold"]], args.cover_days)[0]
        <= 1.10 * min(curve))
    cost_curve_figure(grid, curve, by_price[mid], by_price[mid]["threshold"], spray, mid_loss,
                      DOCS / "cost_by_threshold.png")
    REPORT.write_text(render(results), encoding="utf-8")
    REPORT.with_suffix(".json").write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")

    t = results["test_year"]
    print(f"Test year {t['split']['test'][0]} to {t['split']['test'][1]}: {t['days']} days, "
          f"{t['period_days']} Hutton-period days in {t['spells']} spells")
    print(f"C = KES {_kes(spray)} ({headline_loads} pump loads); threshold C/L = "
          f"{results['headline']['threshold_high_price']:.3f} (KES {_kes(price_list[-1])}/bag) to "
          f"{results['headline']['threshold_low_price']:.3f} (KES {_kes(price_list[0])}/bag)")
    print(f"Test-year cost at KES {_kes(headline_prices[1])}/bag:")
    for name in POLICIES:
        r = by_price[mid][name]
        print(f"  {POLICY_LABELS[name]:48} sprays {r['sprays']:3}  missed {r['missed']:2}  KES {_kes(r['cost']):>10}")
    print(f"  cheapest model threshold on the test year: {results['headline']['cheapest_threshold_mid']:.2f} "
          f"(KES {_kes(results['headline']['cheapest_cost_mid'])})")
    print(f"Wrote {REPORT.relative_to(REPO_ROOT)}, docs/cost_threshold.json and 2 figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
