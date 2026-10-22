"""Tests for tools/fetch_models.py: checksums decide what is downloaded and what is accepted."""
import hashlib

import httpx
import pytest

from tools import fetch_models as fm

CONTENT = b"pretend model bytes"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def client_serving(files: dict[str, bytes]):
    """An httpx client whose 'release' serves these files (404 for anything else)."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=files[name]) if name in files else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_the_manifest_lists_the_three_model_files():
    assert set(fm.read_manifest()) == {"cnn.tflite", "risk_model_24h.joblib", "risk_model_48h.joblib"}
    assert all(len(d) == 64 for d in fm.read_manifest().values())


def test_manifest_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "m.sha256"
    path.write_text(f"# a comment\n\n{DIGEST.upper()}  model.bin\n", encoding="utf-8")
    assert fm.read_manifest(path) == {"model.bin": DIGEST}


def test_a_missing_file_is_downloaded_and_checked(tmp_path):
    client = client_serving({"model.bin": CONTENT})
    assert fm.fetch("model.bin", DIGEST, "https://example.test/rel", tmp_path, client).startswith("downloaded")
    assert (tmp_path / "model.bin").read_bytes() == CONTENT


def test_a_file_with_the_right_checksum_is_not_downloaded_again(tmp_path):
    (tmp_path / "model.bin").write_bytes(CONTENT)
    client = client_serving({})
    assert fm.fetch("model.bin", DIGEST, "https://example.test/rel", tmp_path, client) == "already present"
    assert client.calls == []


def test_a_download_with_the_wrong_checksum_is_refused_and_not_saved(tmp_path):
    client = client_serving({"model.bin": b"something else"})
    with pytest.raises(ValueError, match="does not match"):
        fm.fetch("model.bin", DIGEST, "https://example.test/rel", tmp_path, client)
    assert not (tmp_path / "model.bin").exists()


def test_a_stale_local_file_is_replaced(tmp_path):
    (tmp_path / "model.bin").write_bytes(b"old model")
    fm.fetch("model.bin", DIGEST, "https://example.test/rel", tmp_path, client_serving({"model.bin": CONTENT}))
    assert (tmp_path / "model.bin").read_bytes() == CONTENT


def test_a_missing_release_asset_is_an_error(tmp_path):
    with pytest.raises(httpx.HTTPStatusError):
        fm.fetch("model.bin", DIGEST, "https://example.test/rel", tmp_path, client_serving({}))
