"""Evaluate the exported TFLite CNN locally: lab images vs field photos.

The CNN is trained on PlantVillage: single leaves on a plain background in
controlled light. The farmer photographs leaves in a field, against soil and
other foliage, in whatever light there is. This script runs the SAME TFLite
model, with the SAME preprocessing, on both, and puts the results side by
side. The difference between the two columns is the lab-to-field domain gap.

(a) Lab:   the PlantVillage test images. By default, the committed test
           split (data/splits/test.csv), which is exactly the test set used
           in the Colab notebook. Or any folder with <class>/ subfolders.
(b) Field: data/field_images/<class>/ (the 'unlabelled' folder is ignored).

Run from the repo root, with the ML extras installed (requirements-ml.txt):

    .\\.venv\\Scripts\\python.exe -m ml.eval_cnn
    .\\.venv\\Scripts\\python.exe -m ml.eval_cnn --lab path\\to\\plantvillage_test_folder

It prints one metrics table and writes it to docs/cnn_domain_gap.md, with the
numbers also in docs/cnn_domain_gap.json.

Preprocessing must match ml/train_cnn.ipynb exactly: RGB, bilinear
antialiased resize to 224x224, then x / 127.5 - 1 (what MobileNetV2's
preprocess_input does). The model's outputs are in labels.txt order.
"""
import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from backend.config import REPO_ROOT

IMAGE_SIZE = 224
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")

DEFAULT_MODEL = REPO_ROOT / "models" / "cnn.tflite"
DEFAULT_LABELS = REPO_ROOT / "models" / "labels.txt"
DEFAULT_LAB = REPO_ROOT / "data" / "splits" / "test.csv"
DEFAULT_FIELD = REPO_ROOT / "data" / "field_images"
DEFAULT_REPORT = REPO_ROOT / "docs" / "cnn_domain_gap.md"


# --- input -------------------------------------------------------------


def preprocess(path: Path) -> np.ndarray:
    """One image file -> float32 array [1, 224, 224, 3] scaled to -1..1.

    Exactly the server's preprocessing (backend/cnn_service.py: EXIF
    rotation, centre square crop, float bilinear resize, x / 127.5 - 1), so
    the field numbers here are the field numbers the farmer gets.
    """
    from backend import cnn_service

    return cnn_service.to_model_input(cnn_service.load_image(Path(path).read_bytes()))


def read_labels(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def items_from_csv(csv_path: Path, root: Path = REPO_ROOT) -> list[tuple[Path, str]]:
    """(image path, true class) for each row of a split CSV (filepath,label)."""
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [(root / row["filepath"], row["label"]) for row in csv.DictReader(handle)]


def items_from_folder(folder: Path, classes: list[str]) -> list[tuple[Path, str]]:
    """(image path, true class) for images in <folder>/<class>/, for known classes only."""
    items = []
    for class_name in classes:
        class_dir = folder / class_name
        if class_dir.is_dir():
            items.extend(
                (path, class_name)
                for path in sorted(class_dir.iterdir())
                if path.suffix.lower() in IMAGE_SUFFIXES
            )
    return items


def lab_items(source: Path, classes: list[str]) -> list[tuple[Path, str]]:
    """The lab set from either a split CSV or a folder of <class>/ subfolders."""
    if source.suffix.lower() == ".csv":
        return items_from_csv(source)
    return items_from_folder(source, classes)


# --- model -------------------------------------------------------------


class TFLiteClassifier:
    """Runs the TFLite model on one preprocessed image at a time."""

    def __init__(self, model_path: Path):
        try:  # the standalone runtime, if installed
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:  # otherwise the one inside TensorFlow
            import tensorflow as tf

            Interpreter = tf.lite.Interpreter
        self.interpreter = Interpreter(model_path=str(model_path))
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]

    def __call__(self, image: np.ndarray) -> np.ndarray:
        self.interpreter.set_tensor(self.input["index"], image.astype(self.input["dtype"]))
        self.interpreter.invoke()
        return self.interpreter.get_tensor(self.output["index"])[0]


# --- metrics -----------------------------------------------------------


def evaluate(items, predict, classes: list[str]) -> dict:
    """Accuracy, macro F1 and per-class precision / recall / F1 for one image set.

    `predict` maps a preprocessed image to class probabilities. A precision
    with no predictions of that class, or a recall with no images of it, is
    None ("not measurable"), not 0: with no field photos of a class yet, we
    have no evidence either way. Macro F1 averages over the classes that have
    images in the set.
    """
    if not items:
        return {"n": 0}

    index_of = {name: i for i, name in enumerate(classes)}
    truth = np.array([index_of[label] for _, label in items])
    predicted = np.array([int(np.argmax(predict(preprocess(path)))) for path, _ in items])

    confusion = np.zeros((len(classes), len(classes)), dtype=int)
    for t, p in zip(truth, predicted, strict=True):
        confusion[t, p] += 1

    per_class = {}
    for i, name in enumerate(classes):
        true_positive = confusion[i, i]
        predicted_as = confusion[:, i].sum()
        actually = confusion[i, :].sum()
        precision = true_positive / predicted_as if predicted_as else None
        recall = true_positive / actually if actually else None
        if precision is None or recall is None:
            f1 = None
        elif precision + recall == 0:
            f1 = 0.0
        else:
            f1 = 2 * precision * recall / (precision + recall)
        per_class[name] = {"precision": precision, "recall": recall, "f1": f1, "support": int(actually)}

    present = [per_class[name]["f1"] or 0.0 for name in classes if per_class[name]["support"]]
    return {
        "n": len(items),
        "accuracy": float((truth == predicted).mean()),
        "macro_f1": float(np.mean(present)),
        "per_class": per_class,
        "confusion_matrix": confusion.tolist(),
    }


# --- the comparison table ---------------------------------------------


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _gap(lab, field) -> str:
    if lab is None or field is None:
        return "n/a"
    return f"{field - lab:+.3f}"


def comparison_rows(lab: dict, field: dict, classes: list[str]) -> list[tuple[str, str, str, str]]:
    """(metric, lab, field, gap = field - lab) rows for the table."""
    def get(result, *keys):
        value = result
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    rows = [("Images", str(lab.get("n", 0)), str(field.get("n", 0)), "")]
    for label, keys in (("Accuracy", ("accuracy",)), ("Macro F1", ("macro_f1",))):
        a, b = get(lab, *keys), get(field, *keys)
        rows.append((label, _fmt(a), _fmt(b), _gap(a, b)))
    for name in classes:
        for metric in ("precision", "recall", "f1"):
            a, b = get(lab, "per_class", name, metric), get(field, "per_class", name, metric)
            rows.append((f"{name} {metric}", _fmt(a), _fmt(b), _gap(a, b)))
        a, b = get(lab, "per_class", name, "support"), get(field, "per_class", name, "support")
        rows.append((f"{name} images", str(a or 0), str(b or 0), ""))
    return rows


def render_markdown(rows, lab_source: str, field_source: str, model_path: str) -> str:
    lines = [
        "# CNN: lab vs field (domain gap)",
        "",
        "Generated by `ml/eval_cnn.py`. Same TFLite model, same preprocessing, two image sets.",
        "",
        f"- Model: `{model_path}`",
        f"- Lab: PlantVillage test images (`{lab_source}`)",
        f"- Field: farmer's photos (`{field_source}`)",
        f"- Run: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Metric | Lab (PlantVillage test) | Field (farm photos) | Gap (field − lab) |",
        "|---|---|---|---|",
        *[f"| {m} | {a} | {b} | {g} |" for m, a, b, g in rows],
        "",
        "A negative gap is how much worse the model does on real field photos than on the "
        "lab images it was trained to resemble. 'n/a' means not measurable: no images of that "
        "class, or no predictions of it, so there is no evidence either way.",
        "",
    ]
    return "\n".join(lines)


def _relative(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lab vs field evaluation of the TFLite CNN.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--lab", type=Path, default=DEFAULT_LAB,
                        help="test split CSV (default) or a folder of <class>/ subfolders")
    parser.add_argument("--field", type=Path, default=DEFAULT_FIELD)
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    for required in (args.model, args.labels):
        if not required.exists():
            parser.error(f"{required} not found. Unzip the Colab outputs into models/ first.")
    if not args.lab.exists():
        parser.error(f"lab set {args.lab} not found")
    if not args.field.is_dir():
        # Not fatal (the lab column is still useful), but never a silent "0 photos".
        print(f"WARNING: field folder {args.field} not found; the field column will be empty.")

    classes = read_labels(args.labels)
    predict = TFLiteClassifier(args.model)
    lab_set = lab_items(args.lab, classes)
    field_set = items_from_folder(args.field, classes)
    print(f"Lab images: {len(lab_set)}   field images: {len(field_set)}")

    lab = evaluate(lab_set, predict, classes)
    field = evaluate(field_set, predict, classes)
    rows = comparison_rows(lab, field, classes)

    width = max(len(m) for m, *_ in rows)
    print(f"\n{'Metric':{width}}  {'Lab':>8}  {'Field':>8}  {'Gap':>8}")
    for metric, a, b, gap in rows:
        print(f"{metric:{width}}  {a:>8}  {b:>8}  {gap:>8}")
    if not field_set:
        print("\nNo field photos yet: add them under data/field_images/<class>/ and run again.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_markdown(rows, _relative(args.lab), _relative(args.field), _relative(args.model)),
        encoding="utf-8",
    )
    args.out.with_suffix(".json").write_text(
        json.dumps({"classes": classes, "lab": lab, "field": field}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {_relative(args.out)} and {_relative(args.out.with_suffix('.json'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
