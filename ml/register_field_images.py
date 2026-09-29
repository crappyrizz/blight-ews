"""Register the farmer's weekly field photos in data/field_images/metadata.csv.

Why this exists: PlantVillage is lab imagery on a plain background, so a model
scored only on it tells us little about the farmer's own photos. The weekly
photos from the Kinangop plot are the second, harder evaluation set, and they
are only useful if each one has a date, a plot and an honest account of where
its label came from.

Folder layout (the folder is the label):
    data/field_images/early_blight/
    data/field_images/late_blight/
    data/field_images/healthy/
    data/field_images/unlabelled/      <- no label yet

Filename convention: YYYYMMDD_<plot>_<seq>.jpg
    20260921_plotA_001.jpg
The date is the capture date, <plot> identifies the plot or bed, and <seq>
separates several photos taken the same day.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m ml.register_field_images
    .\\.venv\\Scripts\\python.exe -m ml.register_field_images --label-source farmer

Existing rows are never rewritten, only new files are appended, so anything
edited by hand in the CSV (a corrected label, a note) survives a re-scan.
"""
import argparse
import csv
import logging
import re
import sys
from datetime import date
from pathlib import Path

from backend.config import REPO_ROOT

log = logging.getLogger("register_field_images")

FIELD_DIR = REPO_ROOT / "data" / "field_images"
METADATA_PATH = FIELD_DIR / "metadata.csv"

LABEL_DIRS = ("early_blight", "late_blight", "healthy", "unlabelled")
UNLABELLED = "unlabelled"

LABEL_SOURCES = ("farmer", "researcher", "unknown")

METADATA_COLUMNS = [
    "image_id", "filename", "capture_date", "plot_id", "label", "label_source", "notes",
]

# YYYYMMDD_<plot>_<seq>.jpg  — plot: letters, digits and hyphens; seq: 1-4 digits.
FILENAME_PATTERN = re.compile(r"^(\d{8})_([A-Za-z0-9-]+)_(\d{1,4})\.jpe?g$", re.IGNORECASE)


def validate_filename(filename: str) -> tuple[bool, str]:
    """Check one filename against the convention. Returns (ok, reason)."""
    match = FILENAME_PATTERN.match(filename)
    if not match:
        return False, "expected YYYYMMDD_<plot>_<seq>.jpg, e.g. 20260921_plotA_001.jpg"

    stamp = match.group(1)
    try:
        date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
    except ValueError:
        return False, f"{stamp} is not a real date"
    return True, ""


def parse_filename(filename: str) -> dict:
    """Pull the capture date, plot and sequence out of a valid filename."""
    ok, reason = validate_filename(filename)
    if not ok:
        raise ValueError(f"{filename}: {reason}")

    stamp, plot_id, sequence = FILENAME_PATTERN.match(filename).groups()
    return {
        "capture_date": date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8])).isoformat(),
        "plot_id": plot_id,
        "sequence": int(sequence),
    }


def ensure_structure(field_dir: Path = FIELD_DIR) -> None:
    """Create the four folders and the CSV header if they are not there yet."""
    for label in LABEL_DIRS:
        directory = field_dir / label
        directory.mkdir(parents=True, exist_ok=True)
        keep = directory / ".gitkeep"
        if not keep.exists():
            keep.touch()

    metadata = field_dir / "metadata.csv"
    if not metadata.exists():
        with metadata.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(METADATA_COLUMNS)


def read_metadata(metadata_path: Path = METADATA_PATH) -> list[dict]:
    if not metadata_path.exists():
        return []
    with metadata_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def scan_images(field_dir: Path = FIELD_DIR) -> tuple[list[dict], list[tuple[str, str]]]:
    """Find image files in the label folders.

    Returns (found, invalid): `found` is one dict per valid file, `invalid` is
    (filename, reason) pairs, which are reported rather than registered.
    """
    found = []
    invalid = []

    for label in LABEL_DIRS:
        directory = field_dir / label
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name == ".gitkeep":
                continue
            ok, reason = validate_filename(path.name)
            if not ok:
                invalid.append((f"{label}/{path.name}", reason))
                continue
            parsed = parse_filename(path.name)
            found.append({
                "filename": path.name,
                "folder_label": label,
                "capture_date": parsed["capture_date"],
                "plot_id": parsed["plot_id"],
            })

    return found, invalid


def new_rows(found: list[dict], existing: list[dict], label_source: str) -> list[dict]:
    """Rows for files that are not in the CSV yet."""
    known = {row["filename"] for row in existing}
    rows = []
    for item in found:
        if item["filename"] in known:
            continue
        labelled = item["folder_label"] != UNLABELLED
        rows.append({
            # The filename stem is unique by the naming convention, so it makes
            # a stable, readable ID that survives re-scans.
            "image_id": Path(item["filename"]).stem,
            "filename": item["filename"],
            "capture_date": item["capture_date"],
            "plot_id": item["plot_id"],
            "label": item["folder_label"] if labelled else "",
            # An unlabelled photo has no label, so it can have no source for one.
            "label_source": label_source if labelled else "unknown",
            "notes": "",
        })
    return rows


def append_rows(rows: list[dict], metadata_path: Path = METADATA_PATH) -> None:
    if not rows:
        return
    with metadata_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA_COLUMNS)
        writer.writerows(rows)


def register(
    field_dir: Path = FIELD_DIR, label_source: str = "unknown"
) -> dict:
    """Scan the folders and append any new files to metadata.csv."""
    if label_source not in LABEL_SOURCES:
        raise ValueError(f"label_source must be one of {LABEL_SOURCES}")

    ensure_structure(field_dir)
    metadata_path = field_dir / "metadata.csv"
    existing = read_metadata(metadata_path)
    found, invalid = scan_images(field_dir)
    rows = new_rows(found, existing, label_source)
    append_rows(rows, metadata_path)

    return {
        "found": len(found),
        "added": len(rows),
        "already_registered": len(found) - len(rows),
        "invalid": invalid,
    }


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--label-source", choices=LABEL_SOURCES, default="unknown",
        help="who labelled the photos in this batch (default: unknown)",
    )
    args = parser.parse_args(argv)

    result = register(label_source=args.label_source)
    log.info(
        "%s image(s) found, %s newly registered, %s already known",
        result["found"], result["added"], result["already_registered"],
    )
    for filename, reason in result["invalid"]:
        log.warning("Skipped %s: %s", filename, reason)
    if result["invalid"]:
        log.warning(
            "%s file(s) were skipped for breaking the naming convention; rename and re-run",
            len(result["invalid"]),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
