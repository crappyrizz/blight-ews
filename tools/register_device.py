"""Register a sensor device (ESP32 node) and issue its API key.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m tools.register_device node-01 "Bench node A"
    .\\dev.ps1 register-device node-01 "Bench node A"

- A random 32-byte key is generated; only its bcrypt hash is stored.
- The raw key is printed ONCE. It cannot be recovered later, only replaced.
- A device_id that already exists is refused; nothing is changed.

A device is a row in sensor_nodes (device_id = node_id, name = location), so
its readings reach the risk layer like any other node's. It is owned by the
admin account unless --owner-phone names a farmer, so a bench or test device
never appears on a farmer's dashboard by accident. Its position defaults to
the farm site (SITE_LAT / SITE_LON).

--key-file appends DEVICE_KEY_<ID>=<key> to a local, gitignored file (for
example device_keys.local.env) so the key can be copied into the firmware.
"""
import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import generate_node_key, hash_node_key
from backend.config import settings
from backend.models import Farmer, SensorNode
from backend.schemas import normalise_phone

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")  # fits the 32-character ID column


class RegistrationError(Exception):
    """A device that cannot be registered: the message says why."""


def owner_for(session: Session, phone: str | None) -> Farmer:
    """The farmer given by phone number, or else the admin account."""
    if phone:
        try:
            phone = normalise_phone(phone)
        except ValueError as error:
            raise RegistrationError(str(error)) from error
        owner = session.scalar(select(Farmer).where(Farmer.phone_number == phone))
        if owner is None:
            raise RegistrationError(f"no account with phone number {phone}")
        return owner
    owner = session.scalar(select(Farmer).where(Farmer.role == "admin").order_by(Farmer.created_at))
    if owner is None:
        raise RegistrationError("no admin account: run  .\\dev.ps1 seed  first, or pass --owner-phone")
    return owner


def register_device(session: Session, device_id: str, name: str, owner_phone: str | None = None,
                    latitude: float | None = None, longitude: float | None = None) -> tuple[SensorNode, str]:
    """Create the device and return (device, raw key). The raw key exists only in this return value."""
    if not DEVICE_ID_PATTERN.match(device_id):
        raise RegistrationError("device_id must be 1-32 letters, digits, '-' or '_', e.g. node-01")
    if not name.strip():
        raise RegistrationError("name must not be empty")
    if session.get(SensorNode, device_id) is not None:
        raise RegistrationError(f"device {device_id} already exists; nothing was changed")

    key = generate_node_key()  # secrets.token_urlsafe(32): 32 random bytes
    device = SensorNode(
        node_id=device_id,
        farmer_id=owner_for(session, owner_phone).farmer_id,
        location=name.strip(),
        latitude=settings.SITE_LAT if latitude is None else latitude,
        longitude=settings.SITE_LON if longitude is None else longitude,
        api_key_hash=hash_node_key(key),  # only the hash is stored
    )
    session.add(device)
    session.commit()
    return device, key


def key_variable(device_id: str) -> str:
    """DEVICE_KEY_NODE_01 for node-01."""
    return "DEVICE_KEY_" + re.sub(r"[^A-Z0-9]", "_", device_id.upper())


def append_key(path: Path, device_id: str, name: str, key: str) -> None:
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write("# Raw device keys (X-Device-Key). Gitignored: never commit or share this file.\n")
        handle.write(f"# {device_id} ({name}), registered {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n")
        handle.write(f"{key_variable(device_id)}={key}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Register a sensor device and print its API key once.")
    parser.add_argument("device_id", help="e.g. node-01")
    parser.add_argument("name", help='e.g. "Bench node A"')
    parser.add_argument("--owner-phone", help="the owning farmer's phone number (default: the admin account)")
    parser.add_argument("--latitude", type=float, help="default: SITE_LAT")
    parser.add_argument("--longitude", type=float, help="default: SITE_LON")
    parser.add_argument("--key-file", type=Path, help="also append the key to this local file")
    args = parser.parse_args(argv)

    from backend.db import SessionLocal

    with SessionLocal() as session:
        try:
            device, key = register_device(session, args.device_id, args.name, args.owner_phone,
                                          args.latitude, args.longitude)
        except RegistrationError as error:
            print(f"Not registered: {error}")
            return 1

    print(f"Registered device {device.node_id} ({device.location}) at ({device.latitude}, {device.longitude})")
    print(f"X-Device-Key: {key}")
    print("WARNING: this key is shown once and cannot be shown again; only its hash is stored. "
          "Copy it into the device now.")
    if args.key_file:
        append_key(args.key_file, device.node_id, device.location, key)
        print(f"The key was also appended to {args.key_file} as {key_variable(device.node_id)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
