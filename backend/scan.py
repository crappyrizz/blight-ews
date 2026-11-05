"""A leaf scan: 1-3 photos of the same leaf, judged together as one result.

Like an identity check that asks for several selfies, the photos are not
merged into one image (the CNN only knows single, whole leaves). Each photo
passes the quality check and goes through the CNN on its own; their class
probabilities are then averaged into one prediction, which the fusion rule
(backend/fusion.py) treats exactly like a single photo's. One odd photo
(glare, a shadow, a bad angle) therefore moves the result only a third of
the way, and when the photos do not agree on what the leaf shows, the
result says so instead of hiding it.

The app asks for: the whole leaf (top side), a close-up of the worst spot,
and the same leaf from another angle or light. Undersides are not scored:
the model never saw one.
"""
from dataclasses import dataclass

from backend.cnn_service import Prediction

MAX_PHOTOS = 3

# The CNN's classes in the farmer's words (the app uses the same names).
_NAMES = {"late_blight": "late blight", "early_blight": "early blight", "healthy": "a healthy leaf"}


@dataclass(frozen=True)
class ScanPrediction:
    label: str  # the most probable class after averaging
    confidence: float  # its averaged probability
    probabilities: dict[str, float]  # every class, averaged over the photos
    photo_labels: list[str]  # each photo's own most probable class, in upload order
    photos_agree: bool  # every photo's most probable class is the same

    @property
    def photo_count(self) -> int:
        return len(self.photo_labels)


def combine(predictions: list[Prediction]) -> ScanPrediction:
    """Average the photos' class probabilities (one photo: unchanged)."""
    if not predictions:
        raise ValueError("a scan needs at least one photo")
    classes = list(predictions[0].probabilities)
    averaged = {c: sum(p.probabilities[c] for p in predictions) / len(predictions) for c in classes}
    label = max(classes, key=averaged.__getitem__)  # ties: the first class, as numpy's argmax does
    labels = [p.label for p in predictions]
    return ScanPrediction(label=label, confidence=averaged[label], probabilities=averaged,
                          photo_labels=labels, photos_agree=len(set(labels)) == 1)


def disagreement_note(scan: ScanPrediction) -> str | None:
    """One sentence for the farmer when the photos disagree; None when they agree."""
    if scan.photos_agree:
        return None
    counts = {label: scan.photo_labels.count(label) for label in dict.fromkeys(scan.photo_labels)}
    parts = [f"{n} looked like {_NAMES.get(label, label.replace('_', ' '))}" for label, n in counts.items()]
    return (f"The photos did not all show the same thing ({', '.join(parts)}), so this result is "
            "less certain: it may be worth looking closely at the plant, or taking the photos again.")
