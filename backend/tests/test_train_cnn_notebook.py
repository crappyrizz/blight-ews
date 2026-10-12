"""Checks on ml/train_cnn.ipynb that do not need Colab, TensorFlow or the images.

The notebook rebuilds the train/val/test split in Colab and asserts it equals
data/splits/*.csv. Here we run its helper cell and confirm that rebuild gives
exactly the committed CSVs, so a mismatch is caught before anyone spends a
GPU session finding out.
"""
import ast
import json

import pytest

from backend.config import REPO_ROOT
from ml import prepare_images

NOTEBOOK = REPO_ROOT / "ml" / "train_cnn.ipynb"
SPLITS = REPO_ROOT / "data" / "splits"


@pytest.fixture(scope="module")
def notebook():
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def helpers(notebook):
    """The namespace produced by running the cell tagged 'helpers'."""
    tagged = [c for c in notebook["cells"] if "helpers" in c["metadata"].get("tags", [])]
    assert len(tagged) == 1
    namespace = {"SEED": 42}
    exec("".join(tagged[0]["source"]), namespace)
    return namespace


def source_filenames(counts):
    """Archive-style names ('image (1).JPG' ...) for the given class counts."""
    return {c: [f"image ({i}).JPG" for i in range(1, n + 1)] for c, n in counts.items()}


def committed(split_name, helpers):
    return helpers["parse_split_csv"]((SPLITS / f"{split_name}.csv").read_text(encoding="utf-8"))


def test_every_code_cell_is_valid_python(notebook):
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]))


def test_notebook_asks_colab_for_a_gpu(notebook):
    assert notebook["metadata"]["accelerator"] == "GPU"


def test_notebook_has_no_saved_outputs(notebook):
    assert all(not c.get("outputs") for c in notebook["cells"] if c["cell_type"] == "code")


def test_rebuilt_split_equals_the_committed_csvs(helpers):
    rebuilt = helpers["rebuild_split"](
        source_filenames({"early_blight": 1000, "healthy": 152, "late_blight": 1000})
    )
    for split_name in ("train", "val", "test"):
        assert rebuilt[split_name] == committed(split_name, helpers), split_name


def test_rebuild_matches_prepare_images(helpers, tmp_path):
    """The notebook's copy of the split rule agrees with the original in ml/prepare_images.py."""
    counts = {"early_blight": 23, "healthy": 7, "late_blight": 19}
    rebuilt = helpers["rebuild_split"](source_filenames(counts))

    files = {c: [tmp_path / f"{c}_{i:05d}.jpg" for i in range(n)] for c, n in counts.items()}
    original = prepare_images.stratified_split(files, seed=42)
    for split_name in ("train", "val", "test"):
        names = [(path.rsplit("/", 1)[1], label) for path, label in rebuilt[split_name]]
        expected = [(path.rsplit("/", 1)[1], label) for path, label in original[split_name]]
        assert names == expected


def test_export_names_follow_sorted_source_filename_order(helpers):
    paths = helpers["export_paths"]({"healthy": ["image (10).JPG", "image (1).JPG", "image (2).JPG"]})
    # String order, as the archive export sorted them: (1) < (10) < (2).
    assert paths["healthy"] == {
        "image (1).JPG": "data/plantvillage_potato/healthy/healthy_00000.jpg",
        "image (10).JPG": "data/plantvillage_potato/healthy/healthy_00001.jpg",
        "image (2).JPG": "data/plantvillage_potato/healthy/healthy_00002.jpg",
    }


def test_potato_labels_are_read_from_the_dataset_names(helpers):
    names = ["Apple___Apple_scab", "Potato___Early_blight", "Potato___healthy",
             "Potato___Late_blight", "Tomato___healthy"]
    assert helpers["potato_label_names"](names) == {
        "Potato___Early_blight": "early_blight",
        "Potato___healthy": "healthy",
        "Potato___Late_blight": "late_blight",
    }


def test_unexpected_potato_classes_stop_the_notebook(helpers):
    with pytest.raises(ValueError, match="Expected potato classes"):
        helpers["potato_label_names"](["Potato___Early_blight", "Potato___healthy"])


def test_class_order_is_the_labels_txt_order(helpers):
    assert helpers["CLASS_NAMES"] == ["early_blight", "healthy", "late_blight"]
