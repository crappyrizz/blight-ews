"""Tests for the split logic in ml/prepare_images.py.

No TensorFlow here: the export step is imported lazily, so the split and
report code is tested on plain file paths.
"""
import pytest

from ml import prepare_images as pi


def fake_files(tmp_path, counts):
    """Create empty .jpg files per class and return {class: [paths]}."""
    files = {}
    for class_name, n in counts.items():
        directory = tmp_path / class_name
        directory.mkdir(parents=True)
        paths = []
        for index in range(n):
            path = directory / f"{class_name}_{index:05d}.jpg"
            path.touch()
            paths.append(path)
        files[class_name] = paths
    return files


# --- reading the class names from the dataset --------------------------


def test_potato_labels_are_read_from_the_dataset_names():
    names = [
        "Apple___healthy",
        "Potato___Early_blight",
        "Potato___healthy",
        "Tomato___Late_blight",
        "Potato___Late_blight",
    ]

    assert pi.potato_labels(names) == {1: "early_blight", 2: "healthy", 4: "late_blight"}


def test_unexpected_potato_classes_stop_the_export():
    """If the dataset changes, fail loudly instead of mislabelling the export."""
    names = ["Potato___Early_blight", "Potato___healthy"]  # late blight missing

    with pytest.raises(ValueError, match="Expected potato classes"):
        pi.potato_labels(names)


# --- the split ---------------------------------------------------------


def test_split_is_70_15_15_per_class(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 100, "late_blight": 100, "healthy": 20})

    splits = pi.stratified_split(files)
    counts = pi.split_counts(splits)

    assert counts["train"]["early_blight"] == 70
    assert counts["val"]["early_blight"] == 15
    assert counts["test"]["early_blight"] == 15
    # The small class keeps its share in every split (stratified).
    assert counts["train"]["healthy"] == 14
    assert counts["val"]["healthy"] == 3
    assert counts["test"]["healthy"] == 3


def test_every_file_lands_in_exactly_one_split(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 37, "late_blight": 11, "healthy": 5})

    splits = pi.stratified_split(files)
    paths = [path for rows in splits.values() for path, _ in rows]

    assert len(paths) == 53  # nothing lost by the integer division
    assert len(set(paths)) == 53  # and nothing duplicated


def test_no_file_appears_in_two_splits(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 40, "late_blight": 40, "healthy": 9})

    splits = pi.stratified_split(files)
    train = {path for path, _ in splits["train"]}
    val = {path for path, _ in splits["val"]}
    test = {path for path, _ in splits["test"]}

    assert train & val == set()
    assert train & test == set()
    assert val & test == set()


def test_same_seed_gives_the_same_split(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 30, "late_blight": 30, "healthy": 7})

    first = pi.stratified_split(files, seed=42)
    second = pi.stratified_split(files, seed=42)

    assert first == second


def test_a_different_seed_gives_a_different_split(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 60, "late_blight": 60, "healthy": 20})

    assert pi.stratified_split(files, seed=42) != pi.stratified_split(files, seed=7)


def test_split_does_not_depend_on_the_order_files_are_listed(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 30, "late_blight": 30, "healthy": 7})
    shuffled = {name: list(reversed(paths)) for name, paths in files.items()}

    assert pi.stratified_split(files) == pi.stratified_split(shuffled)


def test_labels_match_the_folder_each_file_came_from(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 10, "healthy": 10, "late_blight": 10})

    splits = pi.stratified_split(files)

    for rows in splits.values():
        for path, label in rows:
            assert f"/{label}/" in path


# --- writing and reading the CSVs -------------------------------------


def test_splits_round_trip_through_csv(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 10, "late_blight": 10, "healthy": 4})
    splits = pi.stratified_split(files)
    splits_dir = tmp_path / "splits"

    pi.write_splits(splits, splits_dir)

    assert sorted(p.name for p in splits_dir.glob("*.csv")) == [
        "test.csv", "train.csv", "val.csv"
    ]
    assert pi.read_splits(splits_dir) == splits
    header = (splits_dir / "train.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "filepath,label"


def test_paths_are_stored_with_forward_slashes(tmp_path):
    files = fake_files(tmp_path, {"early_blight": 4, "late_blight": 4, "healthy": 4})

    splits = pi.stratified_split(files)

    for rows in splits.values():
        for path, _ in rows:
            assert "\\" not in path  # works on Windows and Linux alike


# --- the report --------------------------------------------------------


def test_imbalance_is_measured():
    counts = pi.split_counts({
        "train": [("a", "early_blight")] * 700 + [("b", "healthy")] * 100,
        "val": [], "test": [],
    })

    summary = pi.imbalance_summary(counts)

    assert summary["largest"] == "early_blight"
    assert summary["smallest"] == "healthy"
    assert summary["ratio"] == 7.0


def test_report_flags_the_imbalance_and_records_the_licence():
    counts = pi.split_counts({
        "train": [("a", "early_blight")] * 700 + [("b", "healthy")] * 100,
        "val": [("c", "early_blight")] * 150,
        "test": [("d", "early_blight")] * 150,
    })

    report = pi.render_report(counts, {"written": {"early_blight": 1000}, "unreadable": 2,
                                       "duplicates": 3, "source": "test archive"})

    assert "classes are imbalanced" in report
    assert "CC0 1.0" in report  # licence, taken from the data record
    assert "Hughes" in report  # citation
    assert "Unreadable images dropped: **2**" in report
    assert "duplicates dropped" in report.lower()
    assert "macro-averaged F1" in report  # says how to handle the imbalance
    assert "Export route: test archive" in report  # says where the images came from


def test_report_works_without_a_recorded_source():
    """An older integrity dict, or none at all, must not break the report."""
    counts = pi.split_counts({"train": [("a", "healthy")], "val": [], "test": []})

    assert "unrecorded" in pi.render_report(counts, {"written": {"healthy": 1},
                                                    "unreadable": 0, "duplicates": 0})
    assert "images already on disk" in pi.render_report(counts, None)
