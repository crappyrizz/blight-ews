"""Tests for backend/scan.py and POST /scans: several photos of one leaf, one result.

The model is faked (FakeInterpreter from test_cnn_service), answering a
different set of probabilities for each photo, in upload order.
"""
import numpy as np
import pytest
from PIL import Image
from test_cnn_service import LABELS, FakeInterpreter, add_node, encode, leaf_like_bytes, token_for

from backend import app as app_module
from backend import cnn_service, scan
from backend.app import app
from backend.cnn_service import Prediction
from backend.models import DiagnosisResult, LeafImage

LATE = (0.10, 0.10, 0.80)  # early_blight, healthy, late_blight
HEALTHY = (0.10, 0.80, 0.10)
EARLY = (0.70, 0.20, 0.10)


def prediction(probabilities):
    probs = dict(zip(LABELS, probabilities, strict=True))
    label = max(probs, key=probs.__getitem__)
    return Prediction(label=label, confidence=probs[label], probabilities=probs)


# --- combining ------------------------------------------------------------------------


def test_one_photo_is_unchanged():
    combined = scan.combine([prediction(LATE)])
    assert combined.label == "late_blight" and combined.confidence == pytest.approx(0.8)
    assert combined.photos_agree and combined.photo_count == 1
    assert scan.disagreement_note(combined) is None


def test_the_photos_are_averaged():
    combined = scan.combine([prediction(LATE), prediction(HEALTHY), prediction(LATE)])
    assert combined.probabilities == pytest.approx({"early_blight": 0.1, "healthy": 1.0 / 3, "late_blight": 1.7 / 3})
    assert combined.label == "late_blight"
    assert combined.photo_labels == ["late_blight", "healthy", "late_blight"]


def test_one_odd_photo_cannot_decide_alone():
    # Two healthy-looking photos outweigh one that looks like late blight.
    combined = scan.combine([prediction(HEALTHY), prediction(LATE), prediction(HEALTHY)])
    assert combined.label == "healthy"


def test_disagreement_is_said_in_words():
    combined = scan.combine([prediction(LATE), prediction(HEALTHY), prediction(LATE)])
    assert not combined.photos_agree
    note = scan.disagreement_note(combined)
    assert "2 looked like late blight, 1 looked like a healthy leaf" in note
    assert "less certain" in note


def test_a_scan_needs_a_photo():
    with pytest.raises(ValueError):
        scan.combine([])


# --- POST /scans -----------------------------------------------------------------------


class SequenceInterpreter(FakeInterpreter):
    """Answers the given probabilities in turn, one set per photo."""

    def __init__(self, *answers):
        super().__init__(answers[0])
        self.answers = [np.array([a], dtype=np.float32) for a in answers]
        self.calls = 0

    def get_tensor(self, index):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer


@pytest.fixture
def model_answering(client, tmp_path, monkeypatch):
    """model_answering(LATE, HEALTHY, ...) installs a fake model answering those, in order."""
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path)

    def install(*answers):
        interpreter = SequenceInterpreter(*answers)
        app.dependency_overrides[cnn_service.get_classifier] = lambda: cnn_service.CNNClassifier(interpreter, LABELS)
        return interpreter
    return install


def send_scan(client, headers, photos, **form):
    files = [("files", (f"leaf{i}.jpg", data, "image/jpeg")) for i, data in enumerate(photos)]
    return client.post("/scans", headers=headers, files=files, data=form)


def three_photos():
    return [leaf_like_bytes(seed=s) for s in (1, 2, 3)]


def test_three_photos_make_one_diagnosis(client, db_session, model_answering, tmp_path):
    model_answering(LATE, LATE, LATE)
    response = send_scan(client, token_for(client), three_photos())

    assert response.status_code == 201
    body = response.json()
    assert body["photos_agree"] is True
    assert [p["label"] for p in body["photos"]] == ["late_blight"] * 3
    assert body["prediction"]["confidence"] == pytest.approx(0.8)

    first_id = body["image"]["image_id"]
    rows = db_session.query(LeafImage).order_by(LeafImage.first_image_id.nulls_first()).all()
    assert len(rows) == 3 and len(list(tmp_path.rglob("*.jpg"))) == 3
    assert rows[0].image_id == first_id and rows[0].first_image_id is None
    assert {r.first_image_id for r in rows[1:]} == {first_id}  # the others point to the first
    assert all(r.probabilities["late_blight"] == pytest.approx(0.8) for r in rows)

    [diagnosis] = db_session.query(DiagnosisResult).all()  # one result for the leaf
    assert diagnosis.image_id == first_id


def test_photos_that_disagree_are_averaged_and_flagged_in_the_summary(client, db_session, model_answering):
    model_answering(LATE, HEALTHY, LATE)
    body = send_scan(client, token_for(client), three_photos()).json()

    assert body["photos_agree"] is False
    assert [p["label"] for p in body["photos"]] == ["late_blight", "healthy", "late_blight"]
    assert body["prediction"]["probabilities"]["late_blight"] == pytest.approx(1.7 / 3)
    assert body["diagnosis"]["label"] == "late_blight"
    assert "2 looked like late blight, 1 looked like a healthy leaf" in body["diagnosis"]["summary"]
    stored = db_session.get(DiagnosisResult, body["diagnosis"]["result_id"])
    assert stored.probabilities["late_blight"] == pytest.approx(1.7 / 3)  # the combined result is kept


def test_one_bad_photo_names_it_and_nothing_is_stored(client, db_session, model_answering, tmp_path):
    interpreter = model_answering(LATE)
    dark = encode(Image.new("RGB", (400, 400), (0, 0, 0)))
    response = send_scan(client, token_for(client), [leaf_like_bytes(), dark, leaf_like_bytes(seed=5)])

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "too_dark"
    assert detail["photos"] == [{"index": 1, "reason": "too_dark", "message": detail["photos"][0]["message"]}]
    assert detail["message"].startswith("Photo 2: ")
    assert db_session.query(LeafImage).count() == 0 and not any(tmp_path.rglob("*.*"))
    assert interpreter.calls == 0  # the model never judged a scan with a bad photo


def test_every_bad_photo_is_listed_at_once(client, model_answering):
    model_answering(LATE)
    tiny = leaf_like_bytes(size=(120, 120))
    detail = send_scan(client, token_for(client), [tiny, leaf_like_bytes(), b"not a photo"]).json()["detail"]
    assert [(p["index"], p["reason"]) for p in detail["photos"]] == [(0, "too_small"), (2, "unreadable")]


@pytest.mark.parametrize("count", [4, 5])
def test_more_than_three_photos_are_refused(client, model_answering, count):
    model_answering(LATE)
    response = send_scan(client, token_for(client), [leaf_like_bytes(seed=s) for s in range(count)])
    assert response.status_code == 422
    assert "1 to 3 photos" in response.json()["detail"]


def test_a_scan_with_no_photo_is_refused(client, model_answering):
    model_answering(LATE)
    assert client.post("/scans", headers=token_for(client), data={}).status_code == 422


def test_history_says_how_many_photos_a_check_used(client, db_session, model_answering):
    model_answering(LATE)
    headers = token_for(client)
    node = add_node(db_session, client.get("/me", headers=headers).json()["farmer_id"])
    send_scan(client, headers, three_photos(), node_id=node.node_id)
    send_scan(client, headers, [leaf_like_bytes()], node_id=node.node_id)

    diagnoses = client.get(f"/nodes/{node.node_id}/history?days=3", headers=headers).json()["diagnoses"]
    assert sorted(d["photo_count"] for d in diagnoses) == [1, 3]
