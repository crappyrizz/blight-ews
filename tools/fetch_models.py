"""Download the trained model files from the project's GitHub release.

The models are gitignored (they are build outputs: ml/train_risk.py and the
Colab notebook make them). A server that deploys from git, such as Render
(render.yaml), runs this in its build step to get them:

    .\\.venv\\Scripts\\python.exe -m tools.fetch_models

Every file is checked against models/models.sha256, which IS committed, so
the server only ever loads the exact models that were evaluated; a wrong or
corrupted download fails the build instead of serving a different model.
A file already present with the right checksum is not downloaded again.

To publish new models: retrain, upload the files to a new release, put its
tag in RELEASE_TAG and the new checksums in models/models.sha256.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

import httpx

from backend.config import REPO_ROOT

MODELS_DIR = REPO_ROOT / "models"
MANIFEST = MODELS_DIR / "models.sha256"
RELEASE_TAG = "models-2026-10-12"
DEFAULT_BASE_URL = f"https://github.com/crappyrizz/blight-ews/releases/download/{RELEASE_TAG}"


def read_manifest(path: Path = MANIFEST) -> dict[str, str]:
    """{file name: expected sha256} from 'sha256  name' lines (# comments allowed)."""
    expected = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, name = line.split(maxsplit=1)
            expected[name.strip()] = digest.lower()
    return expected


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(name: str, digest: str, base_url: str, target_dir: Path, client) -> str:
    """Make sure target_dir/name exists with the right checksum. Returns what was done."""
    target = target_dir / name
    if target.exists() and sha256_of(target) == digest:
        return "already present"
    response = client.get(f"{base_url}/{name}")
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != digest:
        raise ValueError(f"{name}: downloaded file does not match models/models.sha256")
    target.write_bytes(response.content)
    return f"downloaded ({len(response.content):,} bytes)"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download the model files from the GitHub release.")
    parser.add_argument("--base-url", default=os.environ.get("MODELS_BASE_URL", DEFAULT_BASE_URL))
    args = parser.parse_args(argv)

    MODELS_DIR.mkdir(exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for name, digest in read_manifest().items():
            try:
                print(f"{name}: {fetch(name, digest, args.base_url, MODELS_DIR, client)}")
            except (httpx.HTTPError, ValueError) as error:
                print(f"{name}: FAILED: {error}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
