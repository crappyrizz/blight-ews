"""Tests for ml/run_all_evals.py: which steps run, and that one failure does not stop the rest."""
from ml import run_all_evals as run_all


def parse(argv):
    return run_all.build_parser().parse_args(argv)


def names(argv):
    return [name for name, _, _ in run_all.steps(parse(argv))]


def test_every_evaluation_runs_by_default():
    assert names([]) == ["eval_risk", "eval_cnn", "fusion_eval", "compare_sensor_vs_api",
                         "cost_threshold", "make_test_plan"]


def test_sensor_data_defaults_to_simulated_and_is_passed_on():
    steps = {name: argv for name, _, argv in run_all.steps(parse([]))}
    assert steps["compare_sensor_vs_api"] == ["--sensor-data", "simulated"]
    steps = {name: argv for name, _, argv in run_all.steps(parse(["--sensor-data", "real"]))}
    assert steps["compare_sensor_vs_api"] == ["--sensor-data", "real"]


def test_pump_loads_are_passed_on():
    steps = {name: argv for name, _, argv in run_all.steps(parse(["--pump-loads", "12"]))}
    assert steps["cost_threshold"] == ["--pump-loads", "12.0"]
    assert {name: argv for name, _, argv in run_all.steps(parse([]))}["cost_threshold"] == []


def test_skip_tests_leaves_out_the_test_plan():
    assert "make_test_plan" not in names(["--skip-tests"])


def test_a_failing_step_is_reported_and_the_rest_still_run(monkeypatch):
    ran = []

    def ok(argv):
        ran.append("ok")
        return 0

    def broken(argv):
        ran.append("broken")
        raise RuntimeError("no database")

    monkeypatch.setattr(run_all, "steps", lambda args: [("a", broken, []), ("b", ok, [])])
    monkeypatch.setattr(run_all, "headlines", lambda: [])
    assert run_all.main([]) == 1
    assert ran == ["broken", "ok"]


def test_a_non_zero_exit_counts_as_a_failure():
    ok, _, note = run_all.run_step("x", lambda argv: 1, [])
    assert not ok and note == "exit code 1"
    assert run_all.run_step("y", lambda argv: None, [])[0]
