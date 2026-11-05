"""Tests for tools/set_app_signing_secrets.py: reading key.properties into the GitHub secrets. No gh calls."""
import base64

import pytest

from tools import set_app_signing_secrets as tool


def write_key(tmp_path, **overrides):
    keystore = tmp_path / "upload.jks"
    keystore.write_bytes(b"\x30\x82keystore-bytes")
    props = {"storePassword": "pw", "keyPassword": "pw", "keyAlias": "upload", "storeFile": keystore.as_posix()}
    props.update(overrides)
    path = tmp_path / "key.properties"
    path.write_text("# made by hand\n\n" + "".join(f"{k}={v}\n" for k, v in props.items()), encoding="utf-8")
    return path, keystore


def test_the_three_secrets_come_from_key_properties_and_the_keystore(tmp_path):
    path, keystore = write_key(tmp_path)
    secrets = tool.secrets_from(tool.read_key_properties(path))
    assert secrets == {
        "ANDROID_KEYSTORE_BASE64": base64.b64encode(keystore.read_bytes()).decode(),
        "ANDROID_KEYSTORE_PASSWORD": "pw",
        "ANDROID_KEY_ALIAS": "upload",
    }


def test_a_value_may_contain_equals_signs(tmp_path):
    path, _ = write_key(tmp_path, storePassword="a=b", keyPassword="a=b")
    assert tool.secrets_from(tool.read_key_properties(path))["ANDROID_KEYSTORE_PASSWORD"] == "a=b"


@pytest.mark.parametrize("overrides, reason", [
    ({"storeFile": "C:/nowhere/missing.jks"}, "does not exist"),
    ({"keyPassword": "other"}, "one password"),
])
def test_a_key_the_workflow_cannot_use_is_refused(tmp_path, overrides, reason):
    path, _ = write_key(tmp_path, **overrides)
    with pytest.raises(ValueError, match=reason):
        tool.secrets_from(tool.read_key_properties(path))


def test_missing_entries_are_named():
    with pytest.raises(ValueError, match="keyAlias, storeFile"):
        tool.secrets_from({"storePassword": "pw"})
