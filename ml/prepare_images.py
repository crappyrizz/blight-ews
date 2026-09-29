"""Export the PlantVillage potato subset and build reproducible train/val/test splits.

The images come from TensorFlow Datasets' `plant_village`, which is the
PlantVillage collection (54,303 leaf images, 38 classes). Only the three
potato classes are exported: early blight, late blight and healthy.

Class names are read from the dataset's own metadata rather than typed in
here, so a change in the dataset is noticed instead of silently mis-labelling
the export. The three short names this project uses (early_blight,
late_blight, healthy) come from the part after '___' in the dataset's labels.

What it does:
  1. downloads (once, cached) and iterates the dataset;
  2. keeps potato images only, writing them as JPGs under
     data/plantvillage_potato/<class>/;
  3. drops images that cannot be decoded, and drops exact duplicates by MD5
     of the decoded pixels;
  4. writes a stratified 70/15/15 split (seed 42) to data/splits/*.csv;
  5. writes docs/data_report.md with the counts, the imbalance, and the
     dataset's source, citation and licence.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m ml.prepare_images

tensorflow-datasets (and TensorFlow) are only needed for step 1-2 and are
imported lazily, so the split and report code can be tested without them.
"""
import argparse
import csv
import hashlib
import logging
import sys
from collections import Counter
from pathlib import Path

from backend.config import REPO_ROOT

log = logging.getLogger("prepare_images")

DATASET_NAME = "plant_village"
SPECIES = "potato"  # matched against the part before '___' in dataset labels
EXPECTED_CLASSES = {"early_blight", "late_blight", "healthy"}

IMAGE_DIR = REPO_ROOT / "data" / "plantvillage_potato"
SPLITS_DIR = REPO_ROOT / "data" / "splits"
REPORT_PATH = REPO_ROOT / "docs" / "data_report.md"

SPLIT_FRACTIONS = {"train": 0.70, "val": 0.15, "test": 0.15}
SPLIT_SEED = 42

# Dataset provenance, recorded in the report. The licence is the one stated on
# the Mendeley record that TFDS downloads from, not an assumption.
DATASET_HOMEPAGE = "https://data.mendeley.com/datasets/tywbtsjrjv/1"
DATASET_DOI = "10.17632/tywbtsjrjv.1"
DATASET_LICENCE = "CC0 1.0 (public domain dedication)"
DATASET_CITATIONS = [
    "Hughes, D. P., & Salathé, M. (2015). An open access repository of images on plant "
    "health to enable the development of mobile disease diagnostics through machine "
    "learning and crowdsourcing. arXiv:1511.08060.",
    "Mohanty, S. P., Hughes, D. P., & Salathé, M. (2016). Using deep learning for "
    "image-based plant disease detection. Frontiers in Plant Science, 7, 1419.",
    "Arun Pandian, J., & Gopal, G. (2019). Data for: Identification of Plant Leaf "
    "Diseases Using a 9-layer Deep Convolutional Neural Network. Mendeley Data, V1. "
    "doi:10.17632/tywbtsjrjv.1.",
]


def short_class_name(dataset_label: str) -> str:
    """'Potato___Early_blight' -> 'early_blight'."""
    _, _, disease = dataset_label.partition("___")
    return disease.lower()


def potato_classes(label_names) -> dict[str, str]:
    """Map the dataset's potato label names to this project's class names.

    Raises if the potato classes are not the three expected ones, so a change
    in the dataset stops the export instead of quietly producing something
    else.
    """
    found = {
        name: short_class_name(name)
        for name in label_names
        if name.split("___")[0].lower() == SPECIES
    }
    if set(found.values()) != EXPECTED_CLASSES:
        raise ValueError(
            f"Expected potato classes {sorted(EXPECTED_CLASSES)}, "
            f"but the dataset has {sorted(found.values())}"
        )
    return found


def potato_labels(label_names: list[str]) -> dict[int, str]:
    """Same, but keyed by the dataset's label index (what TFDS gives us)."""
    by_name = potato_classes(label_names)
    return {
        index: by_name[name]
        for index, name in enumerate(label_names)
        if name in by_name
    }


# Mendeley (where TFDS fetches the archive from) answers 403 to the default
# python-requests User-Agent but serves the file to a browser-like one. Without
# this the download fails before it starts. It changes nothing about the data.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def use_browser_user_agent() -> None:
    """Send a browser User-Agent on TFDS's download requests."""
    import requests

    if getattr(requests.Session, "_blight_user_agent_patched", False):
        return

    original_request = requests.Session.request

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("User-Agent", BROWSER_USER_AGENT)
        kwargs["headers"] = headers
        return original_request(self, method, url, **kwargs)

    requests.Session.request = request
    requests.Session._blight_user_agent_patched = True


def export_images(image_dir: Path = IMAGE_DIR, limit_per_class: int | None = None) -> dict:
    """Write the potato images to disk, skipping unreadable ones and duplicates.

    Returns counts: written per class, plus how many were unreadable or
    duplicates.
    """
    # Imported here so the rest of this module works without TensorFlow.
    import numpy as np
    import tensorflow_datasets as tfds
    from PIL import Image

    use_browser_user_agent()
    builder = tfds.builder(DATASET_NAME)
    builder.download_and_prepare()
    label_feature = builder.info.features["label"]
    wanted = potato_labels(list(label_feature.names))
    log.info("Dataset labels for potato: %s", {label_feature.names[i]: n for i, n in wanted.items()})

    for class_name in sorted(set(wanted.values())):
        (image_dir / class_name).mkdir(parents=True, exist_ok=True)

    written = Counter()
    unreadable = 0
    duplicates = 0
    seen_hashes: set[str] = set()

    dataset = builder.as_dataset(split="train", shuffle_files=False)
    for example in tfds.as_numpy(dataset):
        label_index = int(example["label"])
        if label_index not in wanted:
            continue
        class_name = wanted[label_index]
        if limit_per_class is not None and written[class_name] >= limit_per_class:
            continue

        try:
            array = np.asarray(example["image"])
            image = Image.fromarray(array)
            # Hash the decoded pixels, so two files that differ only in JPEG
            # encoding still count as the same image.
            digest = hashlib.md5(array.tobytes()).hexdigest()
        except Exception as error:  # a corrupt record, not something to guess at
            log.warning("Unreadable image skipped: %s", error)
            unreadable += 1
            continue

        if digest in seen_hashes:
            duplicates += 1
            continue
        seen_hashes.add(digest)

        path = image_dir / class_name / f"{class_name}_{written[class_name]:05d}.jpg"
        image.convert("RGB").save(path, format="JPEG", quality=95)
        written[class_name] += 1

    return {
        "written": dict(written),
        "unreadable": unreadable,
        "duplicates": duplicates,
        "source": f"TensorFlow Datasets {DATASET_NAME}",
    }


# --- exporting from the downloaded archive ----------------------------
# Why this path exists: Mendeley (which hosts the dataset TFDS downloads)
# blocks TFDS's HTTP client with a 403 while serving the same file to a
# browser or curl, so the TFDS download cannot be relied on. Downloading the
# archive once with curl and exporting from it uses exactly the same CC0
# source, and needs no TensorFlow at all.
#
#   curl -L -A "Mozilla/5.0 ..." -o data/raw/plantvillage_mendeley.zip \
#     "https://data.mendeley.com/public-files/datasets/tywbtsjrjv/files/\
# d5652a28-c1d8-4b76-97f3-72fb80f94efc/file_downloaded"
#   python -m ml.prepare_images --from-archive data/raw/plantvillage_mendeley.zip

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
# The archive holds both an augmented and a non-augmented copy. Only the
# non-augmented one is wanted: augmented images are rotations and colour
# shifts of the originals, and letting them in would leak near-duplicates
# across the train/test split and flatter the model.
UNAUGMENTED_MARKER = "without_augmentation"


def archive_potato_entries(names: list[str]) -> dict[str, list[str]]:
    """Group the archive's potato image entries by this project's class names."""
    roots = {name.split("/")[0] for name in names if "/" in name}
    unaugmented = sorted(r for r in roots if UNAUGMENTED_MARKER in r.lower())
    root = unaugmented[0] if unaugmented else None

    directories = {
        name.split("/")[-2]
        for name in names
        if name.lower().endswith(IMAGE_SUFFIXES)
        and len(name.split("/")) >= 2
        and (root is None or name.startswith(f"{root}/"))
    }
    class_of = potato_classes(sorted(directories))

    grouped: dict[str, list[str]] = {short: [] for short in class_of.values()}
    for name in sorted(names):
        if not name.lower().endswith(IMAGE_SUFFIXES):
            continue
        if root is not None and not name.startswith(f"{root}/"):
            continue
        parts = name.split("/")
        if len(parts) < 2 or parts[-2] not in class_of:
            continue
        grouped[class_of[parts[-2]]].append(name)
    return grouped


def export_from_archive(
    archive_path: Path, image_dir: Path = IMAGE_DIR, limit_per_class: int | None = None
) -> dict:
    """Export the potato classes from the downloaded PlantVillage archive."""
    import zipfile

    from PIL import Image

    written = Counter()
    unreadable = 0
    duplicates = 0
    seen_hashes: set[str] = set()

    with zipfile.ZipFile(archive_path) as archive:
        grouped = archive_potato_entries(archive.namelist())
        log.info(
            "Archive holds %s",
            {name: len(entries) for name, entries in sorted(grouped.items())},
        )

        for class_name, entries in sorted(grouped.items()):
            (image_dir / class_name).mkdir(parents=True, exist_ok=True)
            for entry in entries:
                if limit_per_class is not None and written[class_name] >= limit_per_class:
                    break
                try:
                    with archive.open(entry) as handle:
                        image = Image.open(handle)
                        image = image.convert("RGB")  # forces a full decode
                except Exception as error:  # a truncated or corrupt file
                    log.warning("Unreadable image skipped (%s): %s", entry, error)
                    unreadable += 1
                    continue

                # Hash the decoded pixels, so two files that differ only in
                # their encoding still count as the same image.
                digest = hashlib.md5(image.tobytes()).hexdigest()
                if digest in seen_hashes:
                    duplicates += 1
                    continue
                seen_hashes.add(digest)

                path = image_dir / class_name / f"{class_name}_{written[class_name]:05d}.jpg"
                image.save(path, format="JPEG", quality=95)
                written[class_name] += 1

    return {
        "written": dict(written),
        "unreadable": unreadable,
        "duplicates": duplicates,
        "source": f"Mendeley archive {Path(archive_path).name}",
    }


def list_exported(image_dir: Path = IMAGE_DIR) -> dict[str, list[Path]]:
    """Exported files per class, sorted, so the split does not depend on disk order."""
    classes = {}
    for class_dir in sorted(p for p in image_dir.iterdir() if p.is_dir()):
        classes[class_dir.name] = sorted(class_dir.glob("*.jpg"))
    return classes


def stratified_split(
    files_by_class: dict[str, list[Path]], seed: int = SPLIT_SEED
) -> dict[str, list[tuple[str, str]]]:
    """Split each class 70/15/15 with a fixed seed.

    Stratified, so the small healthy class keeps its share in every split.
    Shuffling a sorted list with a seeded Random makes the result identical on
    every machine and every run, which is what makes the committed CSVs
    meaningful.
    """
    import random

    splits: dict[str, list[tuple[str, str]]] = {name: [] for name in SPLIT_FRACTIONS}

    for class_name, files in sorted(files_by_class.items()):
        shuffled = sorted(files)
        random.Random(seed).shuffle(shuffled)

        total = len(shuffled)
        n_train = int(total * SPLIT_FRACTIONS["train"])
        n_val = int(total * SPLIT_FRACTIONS["val"])
        # Remainder goes to test, so every file lands in exactly one split.
        chunks = {
            "train": shuffled[:n_train],
            "val": shuffled[n_train:n_train + n_val],
            "test": shuffled[n_train + n_val:],
        }
        for split_name, paths in chunks.items():
            splits[split_name].extend(
                (as_repo_path(path), class_name) for path in paths
            )

    return splits


def as_repo_path(path: Path) -> str:
    """Repo-relative path with forward slashes, so the CSVs work on any OS."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # Outside the repo (e.g. a temporary directory in a test): keep the
        # full path, still with forward slashes.
        return resolved.as_posix()


def write_splits(splits: dict[str, list[tuple[str, str]]], splits_dir: Path = SPLITS_DIR) -> None:
    splits_dir.mkdir(parents=True, exist_ok=True)
    for split_name, rows in splits.items():
        with (splits_dir / f"{split_name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["filepath", "label"])
            writer.writerows(rows)


def read_splits(splits_dir: Path = SPLITS_DIR) -> dict[str, list[tuple[str, str]]]:
    splits = {}
    for split_name in SPLIT_FRACTIONS:
        path = splits_dir / f"{split_name}.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader)  # header
            splits[split_name] = [(row[0], row[1]) for row in reader]
    return splits


def split_counts(splits: dict[str, list[tuple[str, str]]]) -> dict[str, Counter]:
    return {name: Counter(label for _, label in rows) for name, rows in splits.items()}


def imbalance_summary(counts: dict[str, Counter]) -> dict:
    """Totals per class and how lopsided they are."""
    totals = Counter()
    for counter in counts.values():
        totals.update(counter)
    if not totals:
        return {"totals": {}, "largest": None, "smallest": None, "ratio": None}

    largest, largest_n = max(totals.items(), key=lambda item: item[1])
    smallest, smallest_n = min(totals.items(), key=lambda item: item[1])
    return {
        "totals": dict(totals),
        "largest": largest,
        "smallest": smallest,
        "ratio": round(largest_n / smallest_n, 2) if smallest_n else None,
        "smallest_share": round(100 * smallest_n / sum(totals.values()), 1),
    }


def render_report(
    counts: dict[str, Counter], integrity: dict | None, seed: int = SPLIT_SEED
) -> str:
    """The markdown for docs/data_report.md."""
    classes = sorted({label for counter in counts.values() for label in counter})
    balance = imbalance_summary(counts)

    lines = [
        "# Image dataset report (PlantVillage potato subset)",
        "",
        "Generated by `ml/prepare_images.py`. Re-running it reproduces this exactly.",
        "",
        "## Class counts per split",
        "",
        "| Split | " + " | ".join(classes) + " | Total |",
        "|---" * (len(classes) + 2) + "|",
    ]
    for split_name in ("train", "val", "test"):
        counter = counts.get(split_name, Counter())
        row = [str(counter.get(c, 0)) for c in classes]
        lines.append(f"| {split_name} | " + " | ".join(row) + f" | {sum(counter.values())} |")

    totals = balance["totals"]
    lines.append(
        "| **all** | " + " | ".join(str(totals.get(c, 0)) for c in classes)
        + f" | {sum(totals.values())} |"
    )

    lines += [
        "",
        f"Split: 70/15/15, stratified per class, seed {seed}. Files:",
        "`data/splits/train.csv`, `val.csv`, `test.csv` (columns: filepath, label).",
        "These CSVs are committed; the images themselves are gitignored.",
        "",
        "## Class imbalance",
        "",
    ]
    if balance["ratio"]:
        lines += [
            f"**The classes are imbalanced.** `{balance['largest']}` has "
            f"{totals[balance['largest']]} images and `{balance['smallest']}` has "
            f"{totals[balance['smallest']]}, a ratio of {balance['ratio']}:1. The smallest "
            f"class is {balance['smallest_share']}% of the data.",
            "",
            "Why it matters here: a model can score well on overall accuracy while being "
            "unreliable on the small class, and `healthy` is the class that says \"do "
            "nothing\". So:",
            "",
            "- report per-class precision and recall, and a confusion matrix, not just accuracy;",
            "- use class weights or balanced sampling during training;",
            "- treat macro-averaged F1 as the headline number in Chapter 5.",
        ]
    else:
        lines.append("No counts available yet.")

    if integrity:
        lines += [
            "",
            "## Integrity checks",
            "",
            f"- Unreadable images dropped: **{integrity['unreadable']}**",
            f"- Exact duplicates dropped (MD5 of decoded pixels): **{integrity['duplicates']}**",
            f"- Images written: **{sum(integrity['written'].values())}** "
            + ", ".join(f"{k}: {v}" for k, v in sorted(integrity["written"].items())),
            f"- Exported from: {integrity.get('source', 'unknown source')}",
        ]

    lines += [
        "",
        "## Source, citation and licence",
        "",
        "- Dataset: PlantVillage, the standard leaf-disease collection "
        f"(TFDS `{DATASET_NAME}`: 54,303 images, 38 classes). Only the 3 potato classes "
        "are exported here.",
        f"- Data record: {DATASET_HOMEPAGE} (DOI {DATASET_DOI}).",
        f"- **Licence: {DATASET_LICENCE}**, as stated on that record.",
        f"- Export route: {integrity.get('source', 'unrecorded') if integrity else 'images already on disk'}. "
        "Mendeley refuses TFDS's downloader with HTTP 403 while serving the same file to a "
        "browser, so the archive is downloaded once with curl and exported from directly; "
        "the data is identical.",
        "- The archive holds both an augmented and a non-augmented copy. Only the "
        "non-augmented copy is used: the augmented images are rotations and colour shifts of "
        "the originals, which would put near-duplicates on both sides of the split.",
        "- The three potato class names come from the dataset's own label names "
        "(`Potato___Early_blight`, `Potato___Late_blight`, `Potato___healthy`) and are "
        "validated at export time rather than hard-coded, so a change in the dataset stops "
        "the export instead of mislabelling it.",
        "",
        "Citations:",
        "",
    ]
    lines += [f"{n}. {citation}" for n, citation in enumerate(DATASET_CITATIONS, start=1)]
    lines += [
        "",
        "## Known limitation",
        "",
        "PlantVillage images are single leaves photographed against a plain background in "
        "controlled conditions. The farmer will photograph leaves in a field, in daylight, "
        "against soil and other foliage. Accuracy on PlantVillage test data is therefore an "
        "upper bound, not an estimate of field performance; the field images collected under "
        "`data/field_images/` are what make a fair second evaluation possible.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skip-export", action="store_true",
        help="use the images already in data/plantvillage_potato/ and only rebuild splits",
    )
    parser.add_argument(
        "--from-archive", type=Path, default=None,
        help="export from a downloaded PlantVillage zip instead of through TFDS "
             "(see the note in this file: Mendeley blocks TFDS's downloader)",
    )
    parser.add_argument(
        "--limit-per-class", type=int, default=None,
        help="export at most N images per class (for a quick trial run)",
    )
    parser.add_argument("--seed", type=int, default=SPLIT_SEED)
    args = parser.parse_args(argv)

    integrity = None
    if args.skip_export:
        log.info("Skipping export; using images already on disk")
    elif args.from_archive is not None:
        integrity = export_from_archive(args.from_archive, limit_per_class=args.limit_per_class)
    else:
        integrity = export_images(limit_per_class=args.limit_per_class)

    if integrity is not None:
        log.info(
            "Exported %s images from %s (%s unreadable, %s duplicates dropped)",
            sum(integrity["written"].values()), integrity["source"],
            integrity["unreadable"], integrity["duplicates"],
        )

    files_by_class = list_exported()
    splits = stratified_split(files_by_class, seed=args.seed)
    write_splits(splits)

    counts = split_counts(splits)
    for split_name in ("train", "val", "test"):
        log.info("%s: %s", split_name, dict(sorted(counts[split_name].items())))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(counts, integrity, seed=args.seed), encoding="utf-8")
    log.info("Wrote %s", as_repo_path(REPORT_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
