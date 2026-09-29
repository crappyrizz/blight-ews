"""Tests for ml/register_field_images.py (the farmer's weekly photos)."""
import csv

import pytest

from ml import register_field_images as rfi


@pytest.mark.parametrize(
    "filename",
    [
        "20260921_plotA_001.jpg",
        "20260921_plot-a_1.jpg",
        "20250101_A1_0001.jpg",
        "20240229_plotA_001.jpg",  # a real leap day
        "20260921_plotA_001.JPG",
        "20260921_plotA_001.jpeg",
    ],
)
def test_valid_filenames_are_accepted(filename):
    ok, reason = rfi.validate_filename(filename)
    assert ok, reason


@pytest.mark.parametrize(
    "filename",
    [
        "20260921_plotA.jpg",  # no sequence
        "20260921_001.jpg",  # no plot
        "2026921_plotA_001.jpg",  # 7-digit date
        "20261301_plotA_001.jpg",  # month 13
        "20260231_plotA_001.jpg",  # 31 February
        "20260921_plot A_001.jpg",  # space in the plot
        "20260921_plotA_001.png",  # not a JPEG
        "IMG_1234.jpg",  # straight off a phone
        "20260921_plotA_001",  # no extension
    ],
)
def test_invalid_filenames_are_rejected(filename):
    ok, reason = rfi.validate_filename(filename)
    assert not ok
    assert reason


def test_filename_is_parsed_into_date_plot_and_sequence():
    assert rfi.parse_filename("20260921_plotA_007.jpg") == {
        "capture_date": "2026-09-21",
        "plot_id": "plotA",
        "sequence": 7,
    }


def test_parsing_an_invalid_name_raises():
    with pytest.raises(ValueError):
        rfi.parse_filename("IMG_1234.jpg")


# --- the folder structure and metadata.csv ----------------------------


def test_structure_and_csv_header_are_created(tmp_path):
    rfi.ensure_structure(tmp_path)

    for label in rfi.LABEL_DIRS:
        assert (tmp_path / label).is_dir()
        assert (tmp_path / label / ".gitkeep").exists()
    header = (tmp_path / "metadata.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == rfi.METADATA_COLUMNS


def add_photo(field_dir, label, filename):
    (field_dir / label).mkdir(parents=True, exist_ok=True)
    (field_dir / label / filename).write_bytes(b"not-really-a-jpeg")


def rows_of(field_dir):
    with (field_dir / "metadata.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_new_photos_are_registered_with_their_folder_as_the_label(tmp_path):
    rfi.ensure_structure(tmp_path)
    add_photo(tmp_path, "late_blight", "20260921_plotA_001.jpg")
    add_photo(tmp_path, "healthy", "20260921_plotA_002.jpg")

    result = rfi.register(tmp_path, label_source="farmer")

    assert (result["found"], result["added"]) == (2, 0 + 2)
    rows = {row["filename"]: row for row in rows_of(tmp_path)}
    assert rows["20260921_plotA_001.jpg"]["label"] == "late_blight"
    assert rows["20260921_plotA_001.jpg"]["label_source"] == "farmer"
    assert rows["20260921_plotA_001.jpg"]["capture_date"] == "2026-09-21"
    assert rows["20260921_plotA_001.jpg"]["plot_id"] == "plotA"
    assert rows["20260921_plotA_001.jpg"]["image_id"] == "20260921_plotA_001"


def test_unlabelled_photos_get_no_label_and_no_label_source(tmp_path):
    rfi.ensure_structure(tmp_path)
    add_photo(tmp_path, "unlabelled", "20260921_plotB_001.jpg")

    rfi.register(tmp_path, label_source="farmer")

    row = rows_of(tmp_path)[0]
    assert row["label"] == ""
    # A photo with no label cannot have a source for one.
    assert row["label_source"] == "unknown"


def test_rescanning_adds_nothing_and_keeps_hand_edits(tmp_path):
    rfi.ensure_structure(tmp_path)
    add_photo(tmp_path, "unlabelled", "20260921_plotA_001.jpg")
    rfi.register(tmp_path)

    # The researcher later labels it by hand in the CSV.
    path = tmp_path / "metadata.csv"
    text = path.read_text(encoding="utf-8").replace(
        "20260921_plotA_001,20260921_plotA_001.jpg,2026-09-21,plotA,,unknown,",
        "20260921_plotA_001,20260921_plotA_001.jpg,2026-09-21,plotA,late_blight,researcher,"
        "confirmed under a lens",
    )
    path.write_text(text, encoding="utf-8")

    result = rfi.register(tmp_path)

    assert result["added"] == 0
    assert result["already_registered"] == 1
    row = rows_of(tmp_path)[0]
    assert row["label"] == "late_blight"
    assert row["label_source"] == "researcher"
    assert row["notes"] == "confirmed under a lens"


def test_only_new_photos_are_appended(tmp_path):
    rfi.ensure_structure(tmp_path)
    add_photo(tmp_path, "healthy", "20260921_plotA_001.jpg")
    rfi.register(tmp_path)

    add_photo(tmp_path, "healthy", "20260928_plotA_001.jpg")
    result = rfi.register(tmp_path)

    assert (result["added"], result["already_registered"]) == (1, 1)
    assert len(rows_of(tmp_path)) == 2


def test_badly_named_files_are_reported_and_not_registered(tmp_path):
    rfi.ensure_structure(tmp_path)
    add_photo(tmp_path, "healthy", "IMG_4021.jpg")
    add_photo(tmp_path, "healthy", "20260921_plotA_001.jpg")

    result = rfi.register(tmp_path)

    assert result["added"] == 1
    assert [name for name, _ in result["invalid"]] == ["healthy/IMG_4021.jpg"]
    assert len(rows_of(tmp_path)) == 1


def test_gitkeep_files_are_ignored(tmp_path):
    rfi.ensure_structure(tmp_path)

    result = rfi.register(tmp_path)

    assert result == {"found": 0, "added": 0, "already_registered": 0, "invalid": []}


def test_an_unknown_label_source_is_refused(tmp_path):
    with pytest.raises(ValueError):
        rfi.register(tmp_path, label_source="the-neighbour")
