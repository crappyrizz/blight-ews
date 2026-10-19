"""Tests for ml/make_test_plan.py: the FR table is filled from real pytest results.

pytest is not run again here: the results come from a small hand-written
JUnit XML file in the shape pytest writes.
"""
import ast
import csv

from backend.config import REPO_ROOT
from ml import make_test_plan as tp

TESTS_DIR = REPO_ROOT / "backend" / "tests"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="5" time="12.5">
  <testcase classname="backend.tests.test_auth" name="test_login_returns_a_token" time="0.1"/>
  <testcase classname="backend.tests.test_auth" name="test_hash_and_verify" time="0.1"/>
  <testcase classname="backend.tests.test_ingest" name="test_range[-10.0]" time="0.1"/>
  <testcase classname="backend.tests.test_ingest" name="test_range[50.0]" time="0.1">
    <failure message="assert 0 == 1&#10;second line">trace</failure></testcase>
  <testcase classname="backend.tests.test_hutton" name="test_later" time="0.0">
    <skipped message="not yet"/></testcase>
</testsuite></testsuites>
"""


def junit(tmp_path):
    path = tmp_path / "pytest.xml"
    path.write_text(JUNIT, encoding="utf-8")
    return path


def test_results_are_keyed_by_file_and_test_name(tmp_path):
    results = tp.read_junit(junit(tmp_path))
    assert results["test_auth.py::test_login_returns_a_token"] == ("passed", "")
    assert results["test_hutton.py::test_later"] == ("skipped", "not yet")


def test_a_parametrised_test_fails_if_any_case_fails(tmp_path):
    outcome, message = tp.read_junit(junit(tmp_path))["test_ingest.py::test_range"]
    assert outcome == "failed"
    assert message == "assert 0 == 1"  # first line only


def test_suite_summary(tmp_path):
    assert tp.suite_summary(junit(tmp_path)) == {
        "total": 5, "failed": 1, "skipped": 1, "passed": 3, "seconds": 12.5}


def test_a_row_passes_only_if_every_test_passed(tmp_path):
    results = tp.read_junit(junit(tmp_path))
    good = {"tests": ["test_auth.py::test_login_returns_a_token", "test_auth.py::test_hash_and_verify"]}
    assert tp.outcome_of(good, results) == ("As expected: 2/2 tests passed", "Pass")

    mixed = {"tests": ["test_auth.py::test_hash_and_verify", "test_ingest.py::test_range"]}
    actual, result = tp.outcome_of(mixed, results)
    assert result == "Fail"
    assert "1/2 tests passed" in actual and "test_range failed" in actual


def test_a_skipped_or_missing_test_does_not_pass_the_row(tmp_path):
    results = tp.read_junit(junit(tmp_path))
    assert tp.outcome_of({"tests": ["test_hutton.py::test_later"]}, results)[1] == "Fail"
    actual, result = tp.outcome_of({"tests": ["test_auth.py::test_renamed"]}, results)
    assert result == "Fail" and "not found" in actual


def test_every_named_test_exists():
    """A renamed test must be updated in the plan, not silently dropped from it."""
    defined = {}
    for path in TESTS_DIR.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined[path.name] = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for row in tp.AUTOMATED:
        for test in row["tests"]:
            file, name = test.split("::")
            assert name in defined.get(file, set()), f"{test} is not defined"


def test_every_requirement_has_automated_and_manual_rows():
    manual = tp.read_manual()
    for fr in tp.FRS:
        assert any(r["fr"] == fr for r in tp.AUTOMATED), f"FR {fr} has no automated row"
        assert any(r["FR"] == fr for r in manual), f"FR {fr} has no manual check"


def test_manual_checks_not_done_are_pending_never_pass(tmp_path):
    path = tmp_path / "manual.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=tp.MANUAL_COLUMNS)
        writer.writeheader()
        writer.writerow({"ID": "M-01", "FR": "i", "Actual": "", "Pass/Fail": ""})
        writer.writerow({"ID": "M-02", "FR": "i", "Actual": "Worked", "Pass/Fail": "pass"})
        writer.writerow({"ID": "M-03", "FR": "i", "Actual": "Odd", "Pass/Fail": "maybe"})
    rows = tp.read_manual(path)
    assert [(r["Actual"], r["Pass/Fail"]) for r in rows] == [
        ("Not run yet", "Pending"), ("Worked", "Pass"), ("Odd", "Pending")]


def test_render_has_the_requested_columns_and_ids(tmp_path):
    results = tp.read_junit(junit(tmp_path))
    automated = tp.build(results)
    text = tp.render(automated, tp.read_manual(), tp.suite_summary(junit(tmp_path)), tp.datetime.now(tp.UTC))
    assert "| ID | Module | Test case | Input | Expected | Actual | Pass/Fail |" in text
    assert "| T-01 |" in text and "| M-01 |" in text
    for fr in tp.FRS:
        assert f"## FR {fr}:" in text
