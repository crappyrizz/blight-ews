"""Tests for exporting from the downloaded PlantVillage archive."""
import io
import zipfile

import pytest
from PIL import Image

from ml import prepare_images as pi

AUGMENTED_ROOT = "Plant_leave_diseases_dataset_with_augmentation"
PLAIN_ROOT = "Plant_leave_diseases_dataset_without_augmentation"
POTATO_DIRS = ("Potato___Early_blight", "Potato___Late_blight", "Potato___healthy")


def jpeg_bytes(colour) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def build_archive(path, per_class=2, include_augmented=True, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        roots = [PLAIN_ROOT] + ([AUGMENTED_ROOT] if include_augmented else [])
        for root in roots:
            for directory in POTATO_DIRS:
                for index in range(per_class):
                    # A different colour per image, so none are duplicates.
                    shade = (10 * index + len(directory) + len(root)) % 250
                    archive.writestr(
                        f"{root}/{directory}/{directory}_{index}.JPG",
                        jpeg_bytes((shade, shade, shade)),
                    )
            archive.writestr(f"{root}/Tomato___healthy/t_0.JPG", jpeg_bytes((1, 2, 3)))
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return path


# --- picking the right entries out of the archive ----------------------


def test_only_potato_entries_are_selected(tmp_path):
    archive = build_archive(tmp_path / "pv.zip", per_class=3)

    with zipfile.ZipFile(archive) as handle:
        grouped = pi.archive_potato_entries(handle.namelist())

    assert sorted(grouped) == ["early_blight", "healthy", "late_blight"]
    assert all(len(entries) == 3 for entries in grouped.values())


def test_augmented_copies_are_excluded(tmp_path):
    """Augmented images are transformations of the originals: letting them in
    would put near-duplicates on both sides of the train/test split."""
    archive = build_archive(tmp_path / "pv.zip", per_class=2, include_augmented=True)

    with zipfile.ZipFile(archive) as handle:
        grouped = pi.archive_potato_entries(handle.namelist())

    for entries in grouped.values():
        assert all(entry.startswith(PLAIN_ROOT) for entry in entries)
        assert len(entries) == 2  # not 4


def test_missing_potato_class_stops_the_export(tmp_path):
    path = tmp_path / "pv.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for directory in ("Potato___Early_blight", "Potato___healthy"):
            archive.writestr(f"{PLAIN_ROOT}/{directory}/x.JPG", jpeg_bytes((5, 5, 5)))

    with zipfile.ZipFile(path) as handle:
        with pytest.raises(ValueError, match="Expected potato classes"):
            pi.archive_potato_entries(handle.namelist())


# --- the export itself -------------------------------------------------


def test_images_are_written_per_class(tmp_path):
    archive = build_archive(tmp_path / "pv.zip", per_class=4)
    out = tmp_path / "out"

    result = pi.export_from_archive(archive, image_dir=out)

    assert result["written"] == {"early_blight": 4, "late_blight": 4, "healthy": 4}
    assert result["unreadable"] == 0
    assert result["duplicates"] == 0
    assert len(list((out / "healthy").glob("*.jpg"))) == 4
    assert "Mendeley archive" in result["source"]


def test_duplicate_images_are_dropped(tmp_path):
    """Two files with identical pixels count once."""
    same = jpeg_bytes((77, 77, 77))
    path = tmp_path / "pv.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for directory in POTATO_DIRS:
            archive.writestr(f"{PLAIN_ROOT}/{directory}/a.JPG", same)
            archive.writestr(f"{PLAIN_ROOT}/{directory}/b.JPG", same)

    result = pi.export_from_archive(path, image_dir=tmp_path / "out")

    # One image survives overall: the same pixels in every class folder.
    assert sum(result["written"].values()) == 1
    assert result["duplicates"] == 5


def test_unreadable_images_are_dropped_and_counted(tmp_path):
    archive = build_archive(
        tmp_path / "pv.zip", per_class=2,
        extra={f"{PLAIN_ROOT}/Potato___healthy/broken.JPG": b"this is not an image"},
    )

    result = pi.export_from_archive(archive, image_dir=tmp_path / "out")

    assert result["unreadable"] == 1
    assert result["written"]["healthy"] == 2


def test_limit_per_class_is_respected(tmp_path):
    archive = build_archive(tmp_path / "pv.zip", per_class=5)

    result = pi.export_from_archive(archive, image_dir=tmp_path / "out", limit_per_class=2)

    assert result["written"] == {"early_blight": 2, "late_blight": 2, "healthy": 2}


def test_exported_files_are_readable_jpegs(tmp_path):
    archive = build_archive(tmp_path / "pv.zip", per_class=1)
    out = tmp_path / "out"

    pi.export_from_archive(archive, image_dir=out)

    for path in out.rglob("*.jpg"):
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert image.mode == "RGB"
