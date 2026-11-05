"""Copy the app's release signing key into the GitHub repository secrets, never shown.

    .\\.venv\\Scripts\\python.exe -m tools.set_app_signing_secrets

The "app release" workflow (.github/workflows/app-release.yml) signs every APK
it builds with this key, so phones can install each new build over the last
one. It reads app/android/key.properties (gitignored) and the keystore it
points at, and sets three repository secrets through the GitHub CLI (`gh`,
logged in as the repo owner):

    ANDROID_KEYSTORE_BASE64    the keystore file
    ANDROID_KEYSTORE_PASSWORD  its password (the key's too)
    ANDROID_KEY_ALIAS          the key's name in it

Only the secret names are printed. Run it again after replacing the key; a
secret that already exists is overwritten.

Keep a copy of the keystore and key.properties somewhere safe (a USB stick, a
password manager): GitHub secrets cannot be read back, and an app signed with
a lost key can never be updated, only uninstalled and installed afresh.
"""
import base64
import subprocess
import sys
from pathlib import Path

from backend.config import REPO_ROOT

KEY_PROPERTIES = REPO_ROOT / "app" / "android" / "key.properties"
REPO = "crappyrizz/blight-ews"


def read_key_properties(path: Path) -> dict[str, str]:
    """key.properties as a dict ("name=value" lines; # comments and blank lines skipped)."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def secrets_from(properties: dict[str, str]) -> dict[str, str]:
    """The repository secrets the workflow needs, from key.properties and its keystore."""
    missing = {"storeFile", "storePassword", "keyAlias"} - properties.keys()
    if missing:
        raise ValueError(f"key.properties lacks {', '.join(sorted(missing))}")
    if properties.get("keyPassword", properties["storePassword"]) != properties["storePassword"]:
        raise ValueError("the workflow assumes one password for the keystore and the key (PKCS12)")
    keystore = Path(properties["storeFile"])
    if not keystore.is_file():
        raise ValueError(f"the keystore {keystore} named in key.properties does not exist")
    return {
        "ANDROID_KEYSTORE_BASE64": base64.b64encode(keystore.read_bytes()).decode("ascii"),
        "ANDROID_KEYSTORE_PASSWORD": properties["storePassword"],
        "ANDROID_KEY_ALIAS": properties["keyAlias"],
    }


def main() -> int:
    if not KEY_PROPERTIES.exists():
        print(f"No {KEY_PROPERTIES}: there is no release key on this PC.")
        return 1
    try:
        values = secrets_from(read_key_properties(KEY_PROPERTIES))
    except ValueError as error:
        print(f"Not set: {error}.")
        return 1
    for name, value in values.items():
        # The value goes in on stdin, so it never appears in a command line or the console.
        result = subprocess.run(["gh", "secret", "set", name, "--repo", REPO], input=value, text=True,
                                capture_output=True)
        if result.returncode != 0:
            print(f"Could not set {name}: {result.stderr.strip()}")
            return 1
        print(f"GitHub secret set: {name}")
    print("Done. The next push to main that touches app/ builds a signed APK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
