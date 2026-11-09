"""Tests for ml/make_test_plan.py: the FR table is filled from real pytest and flutter test results.

Neither suite is run again here: the results come from a small hand-written
JUnit XML file in the shape pytest writes, and `flutter test --machine` lines
in the shape Flutter writes.
"""
import ast
import csv
import json
import re

from backend.config import REPO_ROOT
from ml import make_test_plan as tp

TESTS_DIR = REPO_ROOT / "backend" / "tests"
APP_TESTS_DIR = REPO_ROOT / "app" / "test"

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
            if test.startswith(tp.APP_PREFIX):
                continue  # app tests: see the next test
            file, name = test.split("::")
            assert name in defined.get(file, set()), f"{test} is not defined"


def _dart_literals(text: str, function: str) -> list[str]:
    """The descriptions passed to group(...) / test(...) / testWidgets(...) in a Dart test file."""
    found = re.findall(rf"\b{function}\(\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")", text)
    return [(single or double).replace("\\'", "'") for single, double in found]


def test_every_named_app_test_exists():
    """An app test the plan names must exist: its full name is '<group> <description>' (or just the description)."""
    for row in tp.AUTOMATED:
        for test in row["tests"]:
            if not test.startswith(tp.APP_PREFIX):
                continue
            file, full_name = test[len(tp.APP_PREFIX):].split("::")
            text = (APP_TESTS_DIR / file).read_text(encoding="utf-8")
            descriptions = _dart_literals(text, "test") + _dart_literals(text, "testWidgets")
            names = set(descriptions) | {f"{g} {d}" for g in _dart_literals(text, "group") for d in descriptions}
            assert full_name in names, f"{test} is not defined in app/test/{file}"


MACHINE = [
    '{"protocolVersion":"0.1.1","type":"start","time":0}',
    '{"suite":{"id":0,"platform":"vm","path":"C:/x/app/test/screens_test.dart"},"type":"suite","time":0}',
    '{"test":{"id":1,"name":"loading C:/x/app/test/screens_test.dart","suiteID":0,"groupIDs":[]},'
    '"type":"testStart","time":1}',
    '{"testID":1,"result":"success","skipped":false,"hidden":true,"type":"testDone","time":5}',
    'not JSON: Flutter progress text',
    '[{"event":"test.startedProcess","params":{"vmServiceUri":null}}]',
    '{"test":{"id":2,"name":"Trend bands come from the server","suiteID":0,"groupIDs":[3]},"type":"testStart"}',
    '{"testID":2,"result":"success","skipped":false,"hidden":false,"type":"testDone"}',
    '{"test":{"id":4,"name":"Trend gaps stay gaps","suiteID":0,"groupIDs":[3]},"type":"testStart"}',
    '{"testID":4,"error":"Expected: 1\\n  Actual: 2","stackTrace":"","isFailure":true,"type":"error"}',
    '{"testID":4,"result":"failure","skipped":false,"hidden":false,"type":"testDone"}',
    '{"test":{"id":5,"name":"later","suiteID":0,"groupIDs":[]},"type":"testStart"}',
    '{"testID":5,"result":"success","skipped":true,"hidden":false,"type":"testDone"}',
]


def test_app_results_are_keyed_by_file_and_full_name():
    results, summary = tp.read_flutter_machine(MACHINE)
    assert results["app:screens_test.dart::Trend bands come from the server"] == ("passed", "")
    assert results["app:screens_test.dart::Trend gaps stay gaps"] == ("failed", "Expected: 1")
    assert results["app:screens_test.dart::later"] == ("skipped", "")
    assert not any("loading" in key for key in results)  # Flutter's hidden loading steps are not tests
    assert summary == {"total": 3, "passed": 1, "failed": 1, "skipped": 1}
    json.loads(MACHINE[0])  # the sample is real JSON


def test_an_app_row_fails_when_its_app_test_failed_or_did_not_run():
    results, _ = tp.read_flutter_machine(MACHINE)
    assert tp.outcome_of({"tests": ["app:screens_test.dart::Trend bands come from the server"]}, results)[1] == "Pass"
    assert tp.outcome_of({"tests": ["app:screens_test.dart::Trend gaps stay gaps"]}, results)[1] == "Fail"
    actual, result = tp.outcome_of({"tests": ["app:screens_test.dart::Trend bands come from the server"]}, {})
    assert result == "Fail" and "not found" in actual  # --no-app: app rows never pass unrun


def test_every_requirement_has_evidence_and_every_function_an_automated_test():
    """Each requirement has at least one row; each functional one (FR-xx) an automated test.

    Some requirements cannot be answered by an automated test (NFR-03 performance is measured,
    NFR-04 usability comes from the farmer), so they need a manual row instead.
    """
    manual = tp.read_manual()
    for fr in tp.FRS:
        automated = any(r["fr"] == fr for r in tp.AUTOMATED)
        assert automated or any(r["FR"] == fr for r in manual), f"{fr} has no test or check at all"
        if fr.startswith("FR-"):
            assert automated, f"{fr} has no automated row"


def test_rows_use_only_the_reports_requirement_ids():
    assert {r["fr"] for r in tp.AUTOMATED} <= set(tp.FRS)
    assert {r["FR"] for r in tp.read_manual()} <= set(tp.FRS)


def test_manual_checks_not_done_are_pending_never_pass(tmp_path):
    path = tmp_path / "manual.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=tp.MANUAL_COLUMNS)
        writer.writeheader()
        writer.writerow({"ID": "M-01", "FR": "FR-01", "Actual": "", "Pass/Fail": ""})
        writer.writerow({"ID": "M-02", "FR": "FR-01", "Actual": "Worked", "Pass/Fail": "pass"})
        writer.writerow({"ID": "M-03", "FR": "FR-01", "Actual": "Odd", "Pass/Fail": "maybe"})
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
        assert f"## {tp.heading(fr)}" in text
    assert "App suite (`flutter test`): not run." in text
