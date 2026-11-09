"""docs/test_plan.md: the test table for every requirement in the report (Chapter 4), from real runs.

Two kinds of rows, one table format (ID | Module | Test case | Input | Expected
| Actual | Pass/Fail):

- T-xx, automated. Each row names the tests that check it: backend tests
  ("test_x.py::test_name", pytest) and app tests ("app:x_test.dart::<group>
  <description>", flutter test). This script runs BOTH whole suites once and
  fills Actual and Pass/Fail from those runs: a row passes only if every test
  it names passed, and a test that no longer exists fails the row rather than
  being skipped quietly.
- M-xx, manual: the app checklist on the phone, kept in
  docs/manual_app_checklist.csv. Do each check, then fill in its Actual,
  Pass/Fail and Date columns there; this script copies them across. A check
  not yet done stays "Not run yet" / "Pending", never "Pass".

Every row traces to one requirement, by the IDs the report uses in Chapter 4: FR-01 to FR-11
(functional), NFR-01 to NFR-07 (non-functional) and IR-05 (error handling). A row sits under
the requirement its evidence answers most directly: the access-control tests, for example,
under NFR-01 (security) rather than under the feature they exercise. Requirements no
automated test can answer (NFR-03 performance, NFR-04 usability) are filled by measurement
and by the manual checks.

    .\\.venv\\Scripts\\python.exe -m ml.make_test_plan            # both suites
    .\\.venv\\Scripts\\python.exe -m ml.make_test_plan --no-app   # without Flutter: app rows fail as not run
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from backend.config import REPO_ROOT

REPORT = REPO_ROOT / "docs" / "test_plan.md"
APP_DIR = REPO_ROOT / "app"
APP_PREFIX = "app:"  # app tests are named "app:<file>::<full test name>"
MANUAL_CHECKLIST = REPO_ROOT / "docs" / "manual_app_checklist.csv"
MANUAL_COLUMNS = ["ID", "FR", "Module", "Test case", "Input", "Expected", "Actual", "Pass/Fail", "Date"]

# The requirement IDs and names of the report, Chapter 4 (section 4.2), in report order.
FRS = {
    "FR-01": "Farmer registration and authentication",
    "FR-02": "Microclimate sensing and logging",
    "FR-03": "Blight risk computation",
    "FR-04": "Leaf image capture and submission",
    "FR-05": "CNN-based leaf classification",
    "FR-06": "Risk-weighted fusion",
    "FR-07": "SMS alerting",
    "FR-08": "Diagnosis history",
    "FR-09": "Symptom onset reporting",
    "FR-10": "Sensor node registration",
    "FR-11": "System status and error reporting",
    "NFR-01": "System security",
    "NFR-02": "Reliability and fault tolerance",
    "NFR-03": "Performance and response time",
    "NFR-04": "Usability",
    "NFR-05": "Low-connectivity tolerance",
    "NFR-06": "Maintainability and testability",
    "NFR-07": "Transparency",
    "IR-05": "Error handling",
}


def case(fr, module, test_case, given, expected, tests):
    return {"fr": fr, "module": module, "case": test_case, "input": given, "expected": expected, "tests": tests}


# Each test is "<file in backend/tests>::<test function>", or "app:<file in app/test>::<group> <description>".
AUTOMATED = [
    # --- FR-01: registration and authentication -------------------------------------
    case("FR-01", "backend/auth.py", "Register a farmer",
         "POST /auth/register: name, 07… phone, 8+ character password, farm ID",
         "201; role farmer; phone stored as +2547…; password stored only as a hash",
         ["test_auth.py::test_register_creates_a_farmer"]),
    case("FR-01", "backend/auth.py", "Refuse a duplicate or invalid registration",
         "Same phone twice; a non-Kenyan number; a 5-character password",
         "409 for the duplicate; 422 for the invalid number and the short password",
         ["test_auth.py::test_register_rejects_a_duplicate_phone_number",
          "test_auth.py::test_register_rejects_a_non_kenyan_phone_number",
          "test_auth.py::test_register_rejects_a_short_password",
          "test_auth.py::test_invalid_phone_numbers_are_rejected"]),
    case("FR-01", "backend/auth.py", "Log in with any accepted phone format",
         "POST /auth/login as 07…, 254…, +254… and with spaces/dashes",
         "200 with a JWT access token each time",
         ["test_auth.py::test_login_returns_a_token",
          "test_auth.py::test_login_accepts_any_accepted_phone_format",
          "test_auth.py::test_phone_numbers_are_normalised"]),
    case("FR-01", "backend/auth.py", "Wrong password and unknown number look the same",
         "Login with a wrong password; login with an unregistered number",
         "401 with the same message for both, so numbers cannot be probed",
         ["test_auth.py::test_login_with_a_wrong_password_fails",
          "test_auth.py::test_login_with_an_unknown_phone_number_gives_the_same_error"]),
    case("FR-01", "backend/auth.py", "Bad tokens are refused",
         "An expired token; one signed with another secret; one for a deleted account; none",
         "401 for each",
         ["test_auth.py::test_expired_token_is_rejected",
          "test_auth.py::test_token_signed_with_another_secret_is_rejected",
          "test_auth.py::test_token_for_a_deleted_account_is_rejected",
          "test_auth.py::test_me_without_a_token_is_rejected"]),
    case("FR-01", "backend/auth.py", "Passwords are salted hashes",
         "Hash the same password twice; verify right and wrong passwords",
         "Two different hashes; only the right password verifies",
         ["test_auth.py::test_same_password_hashes_differently", "test_auth.py::test_hash_and_verify"]),
    case("NFR-01", "backend/auth.py", "Farmers see only their own data",
         "Farmer A asks for farmer B's node; a farmer lists farmers; an admin reads any node",
         "404 (not 403) for B's node; 403 for the farmer list; 200 for the admin",
         ["test_auth.py::test_farmer_a_cannot_read_farmer_bs_node",
          "test_auth.py::test_farmer_cannot_list_farmers",
          "test_auth.py::test_admin_may_read_any_farmers_node",
          "test_auth.py::test_ownership_rule_refuses_records_it_has_no_rule_for"]),
    # --- FR-02 (and FR-10): sensing and logging ----------------------------------------------------
    case("FR-02", "backend/ingest.py", "Store a batch of readings",
         "POST /nodes/{id}/readings with X-Node-Key, up to 500 readings",
         "200 with the accepted count; rows stored; last_sync_time updated",
         ["test_ingest.py::test_batch_is_accepted", "test_ingest.py::test_a_full_size_batch_is_allowed",
          "test_ingest.py::test_sync_time_is_updated"]),
    case("NFR-02", "backend/ingest.py", "Re-sending a batch is harmless",
         "The same batch twice; an overlapping batch; a repeat inside one batch",
         "Repeats counted as duplicates, never stored twice, never an error",
         ["test_ingest.py::test_replayed_batch_adds_no_rows",
          "test_ingest.py::test_overlapping_batch_stores_only_the_new_readings",
          "test_ingest.py::test_duplicate_inside_one_batch_is_counted_once"]),
    case("FR-10", "backend/ingest.py", "Only the node's own key works",
         "No key; a wrong key; another node's key; a farmer's JWT",
         "401 for each; nothing stored",
         ["test_ingest.py::test_missing_node_key_is_rejected", "test_ingest.py::test_wrong_node_key_is_rejected",
          "test_ingest.py::test_a_node_key_does_not_work_for_another_node",
          "test_ingest.py::test_a_farmer_jwt_does_not_work_for_ingest"]),
    case("FR-02", "backend/ingest.py", "Bad readings are rejected one by one",
         "A batch with 60 °C, 150 % RH and a row with no temperature; a timestamp with no timezone; "
         "one in the future",
         "The bad rows are listed by position with a reason; the good rows are stored",
         ["test_ingest.py::test_invalid_rows_are_rejected_individually",
          "test_ingest.py::test_temperature_outside_the_allowed_range_is_rejected",
          "test_ingest.py::test_timestamp_without_a_timezone_is_rejected",
          "test_ingest.py::test_future_timestamp_is_rejected"]),
    case("FR-02", "backend/ingest.py", "Late and out-of-order readings from a node buffer",
         "Readings from days ago; readings newest first",
         "Accepted and stored in time order",
         ["test_ingest.py::test_old_buffered_readings_are_accepted",
          "test_ingest.py::test_out_of_order_timestamps_are_accepted",
          "test_ingest.py::test_farmer_can_read_their_nodes_readings_in_time_order"]),
    case("NFR-02", "tools/simulate_node.py", "Node buffers while offline and loses nothing",
         "Simulated outage; a response lost after the server stored the batch",
         "Readings kept until confirmed, then all delivered; resends counted as duplicates",
         ["test_simulate_node.py::test_offline_node_buffers_then_flushes_everything",
          "test_simulate_node.py::test_lost_response_is_resent_and_counted_as_duplicates",
          "test_simulate_node.py::test_only_confirmed_batches_leave_the_buffer"]),
    case("FR-02", "backend/app.py", "Device upload in the ESP32's format",
         "POST /readings with X-Device-Key: one reading, or a buffered list of {device_id, ts, temp_c, rh}",
         "Stored in the same table as node uploads; last seen updated; the reply carries the risk level",
         ["test_device_readings.py::test_a_single_reading_is_stored",
          "test_device_readings.py::test_a_buffered_batch_is_stored",
          "test_device_readings.py::test_last_seen_is_updated",
          "test_device_readings.py::test_readings_reach_the_same_table_as_the_node_endpoint",
          "test_device_readings.py::test_risk_level_comes_from_the_risk_layer"]),
    case("FR-10", "backend/app.py", "Only a device's own key works",
         "No key; a wrong key; an unknown device; another device's key",
         "401 each time, with the same answer for an unknown device as for a wrong key; nothing stored",
         ["test_device_readings.py::test_a_missing_key_is_refused",
          "test_device_readings.py::test_a_wrong_key_is_refused_and_nothing_is_stored",
          "test_device_readings.py::test_an_unknown_device_gets_the_same_answer_as_a_wrong_key",
          "test_device_readings.py::test_a_key_works_only_for_its_own_device"]),
    case("FR-02", "backend/app.py", "Device readings are checked one by one",
         "Out-of-range values; the limits themselves; a clock 6 minutes and a few seconds fast; unknown fields",
         "Bad rows rejected by position, the rest stored; limits accepted; a slightly fast clock tolerated",
         ["test_device_readings.py::test_out_of_range_values_are_rejected_and_the_rest_stored",
          "test_device_readings.py::test_the_limits_themselves_are_allowed",
          "test_device_readings.py::test_more_than_5_minutes_in_the_future_is_rejected",
          "test_device_readings.py::test_a_slightly_fast_clock_is_tolerated",
          "test_device_readings.py::test_unknown_fields_are_rejected_not_ignored"]),
    case("FR-10", "tools/register_device.py", "Register a device",
         "register_device node-01 ...; the same ID again; a malformed ID",
         "A 32-byte random key, printed once, stored only as a hash and accepted by POST /readings; "
         "duplicates and malformed IDs refused",
         ["test_register_device.py::test_a_device_is_registered_with_only_the_hash_of_its_key",
          "test_register_device.py::test_the_key_is_32_random_bytes",
          "test_register_device.py::test_the_issued_key_works_on_post_readings",
          "test_register_device.py::test_a_duplicate_device_id_is_refused_and_nothing_changes",
          "test_register_device.py::test_a_malformed_device_id_is_refused"]),
    # --- FR-03: risk computation -------------------------------------------------------
    case("FR-03", "backend/hutton.py", "Hutton day thresholds",
         "Days with tmin 10.0 / 9.99 °C and 6 / 5 hours at RH ≥ 90 %",
         "Hutton day only for tmin ≥ 10.0 AND ≥ 6 humid hours",
         ["test_hutton.py::test_tmin_exactly_10_counts", "test_hutton.py::test_tmin_9_99_does_not_count",
          "test_hutton.py::test_six_humid_hours_is_a_hutton_day",
          "test_hutton.py::test_five_humid_hours_is_not_a_hutton_day"]),
    case("FR-03", "backend/hutton.py", "Hutton period on the second day",
         "Two Hutton days running; Hutton day, unknown day, Hutton day",
         "Period flagged on the second day only; an unknown day breaks the period",
         ["test_hutton.py::test_two_consecutive_hutton_days_flag_a_period_on_the_second_day_only",
          "test_hutton.py::test_hutton_day_then_unknown_day_then_hutton_day_is_no_period"]),
    case("FR-03", "backend/hutton.py", "Missing data is unknown, never low",
         "Days with 19 and 20 valid hours; a gap in the hours",
         "19 h: insufficient_data, no score; 20 h: judged; gaps not interpolated; "
         "the database refuses a score on an unjudged day",
         ["test_hutton.py::test_19_valid_hours_is_unknown_with_no_score",
          "test_hutton.py::test_20_valid_hours_is_evaluated",
          "test_hutton.py::test_missing_hours_stay_nan_and_are_not_interpolated",
          "test_hutton.py::test_database_rejects_a_score_on_an_unjudged_day"]),
    case("FR-03", "backend/hutton.py", "Days are Nairobi days",
         "Humid hours either side of local midnight",
         "Split across the two local (UTC+3) days",
         ["test_hutton.py::test_local_day_boundaries_follow_nairobi_time",
          "test_hutton.py::test_humid_hours_spanning_midnight_split_into_two_local_days"]),
    case("FR-03", "ml/build_features.py, ml/train_risk.py", "The learned model cannot see the future",
         "Change or delete every hour after day D; check the split dates",
         "Day D's features do not move; no test date in train/validation; 2-day embargo",
         ["test_build_features.py::test_no_feature_for_day_d_uses_any_hour_after_d",
          "test_train_risk.py::test_no_test_date_appears_in_train_or_validation",
          "test_train_risk.py::test_two_embargo_days_before_each_boundary_belong_to_no_split",
          "test_build_features.py::test_unjudged_target_day_is_dropped_not_counted_as_negative"]),
    case("FR-03", "backend/risk_model.py", "Serving matches training",
         "Day D's features built from the database and from the training pipeline",
         "Identical features; no probability for a day without enough data",
         ["test_risk_model.py::test_serving_features_equal_training_features",
          "test_risk_model.py::test_a_day_without_enough_data_gets_no_probability"]),
    case("FR-03", "backend/risk_job.py", "Hourly job scores both sources",
         "risk_job.run_once, twice",
         "Hutton and learned 24/48 h rows for sensor and API, today and 2 days back; "
         "the second run changes nothing",
         ["test_risk_job.py::test_run_once_scores_both_sources_for_today_and_the_lookback",
          "test_risk_job.py::test_run_once_twice_gives_the_same_table"]),
    case("FR-03", "backend/dashboard.py", "Dashboard says unknown, never low, without data",
         "GET /nodes/{id}/dashboard with no readings",
         "Risk 'unknown' and a sentence that says so",
         ["test_risk_job.py::test_dashboard_with_no_data_says_the_risk_is_unknown",
          "test_risk_job.py::test_unknown_risk_is_never_called_low"]),
    case("FR-03", "backend/risk_job.py", "Two scoring runs at once",
         "Two re-scores of one node at the same moment; a duplicate score row",
         "One set of scores and one SMS; the database refuses the duplicate",
         ["test_concurrent_rescore.py::test_two_simultaneous_runs_send_one_sms_and_write_one_set_of_scores",
          "test_concurrent_rescore.py::test_the_database_refuses_a_duplicate_score"]),
    case("FR-03", "backend/app.py", "Admin re-score now",
         "POST /admin/nodes/{id}/rescore as admin, as a farmer, for an unknown node",
         "Scores now and runs the SMS policy; 403 for a farmer; 404 for an unknown node",
         ["test_seed_demo_event.py::test_rescore_scores_the_node_now_and_fires_the_alert",
          "test_seed_demo_event.py::test_rescore_is_admin_only",
          "test_seed_demo_event.py::test_rescore_of_an_unknown_node_is_404"]),
    case("NFR-02", "backend/risk_job.py", "One failing sensor does not stop the others",
         "A run where the first node's scoring raises an error",
         "The other nodes are scored; the failure is recorded in plain words for the admin",
         ["test_status.py::test_one_failing_sensor_does_not_stop_the_others"]),
    # --- FR-04: leaf image capture and submission ----------------------------------------
    case("FR-04", "backend/cnn_service.py", "Upload a leaf photo",
         "POST /images with a farmer token and a JPEG (optional capture_date)",
         "201; file under data/uploads/<farmer>/; leaf_images row; prediction returned",
         ["test_cnn_service.py::test_upload_stores_the_image_and_returns_the_prediction",
          "test_cnn_service.py::test_capture_date_is_kept_when_given"]),
    case("FR-04", "backend/cnn_service.py", "Unusable photos are refused with a reason",
         "A 223×400 image; a black image; a blurry image; a non-image file; a finger over the lens",
         "422 with reason too_small / too_dark / too_blurry / unreadable / not_a_leaf; nothing stored",
         ["test_cnn_service.py::test_a_finger_over_the_lens_is_not_a_leaf",
          "test_cnn_service.py::test_a_mostly_brown_blighted_leaf_is_still_a_leaf",
          "test_cnn_service.py::test_tiny_image_is_rejected",
          "test_cnn_service.py::test_black_image_is_rejected_as_too_dark",
          "test_cnn_service.py::test_blurry_image_is_rejected",
          "test_cnn_service.py::test_unreadable_files_are_rejected",
          "test_cnn_service.py::test_a_bad_photo_gets_a_reason_and_nothing_is_stored"]),
    case("FR-04", "backend/cnn_service.py", "Good photos are not refused",
         "All PlantVillage images; a sharp, well-lit photo",
         "None rejected by the quality check",
         ["test_cnn_service.py::test_plantvillage_images_pass_the_quality_check",
          "test_cnn_service.py::test_a_sharp_well_lit_photo_passes"]),
    case("NFR-01", "backend/cnn_service.py", "Uploads are private",
         "No token; an oversized file; another farmer's image ID",
         "401; 413; 404 (not 403)",
         ["test_cnn_service.py::test_upload_needs_a_farmer_token",
          "test_cnn_service.py::test_oversized_upload_is_refused",
          "test_cnn_service.py::test_another_farmers_image_is_404_not_403"]),
    case("FR-04", "backend/scan.py, app: scan_screen.dart", "Check one leaf from 1-3 photos",
         "POST /scans with 3 photos of one leaf; with none; with 4. The app's Scan screen",
         "3 photos: one diagnosis linked to the first photo, the others pointing to it; none / 4 refused; "
         "the app asks for up to three guided photos and sends them in one request",
         ["test_scan.py::test_three_photos_make_one_diagnosis",
          "test_scan.py::test_a_scan_with_no_photo_is_refused",
          "test_scan.py::test_more_than_three_photos_are_refused",
          "app:screens_test.dart::Leaf check with several photos the scan screen asks for three photos and "
          "waits for at least one",
          "app:screens_test.dart::Leaf check with several photos the photos go to /scans in one request, in order"]),
    case("FR-04", "backend/scan.py, app: scan_screen.dart", "A bad photo in a scan is named",
         "3 photos, one too dark; 3 photos, two unusable",
         "Nothing stored; every photo to retake listed with its reason; the app marks those slots",
         ["test_scan.py::test_one_bad_photo_names_it_and_nothing_is_stored",
          "test_scan.py::test_every_bad_photo_is_listed_at_once",
          "app:screens_test.dart::Leaf check with several photos the photos to retake are known by position",
          "app:screens_test.dart::Errors become plain sentences a rejected photo keeps the reason, so the app can "
          "offer Retake"]),
    # --- FR-05: CNN classification ----------------------------------------------------------
    case("FR-05", "backend/cnn_service.py", "Preprocessing matches training",
         "Images of any mode, size and EXIF rotation",
         "RGB, rotation applied, float32 [1, 224, 224, 3] scaled as MobileNetV2 (x / 127.5 − 1)",
         ["test_cnn_service.py::test_preprocess_scales_like_mobilenet_v2_preprocess_input",
          "test_cnn_service.py::test_preprocess_gives_the_model_input_shape_and_dtype",
          "test_cnn_service.py::test_preprocess_converts_any_mode_to_rgb",
          "test_cnn_service.py::test_phone_rotation_in_exif_is_applied"]),
    case("FR-05", "backend/cnn_service.py", "Phone photos are cropped, not squashed",
         "A 400×200 and a 200×400 photo; a square image",
         "The centre square is classified; a square image is unchanged",
         ["test_cnn_service.py::test_non_square_photo_is_centre_cropped_not_squashed",
          "test_cnn_service.py::test_tall_photo_keeps_the_middle",
          "test_cnn_service.py::test_a_square_image_is_not_changed_by_the_crop"]),
    case("FR-05", "backend/cnn_service.py", "Result has a label, confidence and all probabilities",
         "A preprocessed leaf",
         "Label early_blight / healthy / late_blight with its confidence and the 3 probabilities",
         ["test_cnn_service.py::test_predict_returns_label_confidence_and_all_probabilities",
          "test_cnn_service.py::test_labels_and_model_outputs_must_agree"]),
    case("FR-05", "backend/cnn_service.py", "No model, clear answer",
         "Start the API without models/cnn.tflite; upload a photo",
         "API still runs; POST /images answers 503",
         ["test_cnn_service.py::test_upload_without_a_loaded_model_is_503",
          "test_cnn_service.py::test_missing_model_files_leave_the_classifier_unloaded"]),
    case("FR-05", "backend/scan.py", "Several photos of one leaf are averaged",
         "1 photo; 3 photos (late blight, healthy, late blight); 2 healthy-looking and 1 late-blight-looking",
         "1 photo unchanged; class probabilities averaged; one odd photo cannot decide alone",
         ["test_scan.py::test_one_photo_is_unchanged",
          "test_scan.py::test_the_photos_are_averaged",
          "test_scan.py::test_one_odd_photo_cannot_decide_alone",
          "app:screens_test.dart::Leaf check with several photos a scan reply carries each photo and whether "
          "they agreed"]),
    # --- FR-06: fusion --------------------------------------------------------------------
    case("FR-06", "backend/fusion.py", "Risk moves the late-blight threshold",
         "r = 0.1, 0.5, 0.9; r far outside 0–1",
         "tau = 0.76, 0.60, 0.44 (lower when risk is higher); clipped to [0.35, 0.85]",
         ["test_fusion.py::test_threshold_values_at_the_ends_of_the_risk_scale",
          "test_fusion.py::test_neutral_risk_gives_the_base_threshold",
          "test_fusion.py::test_tau_is_clamped_at_both_ends"]),
    case("FR-06", "backend/fusion.py", "Same leaf, different weather",
         "One leaf with p_late 0.55, at r = 0.9 and at r = 0.1",
         "Flagged at r = 0.9 (tau 0.44), not at r = 0.1 (tau 0.76); the CNN's label is unchanged; "
         "only late blight is gated",
         ["test_fusion.py::test_same_leaf_is_flagged_after_blight_weather_and_not_after_dry_weather",
          "test_fusion.py::test_only_late_blight_is_gated"]),
    case("FR-06", "backend/fusion.py", "Which risk is used",
         "Learned 24 h row; only Hutton rows; an old Hutton day; no node",
         "learned_24h, then hutton_today, then hutton_fallback (≤ 3 days), then neutral 0.5, recorded; "
         "API rows never used",
         ["test_fusion.py::test_learned_24h_forecast_covering_the_day_is_preferred",
          "test_fusion.py::test_hutton_fallback_when_no_learned_score_exists",
          "test_fusion.py::test_falls_back_to_the_most_recent_judged_hutton_day",
          "test_fusion.py::test_no_node_means_the_neutral_prior_and_says_why",
          "test_fusion.py::test_api_rows_never_stand_in_for_the_plot"]),
    case("FR-06", "backend/fusion.py", "Each diagnosis records what fed it",
         "POST /images for a node with readings",
         "diagnosis_results row (r, basis, tau, flag) linked to the 48 h of readings before the photo",
         ["test_fusion.py::test_diagnose_writes_the_result_and_links_48h_of_readings",
          "test_cnn_service.py::test_upload_returns_and_stores_the_fused_diagnosis"]),
    case("NFR-04", "backend/fusion.py", "Wording is decision support",
         "Summaries for every combination of label, flag and risk",
         "Suggests checking; never instructs to spray",
         ["test_fusion.py::test_summary_is_decision_support_never_an_instruction"]),
    case("FR-06", "backend/scan.py", "Disagreeing photos are said in the result",
         "A scan whose photos show different classes",
         "The summary says how many photos showed what, and that the result is less certain",
         ["test_scan.py::test_disagreement_is_said_in_words",
          "test_scan.py::test_photos_that_disagree_are_averaged_and_flagged_in_the_summary"]),
    case("NFR-07", "app: diagnosis_screen.dart", "The app explains the fusion",
         "Diagnoses at high risk, at low risk, and with no weather data",
         "How the risk lowered or raised the bar, in words; no data shown as unknown risk",
         ["app:screens_test.dart::Diagnosis explains how high risk lowered the bar",
          "app:screens_test.dart::Diagnosis explains how low risk raised the bar and the leaf was not flagged",
          "app:screens_test.dart::Diagnosis no weather data is shown as unknown risk, and says so"]),
    # --- FR-07: SMS ------------------------------------------------------------------------
    case("FR-07", "backend/alerts.py", "High-risk SMS when the risk turns high",
         "Judged level medium → high; high → high",
         "One SMS and an alerts row on the change; nothing while it stays high",
         ["test_alerts.py::test_medium_to_high_sends_and_records_the_alert",
          "test_alerts.py::test_high_risk_only_on_a_change_to_high",
          "test_alerts.py::test_high_to_high_sends_nothing",
          "test_alerts.py::test_the_risk_job_runs_the_high_risk_policy"]),
    case("FR-07", "backend/alerts.py", "At most one high-risk SMS a day",
         "high → medium → high within 24 h",
         "The second high is not sent",
         ["test_alerts.py::test_cooldown_blocks_a_second_high_risk_within_24h",
          "test_alerts.py::test_flicker_back_to_high_within_24h_is_blocked_by_the_cooldown"]),
    case("FR-07", "backend/alerts.py", "Flagged diagnosis sends an SMS",
         "A flagged and an unflagged diagnosis",
         "One SMS for the flagged one; none for the other",
         ["test_alerts.py::test_flagged_diagnosis_sends_one_sms",
          "test_alerts.py::test_unflagged_diagnosis_sends_nothing"]),
    case("FR-07", "backend/alerts.py", "Unknown risk is never reported as low",
         "Status SMS request when the risk is low, high and unknown",
         "Sent only when judged low; otherwise 409 with the reason; unknown changes no alert state",
         ["test_alerts.py::test_status_sms_when_risk_is_low",
          "test_alerts.py::test_status_sms_refused_unless_risk_is_judged_low",
          "test_alerts.py::test_unknown_risk_changes_nothing"]),
    case("FR-07", "backend/alerts.py", "Messages fit one SMS and advise, not instruct",
         "Every template with the longest plot name",
         "≤ 160 GSM-7 characters; decision-support wording",
         ["test_alerts.py::test_templates_fit_one_gsm_sms", "test_alerts.py::test_templates_are_decision_support"]),
    case("FR-07", "backend/sms.py", "Failed sends are recorded and retried",
         "Network failure; SDK TLS failure; a rejected number",
         "Recorded, not raised; TLS falls back to the identical direct request; retried on the next run",
         ["test_alerts.py::test_a_network_failure_is_recorded_not_raised",
          "test_alerts.py::test_sdk_tls_failure_falls_back_to_the_identical_direct_request",
          "test_alerts.py::test_a_failed_send_is_retried_next_run_and_starts_no_cooldown",
          "test_alerts.py::test_a_rejected_number_is_recorded_as_rejected"]),
    case("FR-07", "backend/alerts.py", "Delivery reports update the alert",
         "POST /sms/delivery-report (id, status, failureReason)",
         "delivery_status updated; token checked when configured",
         ["test_alerts.py::test_delivery_report_updates_the_status",
          "test_alerts.py::test_delivery_report_token_is_checked_when_configured"]),
    case("FR-07", "backend/alerts.py", "Undelivered SMS are recognised and recorded",
         "Each delivery status; a send refused by Africa's Talking; a delivery report 'Failed'",
         "error / Failed / Rejected / InvalidPhoneNumber count as not delivered; each recorded for the admin",
         ["test_status.py::test_which_delivery_statuses_mean_the_farmer_did_not_get_it",
          "test_status.py::test_an_sms_that_could_not_be_sent_is_recorded",
          "test_status.py::test_an_sms_reported_undelivered_is_recorded"]),
    # --- FR-08 (and FR-09): history -------------------------------------------------------------------
    case("FR-08", "backend/dashboard.py", "History newest first, gaps shown",
         "GET /nodes/{id}/history?days=10 with days that have no readings",
         "Every day listed newest first; the missing day shown as a gap",
         ["test_risk_job.py::test_history_is_newest_first_with_gaps_shown",
          "test_risk_job.py::test_history_days_must_be_sensible"]),
    case("FR-08", "backend/dashboard.py", "History lists diagnoses and alerts",
         "A node with two diagnoses and two alerts on different days",
         "Both lists newest first",
         ["test_risk_job.py::test_history_lists_diagnoses_and_alerts_newest_first"]),
    case("FR-09", "backend/app.py", "Symptom onset reports",
         "POST /symptom-reports with today's and tomorrow's date; list them",
         "Today accepted, tomorrow refused; own reports only, newest onset first",
         ["test_risk_job.py::test_farmer_reports_symptom_onset",
          "test_risk_job.py::test_onset_today_is_allowed_but_not_tomorrow",
          "test_risk_job.py::test_list_shows_only_own_reports_newest_onset_first"]),
    case("NFR-01", "backend/dashboard.py", "History is private",
         "Farmer A asks for farmer B's history",
         "404",
         ["test_risk_job.py::test_history_of_another_farmers_node_is_404"]),
    case("FR-08", "backend/dashboard.py, app: history_screen.dart", "The app's history reads plainly",
         "History with a multi-photo check, gaps and SMS",
         "Newest first, each day led by its weather risk, gaps shown; a check says how many photos it used",
         ["test_scan.py::test_history_says_how_many_photos_a_check_used",
          "app:screens_test.dart::History newest first, each day led by its weather risk",
          "app:screens_test.dart::History gaps are shown, not hidden",
          "app:screens_test.dart::History photos and SMS read plainly"]),
    case("FR-08", "app: trend_chart.dart", "Trend: the model's forecasts against what happened",
         "30 days of measured Hutton scores and 24 h forecasts, with gaps",
         "Each forecast on the day it was about; tomorrow last; the server's bands; gaps kept; sentences on "
         "direction, blight days, tomorrow and how often the forecast matched",
         ["test_risk_job.py::test_history_is_newest_first_with_gaps_shown",
          "app:screens_test.dart::Trend each forecast sits on the day it was about, and tomorrow ends the chart",
          "app:screens_test.dart::Trend bands come from the server",
          "app:screens_test.dart::Trend summary: direction, blight days, tomorrow, and how the forecasts did",
          "app:screens_test.dart::Trend gaps stay gaps, and too little data says so instead of guessing",
          "app:screens_test.dart::Trend the card draws the chart, the key and the sentences"]),
    # --- FR-11, IR-05 and the NFRs: status, errors, security, connectivity, updates --------------------
    case("IR-05", "backend/app.py", "Every error has one shape",
         "A 404, a validation error, a missing login, an endpoint's own reason; an error the app has no wording for",
         "FastAPI's detail kept, plus error {code, message}; the app shows the server's sentence",
         ["test_status.py::test_every_error_has_a_code_and_a_plain_message",
          "test_status.py::test_an_endpoints_own_reason_becomes_the_code",
          "app:screens_test.dart::Readable errors and status any other error uses the server's plain sentence"]),
    case("FR-11", "backend/events.py", "Problems are recorded for the admin",
         "An unexpected error; the same problem every hour; the log itself failing; old routine rows",
         "A plain 500 with the problem recorded; one record per quiet period; the caller never breaks; "
         "routine rows pruned, problems kept",
         ["test_status.py::test_an_unexpected_error_is_recorded_and_answered_plainly",
          "test_status.py::test_an_event_is_stored_in_plain_words",
          "test_status.py::test_a_problem_that_persists_is_reported_once_per_quiet_period",
          "test_status.py::test_recording_never_breaks_the_caller",
          "test_status.py::test_old_routine_rows_are_pruned_but_problems_kept"]),
    case("FR-11", "backend/status.py, app: status_screen.dart", "Admin status page",
         "GET /admin/status as admin, as a farmer, without login; after a failed run; with the model missing",
         "Each part ok / warning / error with a sentence, sensors, recent problems; 403 / 401 otherwise; "
         "reading it writes nothing; the app reads it",
         ["test_status.py::test_status_is_for_admins_only",
          "test_status.py::test_status_names_each_part_and_the_recent_problems",
          "test_status.py::test_a_failed_run_and_a_missing_leaf_checker_are_errors",
          "test_status.py::test_reading_the_status_never_writes_anything",
          "test_status.py::test_each_run_leaves_a_record_for_the_status_page",
          "app:screens_test.dart::Readable errors and status the system status is read part by part",
          "app:screens_test.dart::Readable errors and status an admin is recognised, and an older server means "
          "a farmer"]),
    case("FR-11", "backend/status.py, app: dashboard_screen.dart", "Farmer warnings",
         "A sensor never seen / quiet for 5 h; an undelivered SMS; the leaf checker down; all well",
         "Only what affects this farmer, with what to check; nothing when all is well",
         ["test_status.py::test_no_warnings_when_all_is_well",
          "test_status.py::test_a_quiet_sensor_says_for_how_long_and_what_to_check",
          "test_status.py::test_an_undelivered_sms_and_a_missing_leaf_checker_are_shown",
          "test_status.py::test_the_dashboard_carries_the_warnings",
          "app:screens_test.dart::Readable errors and status dashboard warnings are read, and a server without "
          "them means none",
          "app:screens_test.dart::Readable errors and status a warning reads plainly, with an icon"]),
    case("NFR-05", "app: api.dart", "The app never crashes on a bad connection",
         "No network; a broken (non-JSON) reply; the model missing; a file too large",
         "A plain sentence each time; no exception escapes",
         ["app:screens_test.dart::Errors become plain sentences no network never escapes as a crash",
          "app:screens_test.dart::Errors become plain sentences a broken reply (not JSON) is still a friendly message",
          "app:screens_test.dart::Errors become plain sentences model missing and file too large"]),
    case("NFR-01", "app: api.dart", "The app talks HTTPS",
         "The app's default server address",
         "https://blight-ews-api.onrender.com",
         ["app:screens_test.dart::by default the app talks to the hosted server over HTTPS"]),
    case("NFR-06", "app: update.dart, tools/set_app_signing_secrets.py", "App updates without the Play Store",
         "version.json with a newer, the same and an older build; no answer; a PC build; the signing key",
         "Only a newer build is offered; a failed check shows nothing; PC builds never ask; the key reaches "
         "the release workflow intact",
         ["app:screens_test.dart::App updates a newer build on GitHub is offered",
          "app:screens_test.dart::App updates the same or an older build is not",
          "app:screens_test.dart::App updates no answer, or a broken one, just means no banner",
          "app:screens_test.dart::App updates a build made on the PC never asks",
          "app:screens_test.dart::App updates the strip names the build and can be closed",
          "test_set_app_signing_secrets.py::test_the_three_secrets_come_from_key_properties_and_the_keystore",
          "test_set_app_signing_secrets.py::test_a_key_the_workflow_cannot_use_is_refused"]),
    case("NFR-02", "backend/app.py", "Health check",
         "GET /health with the database up and down",
         "200 ok; 503 when the database is unreachable",
         ["test_app.py::test_health_ok_when_database_reachable",
          "test_app.py::test_health_503_when_database_unreachable"]),
]


# --- running pytest ---------------------------------------------------------------


def run_pytest(junit_path: Path) -> int:
    """Run the whole suite once, writing JUnit XML. Returns pytest's exit code."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit_path}"]
    print("Running the test suite: " + " ".join(command[1:]))
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def run_flutter() -> list[str] | None:
    """Run the whole app suite once (flutter test --machine). Its JSON lines, or None without Flutter."""
    flutter = shutil.which("flutter")
    if flutter is None:
        print("Flutter not found: app tests not run")
        return None
    print("Running the app tests: flutter test --machine")
    result = subprocess.run([flutter, "test", "--machine"], cwd=APP_DIR, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.splitlines()


def read_flutter_machine(lines: list[str]) -> tuple[dict, dict]:
    """({"app:<file>::<full name>": (outcome, message)}, summary) from `flutter test --machine` output.

    A test's full name is its groups' names and its own, joined by spaces, as Flutter reports it.
    Hidden entries (the "loading <file>" steps) are not tests and are left out.
    """
    suites, tests, errors, results = {}, {}, {}, {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # Flutter's own progress lines
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "suite":
            suites[event["suite"]["id"]] = Path(event["suite"]["path"]).name
        elif kind == "testStart":
            test = event["test"]
            tests[test["id"]] = f"{APP_PREFIX}{suites.get(test['suiteID'], '?')}::{test['name']}"
        elif kind == "error":
            errors.setdefault(event["testID"], str(event.get("error", "")).splitlines()[0][:150])
        elif kind == "testDone" and not event.get("hidden"):
            key = tests.get(event["testID"])
            if key is None:
                continue
            if event.get("skipped"):
                results[key] = ("skipped", "")
            elif event.get("result") == "success":
                results[key] = ("passed", "")
            else:
                results[key] = ("failed", errors.get(event["testID"], event.get("result", "")))
    outcomes = [o for o, _ in results.values()]
    summary = {"total": len(outcomes), "passed": outcomes.count("passed"), "failed": outcomes.count("failed"),
               "skipped": outcomes.count("skipped")}
    return results, summary


def read_junit(path: Path) -> dict:
    """{"test_auth.py::test_name": ("passed" | "failed" | "skipped", message)}.

    Parametrised tests ("test_x[a]") are folded into one entry: failed if any
    case failed, skipped only if every case was skipped.
    """
    results: dict[str, tuple[str, str]] = {}
    for testcase in ET.parse(path).getroot().iter("testcase"):
        file = testcase.get("classname", "").split(".")[-1] + ".py"
        name = testcase.get("name", "").split("[")[0]
        key = f"{file}::{name}"
        outcome, message = "passed", ""
        for child in testcase:
            if child.tag in ("failure", "error"):
                text = child.get("message") or child.tag
                outcome, message = "failed", text.splitlines()[0][:150]
            elif child.tag == "skipped" and outcome == "passed":
                outcome, message = "skipped", child.get("message") or ""
        previous = results.get(key)
        if previous is None or previous[0] == "skipped" or outcome == "failed":
            results[key] = (outcome, message)
    return results


def suite_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    total = int(suite.get("tests", 0))
    failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    return {"total": total, "failed": failed, "skipped": skipped, "passed": total - failed - skipped,
            "seconds": float(suite.get("time", 0))}


# --- filling in the table ----------------------------------------------------------


def outcome_of(row: dict, results: dict) -> tuple[str, str]:
    """(Actual, Pass/Fail) for an automated row, from the pytest results."""
    outcomes = [results.get(test, ("missing", "test not found in this run")) for test in row["tests"]]
    n = len(outcomes)
    bad = [(test, o) for test, o in zip(row["tests"], outcomes, strict=True) if o[0] != "passed"]
    if not bad:
        return f"As expected: {n}/{n} tests passed", "Pass"
    test, (outcome, message) = bad[0]
    detail = f"{test.split('::')[1]} {outcome}" + (f": {message}" if message else "")
    return f"{n - len(bad)}/{n} tests passed; {detail}", "Fail"


def read_manual(path: Path = MANUAL_CHECKLIST) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["Actual"] = (row.get("Actual") or "").strip() or "Not run yet"
        result = (row.get("Pass/Fail") or "").strip().capitalize()
        row["Pass/Fail"] = result if result in ("Pass", "Fail") else "Pending"
    return rows


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def heading(fr: str) -> str:
    return f"{fr}: {FRS[fr]}"


def render(automated: list[dict], manual: list[dict], summary: dict, ran_at: datetime,
           app_summary: dict | None = None) -> str:
    app_line = ("- App suite (`flutter test`): not run." if app_summary is None else
                f"- App suite (`flutter test`): **{app_summary['passed']} passed, {app_summary['failed']} failed, "
                f"{app_summary['skipped']} skipped** of {app_summary['total']} tests.")
    lines = [
        "# Test plan (FR-01 to FR-11, NFR-01 to NFR-07, IR-05)",
        "",
        "Generated by `ml/make_test_plan.py`. Automated rows (T-) are filled in from a real run of the whole "
        "backend suite (pytest) and the whole app suite (flutter test); a row passes only if every test it names "
        "passed. Manual rows (M-) come from `docs/manual_app_checklist.csv`, filled in by hand after each check "
        "on the phone or the node; a check not yet done is Pending, never Pass.",
        "",
        f"- Backend suite (pytest), run {ran_at:%Y-%m-%d %H:%M UTC}: **{summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['skipped']} skipped** of {summary['total']} tests, "
        f"{summary['seconds']:.0f} s.",
        app_line,
        "",
        "## Summary by requirement",
        "",
        "| ID | Requirement | Automated rows (pass) | Tests named | Manual rows (pass / fail / pending) |",
        "|---|---|---|---|---|",
    ]
    for fr, name in FRS.items():
        auto = [r for r in automated if r["fr"] == fr]
        man = [r for r in manual if r["FR"] == fr]
        lines.append(
            f"| {fr} | {name} | {len(auto)} ({sum(r['result'] == 'Pass' for r in auto)}) | "
            f"{sum(len(r['tests']) for r in auto)} | {len(man)} ({sum(r['Pass/Fail'] == 'Pass' for r in man)} / "
            f"{sum(r['Pass/Fail'] == 'Fail' for r in man)} / {sum(r['Pass/Fail'] == 'Pending' for r in man)}) |")
    header = ["| ID | Module | Test case | Input | Expected | Actual | Pass/Fail |", "|---|---|---|---|---|---|---|"]
    for fr in FRS:
        lines += ["", f"## {heading(fr)}", "", *header]
        for r in automated:
            if r["fr"] == fr:
                lines.append(f"| {r['id']} | {_cell(r['module'])} | {_cell(r['case'])} | {_cell(r['input'])} | "
                             f"{_cell(r['expected'])} | {_cell(r['actual'])} | **{r['result']}** |")
        for r in manual:
            if r["FR"] == fr:
                lines.append(f"| {r['ID']} | {_cell(r['Module'])} | {_cell(r['Test case'])} | {_cell(r['Input'])} | "
                             f"{_cell(r['Expected'])} | {_cell(r['Actual'])} | **{r['Pass/Fail']}** |")
    others = [r for r in manual if r["FR"] not in FRS]
    if others:
        lines += ["", "## Other checks", "", *header]
        lines += [f"| {r['ID']} | {_cell(r['Module'])} | {_cell(r['Test case'])} | {_cell(r['Input'])} | "
                  f"{_cell(r['Expected'])} | {_cell(r['Actual'])} | **{r['Pass/Fail']}** |" for r in others]
    lines += [
        "",
        "## Tests behind each automated row",
        "",
        *[f"- **{r['id']}**: " + ", ".join(f"`{t}`" for t in r["tests"]) for r in automated],
        "",
    ]
    return "\n".join(lines)


def build(results: dict) -> list[dict]:
    rows = []
    order = list(FRS)
    # Numbered in report order (stable: rows keep their order within a requirement).
    for number, row in enumerate(sorted(AUTOMATED, key=lambda r: order.index(r["fr"])), start=1):
        actual, result = outcome_of(row, results)
        rows.append({**row, "id": f"T-{number:02d}", "actual": actual, "result": result})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run pytest and write docs/test_plan.md.")
    parser.add_argument("--junit", type=Path, help="use this JUnit XML instead of running pytest")
    parser.add_argument("--no-app", action="store_true", help="do not run the app tests (their rows fail)")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        junit = args.junit
        if junit is None:
            junit = Path(tmp) / "pytest.xml"
            run_pytest(junit)
        if not junit.exists():
            print("pytest wrote no results; is the test database running?")
            return 1
        results, summary = read_junit(junit), suite_summary(junit)

    app_summary = None
    lines = None if args.no_app else run_flutter()
    if lines is not None:
        app_results, app_summary = read_flutter_machine(lines)
        results = {**results, **app_results}

    automated = build(results)
    manual = read_manual()
    REPORT.write_text(render(automated, manual, summary, datetime.now(UTC), app_summary), encoding="utf-8")

    failed = [r["id"] for r in automated if r["result"] != "Pass"]
    print(f"\nBackend suite: {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped")
    if app_summary is not None:
        print(f"App suite: {app_summary['passed']} passed, {app_summary['failed']} failed, "
              f"{app_summary['skipped']} skipped")
    print(f"Automated rows: {len(automated) - len(failed)}/{len(automated)} pass"
          + (f" (failing: {', '.join(failed)})" if failed else ""))
    print(f"Manual rows: {sum(r['Pass/Fail'] == 'Pass' for r in manual)} pass, "
          f"{sum(r['Pass/Fail'] == 'Fail' for r in manual)} fail, "
          f"{sum(r['Pass/Fail'] == 'Pending' for r in manual)} pending (fill in {MANUAL_CHECKLIST.name})")
    print(f"Wrote {REPORT.relative_to(REPO_ROOT)}")
    app_failed = app_summary is not None and app_summary["failed"] > 0
    return 0 if not failed and summary["failed"] == 0 and not app_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
