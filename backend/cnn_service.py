"""Server-side leaf classification with the TFLite CNN (FR iv and v).

The phone only takes the photo; the model runs here, so the app stays small
and every diagnosis uses the same model version.

Three steps for each upload:

1. load_image():    decode the bytes into an upright RGB image, or reject them.
2. check_quality(): reject photos the model cannot judge fairly (too small,
                    too dark, too blurry), with a reason the app can show so
                    the farmer can retake the photo.
3. preprocess() and CNNClassifier.predict(): the training preprocessing,
                    then the TFLite model.

Preprocessing mirrors ml/train_cnn.ipynb: RGB, 224x224 bilinear resize with
antialiasing, then MobileNetV2's preprocess_input (x / 127.5 - 1). The resize
is done in floating point, which matches TensorFlow's
tf.image.resize(antialias=True) to within 0.004 of a grey level (measured on
PlantVillage images and on a 4000x3000 photo). One difference cannot be
removed without TensorFlow: training decoded JPEGs with TensorFlow's default
fast integer IDCT, and Pillow uses the accurate one, so decoded pixels can
differ by up to 4 grey levels out of 255 (about 1 on average). That is far
smaller than the +-20 % brightness augmentation the model was trained with.

Interpreter: ai-edge-litert (Google's standalone TFLite runtime, "LiteRT"),
which has a Windows wheel for Python 3.11 at about 21 MB. tensorflow-cpu's
tf.lite.Interpreter is the fallback if LiteRT is not installed; it works too
but is a ~330 MB install and is deprecated in TensorFlow 2.20.
"""
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from fastapi import HTTPException, status
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.config import REPO_ROOT

log = logging.getLogger("cnn_service")

MODEL_PATH = REPO_ROOT / "models" / "cnn.tflite"
LABELS_PATH = REPO_ROOT / "models" / "labels.txt"

IMAGE_SIZE = 224  # the model's input is IMAGE_SIZE x IMAGE_SIZE x 3

# --- quality-check thresholds -------------------------------------------
# Measured on what the model sees: the photo as 224x224 greyscale, 0-255.
# Calibrated on the 2,152 PlantVillage images, none of which is rejected:
#   sharpness (variance of the Laplacian): lowest 156, median 1,381; the same
#     images blurred with a 2-pixel Gaussian score at most 19.7.
#   brightness (mean grey level): lowest 83, median 128; the same images at
#     1/8 exposure average at most 20.
# Field photos are busier than PlantVillage's plain backgrounds, so they
# should sit higher still. Re-check these once real field photos exist.
MIN_SHORT_SIDE_PX = 224  # smaller and the model would be looking at an upscaled guess
DARK_MEAN_THRESHOLD = 40.0  # mean grey level below this: too dark
BLUR_LAPLACIAN_VAR_THRESHOLD = 50.0  # variance of the Laplacian below this: too blurry


# --- rejections the app can act on --------------------------------------


class ImageRejected(Exception):
    """A photo the model should not judge. `reason` is a stable code for the app."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


UNREADABLE = "unreadable"
TOO_SMALL = "too_small"
TOO_DARK = "too_dark"
TOO_BLURRY = "too_blurry"


def load_image(image_bytes: bytes) -> Image.Image:
    """Decode an upload into an upright RGB image, or raise ImageRejected.

    Phones store photos sideways and record the rotation in EXIF; applying it
    gives the leaf as the farmer saw it. (PlantVillage images carry no EXIF
    rotation, so this changes nothing for images like the training set.)
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()  # force a full decode now, so a truncated file fails here
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ImageRejected(
            UNREADABLE,
            "The photo could not be read. Please send it again as a JPEG or PNG.",
        ) from error


def image_format(image_bytes: bytes) -> str | None:
    """'JPEG', 'PNG', ... from the file's own header (not its name), or None."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return image.format
    except (UnidentifiedImageError, OSError):
        return None


def _model_grey(image: Image.Image) -> np.ndarray:
    """The photo as the model will see it, in greyscale (224x224, 0-255)."""
    return np.asarray(
        image.convert("L").resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )


def laplacian_variance(grey: np.ndarray) -> float:
    """Sharpness: how strongly brightness changes between neighbouring pixels.

    The Laplacian (4-neighbour kernel) responds to edges and fine texture; a
    blurred photo has little of either, so the variance of the response is low.
    """
    laplacian = (
        grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:]
        - 4.0 * grey[1:-1, 1:-1]
    )
    return float(laplacian.var())


def check_quality(image: Image.Image) -> dict:
    """Raise ImageRejected if the model should not judge this photo.

    Returns the measured values for a photo that passes, so they can be
    logged or shown.
    """
    width, height = image.size
    if min(width, height) < MIN_SHORT_SIDE_PX:
        raise ImageRejected(
            TOO_SMALL,
            f"The photo is too small ({width}x{height}). Please take it again at "
            f"full resolution, at least {MIN_SHORT_SIDE_PX} pixels on the short side.",
        )

    grey = _model_grey(image)
    brightness = float(grey.mean())
    if brightness < DARK_MEAN_THRESHOLD:
        raise ImageRejected(
            TOO_DARK,
            "The photo is too dark to judge. Please retake it in daylight, "
            "without shading the leaf.",
        )

    sharpness = laplacian_variance(grey)
    if sharpness < BLUR_LAPLACIAN_VAR_THRESHOLD:
        raise ImageRejected(
            TOO_BLURRY,
            "The photo is blurry. Please hold the phone still and tap the leaf to "
            "focus, then retake it.",
        )

    return {"brightness": brightness, "sharpness": sharpness}


# --- preprocessing --------------------------------------------------------


def centre_square(image: Image.Image) -> Image.Image:
    """The largest centred square of the photo.

    The CNN was trained on square images (PlantVillage, 256x256). Squashing a
    rectangular phone photo straight to 224x224 distorts the leaf: a tall
    1600x739 photo was squeezed to less than half its width, and a clear
    early-blight leaf then came out as 100 % late blight (the same photo,
    centre-cropped, was 100 % early blight). Cropping first keeps the
    leaf's real proportions, as in training. For a square image (every
    training image) the crop changes nothing, so training and serving still
    match exactly.
    """
    width, height = image.size
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def to_model_input(image: Image.Image) -> np.ndarray:
    """RGB image -> float32 [1, 224, 224, 3] in -1..1, as in training.

    Centre square crop (see centre_square), then each channel is resized in
    floating point ('F' mode), because Pillow's 8-bit resize rounds between
    its two passes and would drift from TensorFlow's by about 0.3 grey
    levels on average.
    """
    rgb = np.asarray(centre_square(image.convert("RGB")), dtype=np.float32)
    channels = [
        np.asarray(
            Image.fromarray(rgb[..., c], mode="F").resize(
                (IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR
            ),
            dtype=np.float32,
        )
        for c in range(3)
    ]
    resized = np.stack(channels, axis=-1)
    return (resized / 127.5 - 1.0)[np.newaxis]  # mobilenet_v2.preprocess_input


def preprocess(image_bytes: bytes) -> np.ndarray:
    """Bytes of an image file -> the model's input array (no quality check)."""
    return to_model_input(load_image(image_bytes))


# --- the model ------------------------------------------------------------


def make_interpreter(model_path: Path):
    """A TFLite interpreter: LiteRT if installed, else TensorFlow's."""
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        import tensorflow as tf

        Interpreter = tf.lite.Interpreter
    return Interpreter(model_path=str(model_path))


@dataclass
class Prediction:
    label: str  # the most probable class
    confidence: float  # its probability
    probabilities: dict[str, float]  # every class, in labels.txt order


class CNNClassifier:
    """The loaded TFLite model plus its labels. Load once, reuse per request."""

    def __init__(self, interpreter, labels: list[str]):
        self.labels = labels
        self.interpreter = interpreter
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]

        # Fail at startup, not on the first farmer's photo, if the files disagree.
        expected_input = [1, IMAGE_SIZE, IMAGE_SIZE, 3]
        if list(self.input["shape"]) != expected_input:
            raise ValueError(f"model input is {list(self.input['shape'])}, expected {expected_input}")
        if int(self.output["shape"][-1]) != len(labels):
            raise ValueError(
                f"model has {int(self.output['shape'][-1])} outputs but labels.txt has {len(labels)}"
            )

    @classmethod
    def from_files(cls, model_path: Path = MODEL_PATH, labels_path: Path = LABELS_PATH):
        labels = [
            line.strip()
            for line in labels_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return cls(make_interpreter(model_path), labels)

    def predict(self, model_input: np.ndarray) -> Prediction:
        """Class probabilities for one preprocessed image."""
        self.interpreter.set_tensor(self.input["index"], model_input.astype(self.input["dtype"]))
        self.interpreter.invoke()
        probabilities = np.asarray(self.interpreter.get_tensor(self.output["index"])[0], dtype=float)
        best = int(np.argmax(probabilities))
        return Prediction(
            label=self.labels[best],
            confidence=float(probabilities[best]),
            probabilities={label: float(p) for label, p in zip(self.labels, probabilities, strict=True)},
        )


# --- loaded once, shared by every request ---------------------------------

_classifier: CNNClassifier | None = None


def load_classifier(model_path: Path = MODEL_PATH, labels_path: Path = LABELS_PATH) -> bool:
    """Load the model at startup. Returns False (and logs why) if it is missing.

    The rest of the API keeps working without the model; only image upload
    answers 503 until cnn.tflite and labels.txt are in models/.
    """
    global _classifier
    if not model_path.exists() or not labels_path.exists():
        log.warning("CNN not loaded: %s or %s is missing", model_path, labels_path)
        _classifier = None
        return False
    _classifier = CNNClassifier.from_files(model_path, labels_path)
    log.info("CNN loaded: %s, classes %s", model_path.name, _classifier.labels)
    return True


def get_classifier() -> CNNClassifier:
    """FastAPI dependency: the loaded model, or 503 if there is none."""
    if _classifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The leaf-diagnosis model is not loaded on the server.",
        )
    return _classifier


# --- command line: classify image files with the real model ---------------


def main(argv=None) -> int:
    """Run photos through the same checks and model as POST /images.

        .\.venv\Scripts\python.exe -m backend.cnn_service path\to\leaf.jpg [more.jpg ...]
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Classify leaf photos with the TFLite CNN.")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--model", type=Path, default=MODEL_PATH)
    parser.add_argument("--labels", type=Path, default=LABELS_PATH)
    args = parser.parse_args(argv)

    if not load_classifier(args.model, args.labels):
        print(f"Model not found: put cnn.tflite and labels.txt in {args.model.parent}")
        return 1
    for path in args.images:
        try:
            image = load_image(path.read_bytes())
            quality = check_quality(image)
        except ImageRejected as rejected:
            print(json.dumps({"image": str(path), "rejected": rejected.reason, "message": rejected.message}))
            continue
        prediction = _classifier.predict(to_model_input(image))
        print(json.dumps({
            "image": str(path),
            "label": prediction.label,
            "confidence": round(prediction.confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in prediction.probabilities.items()},
            "quality": {k: round(v, 1) for k, v in quality.items()},
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
