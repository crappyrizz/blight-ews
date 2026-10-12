"""Tests for ml/eval_cnn.py: metrics, the comparison table, and image loading.

No TFLite model is needed: `predict` is a fake that returns fixed answers.
"""
import numpy as np
import pytest
from PIL import Image

from ml import eval_cnn

CLASSES = ["early_blight", "healthy", "late_blight"]


def make_image(path, colour=(200, 30, 30), size=(256, 256)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def fake_predictor(answers):
    """Predict by file name: answers maps a path to the class it should output."""
    def predict_path(path):
        probabilities = np.zeros(len(CLASSES))
        probabilities[CLASSES.index(answers[path])] = 1.0
        return probabilities
    return predict_path


def evaluate_with(items, answers):
    """Run evaluate() with a predictor keyed by path (preprocess is bypassed)."""
    original = eval_cnn.preprocess
    eval_cnn.preprocess = lambda path: path  # hand the path straight to the fake
    try:
        return eval_cnn.evaluate(items, fake_predictor(answers), CLASSES)
    finally:
        eval_cnn.preprocess = original


# --- preprocessing -----------------------------------------------------


def test_preprocess_matches_mobilenet_v2_input(tmp_path):
    black = eval_cnn.preprocess(make_image(tmp_path / "black.jpg", (0, 0, 0)))
    white = eval_cnn.preprocess(make_image(tmp_path / "white.png", (255, 255, 255)))

    assert black.shape == (1, 224, 224, 3)
    assert black.dtype == np.float32
    assert black.max() == pytest.approx(-1.0)  # x / 127.5 - 1
    assert white.min() == pytest.approx(1.0)


def test_preprocess_converts_greyscale_and_resizes(tmp_path):
    path = tmp_path / "grey.png"
    Image.new("L", (300, 200), 128).save(path)
    assert eval_cnn.preprocess(path).shape == (1, 224, 224, 3)


# --- metrics -----------------------------------------------------------


def test_metrics_on_a_hand_made_set():
    # 4 early (3 right, 1 called late), 2 healthy (both right), 4 late (all right).
    items = [(f"e{i}", "early_blight") for i in range(4)] + \
            [(f"h{i}", "healthy") for i in range(2)] + \
            [(f"l{i}", "late_blight") for i in range(4)]
    answers = {path: label for path, label in items}
    answers["e3"] = "late_blight"

    result = evaluate_with(items, answers)

    assert result["n"] == 10
    assert result["accuracy"] == pytest.approx(0.9)
    early = result["per_class"]["early_blight"]
    late = result["per_class"]["late_blight"]
    assert (early["precision"], early["recall"]) == (pytest.approx(1.0), pytest.approx(0.75))
    assert (late["precision"], late["recall"]) == (pytest.approx(0.8), pytest.approx(1.0))
    assert late["f1"] == pytest.approx(2 * 0.8 / 1.8)
    assert result["confusion_matrix"] == [[3, 0, 1], [0, 2, 0], [0, 0, 4]]
    expected_macro = np.mean([2 * 0.75 / 1.75, 1.0, 2 * 0.8 / 1.8])
    assert result["macro_f1"] == pytest.approx(expected_macro)


def test_a_class_with_no_images_is_not_measurable_not_zero():
    items = [("e0", "early_blight"), ("l0", "late_blight")]
    result = evaluate_with(items, {"e0": "early_blight", "l0": "late_blight"})

    healthy = result["per_class"]["healthy"]
    assert healthy["support"] == 0
    assert healthy["recall"] is None
    assert healthy["precision"] is None
    assert result["macro_f1"] == pytest.approx(1.0)  # averaged over classes that are present


def test_a_class_never_predicted_counts_as_zero_f1():
    items = [("h0", "healthy"), ("e0", "early_blight")]
    result = evaluate_with(items, {"h0": "early_blight", "e0": "early_blight"})

    healthy = result["per_class"]["healthy"]
    assert healthy["recall"] == 0.0
    assert healthy["precision"] is None  # never predicted
    assert result["macro_f1"] == pytest.approx(np.mean([2 * 0.5 / 1.5, 0.0]))


def test_an_empty_set_is_reported_as_empty():
    assert eval_cnn.evaluate([], lambda image: None, CLASSES) == {"n": 0}


# --- the comparison table ----------------------------------------------


def test_table_shows_the_gap_as_field_minus_lab():
    lab = evaluate_with([("a", "early_blight"), ("b", "late_blight")],
                        {"a": "early_blight", "b": "late_blight"})
    field = evaluate_with([("c", "early_blight"), ("d", "late_blight")],
                          {"c": "early_blight", "d": "early_blight"})

    rows = {metric: (a, b, gap) for metric, a, b, gap in eval_cnn.comparison_rows(lab, field, CLASSES)}

    assert rows["Accuracy"] == ("1.000", "0.500", "-0.500")
    assert rows["late_blight recall"] == ("1.000", "0.000", "-1.000")
    assert rows["healthy recall"] == ("n/a", "n/a", "n/a")
    assert rows["Images"] == ("2", "2", "")


def test_table_with_no_field_photos_yet():
    lab = evaluate_with([("a", "healthy")], {"a": "healthy"})
    rows = {metric: (a, b, gap) for metric, a, b, gap in eval_cnn.comparison_rows(lab, {"n": 0}, CLASSES)}

    assert rows["Images"] == ("1", "0", "")
    assert rows["Accuracy"] == ("1.000", "n/a", "n/a")


def test_markdown_has_one_table_with_the_three_columns():
    rows = [("Accuracy", "0.950", "0.700", "-0.250")]
    text = eval_cnn.render_markdown(rows, "data/splits/test.csv", "data/field_images", "models/cnn.tflite")
    assert "| Metric | Lab (PlantVillage test) | Field (farm photos) | Gap (field − lab) |" in text
    assert "| Accuracy | 0.950 | 0.700 | -0.250 |" in text


# --- finding the images -------------------------------------------------


def test_field_folder_uses_class_subfolders_and_skips_unlabelled(tmp_path):
    make_image(tmp_path / "late_blight" / "IMG_001.JPG")
    make_image(tmp_path / "healthy" / "IMG_002.png")
    make_image(tmp_path / "unlabelled" / "IMG_003.jpg")
    (tmp_path / "late_blight" / "notes.txt").write_text("not an image")

    items = eval_cnn.items_from_folder(tmp_path, CLASSES)

    assert sorted((p.name, label) for p, label in items) == [
        ("IMG_001.JPG", "late_blight"), ("IMG_002.png", "healthy"),
    ]


def test_missing_field_folder_gives_no_items(tmp_path):
    assert eval_cnn.items_from_folder(tmp_path / "nope", CLASSES) == []


def test_lab_set_from_the_committed_test_split():
    items = eval_cnn.lab_items(eval_cnn.DEFAULT_LAB, CLASSES)

    assert len(items) == 324  # the same test set as the Colab notebook
    assert {label for _, label in items} == set(CLASSES)
    assert items[0][0].as_posix().endswith(".jpg")


def test_labels_file_is_read_in_order(tmp_path):
    path = tmp_path / "labels.txt"
    path.write_text("early_blight\nhealthy\nlate_blight\n", encoding="utf-8")
    assert eval_cnn.read_labels(path) == CLASSES
