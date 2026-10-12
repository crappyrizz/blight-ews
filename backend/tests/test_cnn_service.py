"""Tests for backend/cnn_service.py and the /images endpoints.

No real model is needed: FakeInterpreter stands in for the TFLite runtime,
with the same interface (input/output details, set_tensor, invoke, get_tensor).
"""
import io

import numpy as np
import pytest
from PIL import Image, ImageFilter

from backend import app as app_module
from backend import cnn_service
from backend.app import app
from backend.config import REPO_ROOT
from backend.models import LeafImage

LABELS = ["early_blight", "healthy", "late_blight"]
PHONE = "+254711222333"
OTHER_PHONE = "+254711222444"
PASSWORD = "correct-horse-battery"


class FakeInterpreter:
    """Behaves like a TFLite interpreter for a 224x224x3 -> 3-class model."""

    def __init__(self, probabilities=(0.1, 0.2, 0.7), input_shape=(1, 224, 224, 3), outputs=3):
        self.probabilities = np.array([probabilities], dtype=np.float32)
        self.input_shape = np.array(input_shape)
        self.outputs = outputs
        self.last_input = None

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": self.input_shape, "dtype": np.float32}]

    def get_output_details(self):
        return [{"index": 1, "shape": np.array([1, self.outputs]), "dtype": np.float32}]

    def set_tensor(self, index, value):
        self.last_input = value

    def invoke(self):
        pass

    def get_tensor(self, index):
        return self.probabilities


# --- test images -------------------------------------------------------------


def encode(image, fmt="JPEG", **options):
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def leaf_like(size=(320, 320), seed=0):
    """Sharp, mid-brightness texture: passes every quality check."""
    pixels = np.random.default_rng(seed).integers(40, 220, (size[1], size[0], 3), dtype=np.uint8)
    return Image.fromarray(pixels)


def leaf_like_bytes(**kwargs):
    return encode(leaf_like(**kwargs))


# --- preprocessing -------------------------------------------------------------


def test_preprocess_gives_the_model_input_shape_and_dtype():
    model_input = cnn_service.preprocess(leaf_like_bytes())
    assert model_input.shape == (1, 224, 224, 3)
    assert model_input.dtype == np.float32


def test_preprocess_scales_like_mobilenet_v2_preprocess_input():
    black = cnn_service.preprocess(encode(Image.new("RGB", (300, 300), (0, 0, 0)), "PNG"))
    white = cnn_service.preprocess(encode(Image.new("RGB", (300, 300), (255, 255, 255)), "PNG"))
    grey = cnn_service.preprocess(encode(Image.new("RGB", (300, 300), (51, 102, 204)), "PNG"))

    assert np.allclose(black, -1.0)
    assert np.allclose(white, 1.0)
    # x / 127.5 - 1, channel by channel, in RGB order
    assert np.allclose(grey[0, 100, 100], [51 / 127.5 - 1, 102 / 127.5 - 1, 204 / 127.5 - 1], atol=1e-5)


def test_preprocess_range_is_minus_one_to_one():
    model_input = cnn_service.preprocess(leaf_like_bytes(size=(1000, 750)))
    assert model_input.min() >= -1.0
    assert model_input.max() <= 1.0


@pytest.mark.parametrize("mode", ["L", "RGBA", "P"])
def test_preprocess_converts_any_mode_to_rgb(mode):
    image = leaf_like().convert(mode)
    model_input = cnn_service.preprocess(encode(image, "PNG"))
    assert model_input.shape == (1, 224, 224, 3)


def test_non_square_photo_is_resized_like_training_without_cropping():
    # A 400x200 image, left half black and right half white: squashed to
    # 224x224 (as tf.image.resize did in training), both halves survive.
    pixels = np.zeros((200, 400, 3), dtype=np.uint8)
    pixels[:, 200:] = 255
    model_input = cnn_service.preprocess(encode(Image.fromarray(pixels), "PNG"))[0]
    assert np.allclose(model_input[:, :100], -1.0)
    assert np.allclose(model_input[:, 124:], 1.0)


def test_phone_rotation_in_exif_is_applied():
    image = leaf_like(size=(400, 300))
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90 degrees clockwise to view
    loaded = cnn_service.load_image(encode(image, exif=exif.tobytes()))
    assert loaded.size == (300, 400)


# --- quality check -------------------------------------------------------------


def rejection(image_bytes):
    with pytest.raises(cnn_service.ImageRejected) as caught:
        cnn_service.check_quality(cnn_service.load_image(image_bytes))
    return caught.value


def test_a_sharp_well_lit_photo_passes():
    measured = cnn_service.check_quality(cnn_service.load_image(leaf_like_bytes()))
    assert measured["brightness"] > cnn_service.DARK_MEAN_THRESHOLD
    assert measured["sharpness"] > cnn_service.BLUR_LAPLACIAN_VAR_THRESHOLD


def test_tiny_image_is_rejected():
    error = rejection(leaf_like_bytes(size=(223, 400)))
    assert error.reason == cnn_service.TOO_SMALL
    assert "223x400" in error.message


def test_exactly_224_on_the_short_side_is_accepted():
    cnn_service.check_quality(cnn_service.load_image(leaf_like_bytes(size=(224, 500))))


def test_black_image_is_rejected_as_too_dark():
    error = rejection(encode(Image.new("RGB", (400, 400), (0, 0, 0))))
    assert error.reason == cnn_service.TOO_DARK


def test_blurry_image_is_rejected():
    blurred = leaf_like().filter(ImageFilter.GaussianBlur(6))
    assert rejection(encode(blurred)).reason == cnn_service.TOO_BLURRY


def test_flat_grey_image_is_rejected_as_blurry():
    assert rejection(encode(Image.new("RGB", (400, 400), (128, 128, 128)))).reason == cnn_service.TOO_BLURRY


@pytest.mark.parametrize("data", [b"", b"not an image at all", leaf_like_bytes()[:500]])
def test_unreadable_files_are_rejected(data):
    with pytest.raises(cnn_service.ImageRejected) as caught:
        cnn_service.load_image(data)
    assert caught.value.reason == cnn_service.UNREADABLE


def test_laplacian_variance_is_zero_for_a_flat_image():
    assert cnn_service.laplacian_variance(np.full((50, 50), 90.0, dtype=np.float32)) == 0.0


@pytest.mark.skipif(not (REPO_ROOT / "data" / "plantvillage_potato").exists(),
                    reason="PlantVillage images are local only (gitignored)")
def test_plantvillage_images_pass_the_quality_check():
    folder = REPO_ROOT / "data" / "plantvillage_potato"
    for path in [*sorted(folder.rglob("*.jpg"))[::50]]:
        cnn_service.check_quality(cnn_service.load_image(path.read_bytes()))


# --- the classifier --------------------------------------------------------------


def test_predict_returns_label_confidence_and_all_probabilities():
    classifier = cnn_service.CNNClassifier(FakeInterpreter((0.1, 0.2, 0.7)), LABELS)
    prediction = classifier.predict(cnn_service.preprocess(leaf_like_bytes()))

    assert prediction.label == "late_blight"
    assert prediction.confidence == pytest.approx(0.7)
    assert list(prediction.probabilities) == LABELS
    assert prediction.probabilities["healthy"] == pytest.approx(0.2)


def test_labels_and_model_outputs_must_agree():
    with pytest.raises(ValueError, match="outputs"):
        cnn_service.CNNClassifier(FakeInterpreter(outputs=4), LABELS)


def test_model_input_shape_is_checked():
    with pytest.raises(ValueError, match="input"):
        cnn_service.CNNClassifier(FakeInterpreter(input_shape=(1, 160, 160, 3)), LABELS)


def test_missing_model_files_leave_the_classifier_unloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(cnn_service, "_classifier", None)
    assert cnn_service.load_classifier(tmp_path / "cnn.tflite", tmp_path / "labels.txt") is False
    with pytest.raises(Exception) as caught:
        cnn_service.get_classifier()
    assert caught.value.status_code == 503


# --- the endpoints ------------------------------------------------------------------


def token_for(client, phone=PHONE):
    client.post("/auth/register", json={"name": "Leaf Farmer", "phone_number": phone, "password": PASSWORD})
    response = client.post("/auth/login", json={"phone_number": phone, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def fake_model(client, tmp_path, monkeypatch):
    """A fake classifier and a temporary upload folder for the endpoint tests."""
    interpreter = FakeInterpreter((0.15, 0.05, 0.80))
    classifier = cnn_service.CNNClassifier(interpreter, LABELS)
    app.dependency_overrides[cnn_service.get_classifier] = lambda: classifier
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path)
    return interpreter


def upload(client, headers, data, filename="leaf.jpg", **form):
    return client.post("/images", headers=headers, files={"file": (filename, data, "image/jpeg")}, data=form)


def test_upload_stores_the_image_and_returns_the_prediction(client, db_session, fake_model, tmp_path):
    headers = token_for(client)
    data = leaf_like_bytes()

    response = upload(client, headers, data)

    assert response.status_code == 201
    body = response.json()
    assert body["prediction"]["label"] == "late_blight"
    assert body["prediction"]["confidence"] == pytest.approx(0.8)
    assert body["prediction"]["probabilities"] == pytest.approx(
        {"early_blight": 0.15, "healthy": 0.05, "late_blight": 0.8})

    image_id = body["image"]["image_id"]
    farmer_id = body["image"]["farmer_id"]
    assert image_id.startswith("IMG-")
    stored = tmp_path / farmer_id / f"{image_id}.jpg"
    assert stored.read_bytes() == data  # the original, untouched

    row = db_session.get(LeafImage, image_id)
    assert row.farmer_id == farmer_id
    assert row.file_path.endswith(f"{farmer_id}/{image_id}.jpg")


def test_the_model_receives_the_preprocessed_image(client, fake_model):
    upload(client, token_for(client), leaf_like_bytes(size=(800, 600)))

    assert fake_model.last_input.shape == (1, 224, 224, 3)
    assert -1.0 <= fake_model.last_input.min() and fake_model.last_input.max() <= 1.0


def test_capture_date_is_kept_when_given(client, fake_model):
    response = upload(client, token_for(client), leaf_like_bytes(), capture_date="2026-10-10T08:15:00+03:00")
    assert response.json()["image"]["capture_date"].startswith("2026-10-10T05:15:00")


def test_capture_date_without_timezone_is_refused(client, fake_model):
    response = upload(client, token_for(client), leaf_like_bytes(), capture_date="2026-10-10T08:15:00")
    assert response.status_code == 422


@pytest.mark.parametrize("data, reason", [
    (encode(Image.new("RGB", (400, 400), (0, 0, 0))), "too_dark"),
    (leaf_like_bytes(size=(120, 120)), "too_small"),
    (b"definitely not a photo", "unreadable"),
])
def test_a_bad_photo_gets_a_reason_and_nothing_is_stored(client, db_session, fake_model, tmp_path, data, reason):
    response = upload(client, token_for(client), data)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == reason
    assert response.json()["detail"]["message"]  # something the app can show
    assert db_session.query(LeafImage).count() == 0
    assert not any(tmp_path.rglob("*.*"))
    assert fake_model.last_input is None  # the model was never asked


def test_oversized_upload_is_refused(client, fake_model, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 1000)
    assert upload(client, token_for(client), leaf_like_bytes()).status_code == 413


def test_upload_needs_a_farmer_token(client, fake_model):
    assert upload(client, {}, leaf_like_bytes()).status_code == 401


def test_upload_without_a_loaded_model_is_503(client, monkeypatch, tmp_path):
    monkeypatch.setattr(cnn_service, "_classifier", None)
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path)
    assert upload(client, token_for(client), leaf_like_bytes()).status_code == 503


def test_farmer_can_read_their_own_image(client, fake_model):
    headers = token_for(client)
    image_id = upload(client, headers, leaf_like_bytes()).json()["image"]["image_id"]

    response = client.get(f"/images/{image_id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["image_id"] == image_id


def test_another_farmers_image_is_404_not_403(client, fake_model):
    image_id = upload(client, token_for(client), leaf_like_bytes()).json()["image"]["image_id"]

    response = client.get(f"/images/{image_id}", headers=token_for(client, OTHER_PHONE))

    assert response.status_code == 404


def test_unknown_image_is_404(client, fake_model):
    assert client.get("/images/IMG-DOESNOTEXIST", headers=token_for(client)).status_code == 404


# --- command line ----------------------------------------------------------------


def test_command_line_classifies_and_reports_rejections(tmp_path, monkeypatch, capsys):
    good = tmp_path / "leaf.jpg"
    good.write_bytes(leaf_like_bytes())
    dark = tmp_path / "dark.jpg"
    dark.write_bytes(encode(Image.new("RGB", (400, 400), (0, 0, 0))))
    (tmp_path / "cnn.tflite").write_bytes(b"fake")
    (tmp_path / "labels.txt").write_text("\n".join(LABELS), encoding="utf-8")
    monkeypatch.setattr(cnn_service, "make_interpreter", lambda path: FakeInterpreter())
    monkeypatch.setattr(cnn_service, "_classifier", None)

    code = cnn_service.main([str(good), str(dark), "--model", str(tmp_path / "cnn.tflite"),
                             "--labels", str(tmp_path / "labels.txt")])

    out = capsys.readouterr().out
    assert code == 0
    assert '"label": "late_blight"' in out
    assert '"rejected": "too_dark"' in out


def test_command_line_without_a_model_says_where_to_put_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cnn_service, "_classifier", None)
    assert cnn_service.main(["x.jpg", "--model", str(tmp_path / "cnn.tflite"),
                             "--labels", str(tmp_path / "labels.txt")]) == 1
    assert "Model not found" in capsys.readouterr().out
