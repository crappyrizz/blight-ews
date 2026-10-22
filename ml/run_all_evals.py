"""Regenerate every Chapter 5 evaluation output in docs/ with one command.

    .\\.venv\\Scripts\\python.exe -m ml.run_all_evals
    .\\.venv\\Scripts\\python.exe -m ml.run_all_evals --sensor-data real --pump-loads 12
    .\\.venv\\Scripts\\python.exe -m ml.run_all_evals --skip-tests

Steps, in order (each one's own module explains what it measures):

1. ml.eval_risk              learned risk model vs baselines   docs/risk_eval.md
2. ml.eval_cnn               CNN, lab vs field photos          docs/cnn_domain_gap.md
3. ml.fusion_eval            fusion threshold sensitivity      docs/fusion_eval.md
4. ml.compare_sensor_vs_api  sensor vs API Hutton agreement    docs/sensor_vs_api.md
5. ml.cost_threshold         cost-asymmetric alert threshold   docs/cost_threshold.md
6. ml.make_test_plan         FR i-viii test table (runs pytest) docs/test_plan.md

Nothing is retrained: the models in models/ are evaluated as they are
(retrain with ml.train_risk, or in Colab for the CNN). A step that fails
does not stop the others; the summary lists it, and the exit code is 1.
"""
import argparse
import json
import time
import traceback

from backend.config import REPO_ROOT
from ml import (
    compare_sensor_vs_api,
    cost_threshold,
    eval_cnn,
    eval_risk,
    fusion_eval,
    make_test_plan,
)

DOCS = REPO_ROOT / "docs"


def steps(args) -> list[tuple[str, object, list[str]]]:
    """(name, main function, its arguments) for every step that will run."""
    cost_args = ["--pump-loads", *map(str, args.pump_loads)] if args.pump_loads else []
    plan = [
        ("eval_risk", eval_risk.main, []),
        ("eval_cnn", eval_cnn.main, []),
        ("fusion_eval", fusion_eval.main, []),
        ("compare_sensor_vs_api", compare_sensor_vs_api.main, ["--sensor-data", args.sensor_data]),
        ("cost_threshold", cost_threshold.main, cost_args),
    ]
    if not args.skip_tests:
        plan.append(("make_test_plan", make_test_plan.main, []))
    return plan


def run_step(name: str, main, argv: list[str]) -> tuple[bool, float, str]:
    """Run one step's main(); a crash or a non-zero exit is recorded, not raised."""
    print(f"\n{'=' * 70}\n{name} {' '.join(argv)}\n{'=' * 70}", flush=True)
    started = time.perf_counter()
    try:
        code = main(argv)
        ok, note = code in (0, None), "" if code in (0, None) else f"exit code {code}"
    except SystemExit as stop:  # argparse errors and explicit exits
        ok, note = stop.code in (0, None), f"stopped: {stop.code}"
    except Exception as error:  # keep going: the other outputs are still worth regenerating
        traceback.print_exc()
        ok, note = False, f"{type(error).__name__}: {error}"
    return ok, time.perf_counter() - started, note


def _load(name: str) -> dict | None:
    path = DOCS / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def headlines() -> list[str]:
    """One line per result, read back from the JSON each step wrote."""
    lines = []
    risk = _load("risk_eval.json")
    if risk:
        for horizon, h in risk["horizons"].items():
            m = h["metrics"]
            lines.append(f"Risk {horizon} h: learned PR-AUC {m['learned']['pr_auc']:.3f} vs Hutton-on-forecast "
                         f"{m['forecast']['pr_auc']:.3f}; F1 {m['learned']['f1@t']:.3f} vs "
                         f"{m['forecast']['f1@t']:.3f} ({h['common_days']} test days)")
    cnn = _load("cnn_domain_gap.json")
    if cnn and cnn["lab"].get("n"):
        field = cnn["field"]
        field_text = f"field {field['accuracy']:.3f} on {field['n']}" if field.get("n") else "no field photos yet"
        lines.append(f"CNN accuracy: lab {cnn['lab']['accuracy']:.3f} on {cnn['lab']['n']} images; {field_text}")
    fusion = _load("fusion_eval.json")
    if fusion:
        lines.append(f"Fusion: {len(fusion['borderline'])} of {fusion['images']} test images change call with r")
    sensor = _load("sensor_vs_api.json")
    if sensor:
        d, p, h = sensor["hutton_day"], sensor["hutton_period"], sensor["humid_hours"]
        kappa = lambda g: "n/a" if g["kappa"] is None else f"{g['kappa']:.2f}"  # noqa: E731
        lines.append(f"Sensor vs API [{sensor['banner']}]: Hutton-day kappa {kappa(d)} "
                     f"(agreement {d['agreement']:.2f}, {d['judged']} days), period kappa {kappa(p)}"
                     + (f", humid hours/day sensor - API {h['mean']:+.1f}" if h.get("days") else ""))
    cost = _load("cost_threshold.json")
    if cost:
        c, mid = cost["headline"], str(cost["headline_prices"][1])
        policies = c["by_price"][mid]
        lines.append(f"Cost threshold C/L ({c['pump_loads']} pump loads): {c['threshold_high_price']:.3f} to "
                     f"{c['threshold_low_price']:.3f}; test-year KES at mid price: model "
                     f"{policies['model_cost']['cost']:,.0f}, always {policies['always']['cost']:,.0f}, "
                     f"calendar {policies['calendar']['cost']:,.0f}, never {policies['never']['cost']:,.0f}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regenerate all evaluation outputs in docs/.")
    parser.add_argument("--sensor-data", choices=compare_sensor_vs_api.SENSOR_KINDS, default="simulated",
                        help="where the sensor readings came from (default: simulated)")
    parser.add_argument("--pump-loads", type=float, nargs="+",
                        help="pump loads per acre (default: the cost script's 5 / 10 / 20 sweep)")
    parser.add_argument("--skip-tests", action="store_true", help="do not run pytest / rewrite docs/test_plan.md")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    outcomes = [(name, *run_step(name, main, step_args)) for name, main, step_args in steps(args)]

    print(f"\n{'=' * 70}\nSummary\n{'=' * 70}")
    for name, ok, seconds, note in outcomes:
        print(f"  {'ok    ' if ok else 'FAILED'}  {name:24} {seconds:6.1f} s  {note}")
    print("\nHeadline numbers:")
    for line in headlines():
        print(f"  - {line}")
    return 0 if all(ok for _, ok, _, _ in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
