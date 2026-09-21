"""Seed the database with one admin, one farmer and one sensor node.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m tools.seed

Safe to run more than once: each record is looked up first and only created
if missing. Passwords are generated randomly and printed ONCE, when the
account is first created, so no password is ever stored in the code.
"""
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import hash_password
from backend.config import settings
from backend.models import Farmer, SensorNode

ADMIN_PHONE = "+254700000001"  # placeholder
FARMER_PHONE = "+254700000002"  # placeholder
FARMER_NAME = "Kinangop Farmer"
NODE_LOCATION = "Kinangop plot"


def get_or_create_farmer(session: Session, name: str, phone: str, role: str) -> tuple[Farmer, str | None]:
    """Return (farmer, new_password). new_password is None if the farmer already existed."""
    farmer = session.scalar(select(Farmer).where(Farmer.phone_number == phone))
    if farmer is not None:
        return farmer, None
    password = secrets.token_urlsafe(9)
    farmer = Farmer(name=name, phone_number=phone, password_hash=hash_password(password), role=role)
    session.add(farmer)
    session.flush()  # assigns farmer_id so the node can reference it
    return farmer, password


def get_or_create_node(session: Session, farmer: Farmer) -> tuple[SensorNode, bool]:
    """Return (node, created) for the farmer's node at the configured site."""
    node = session.scalar(
        select(SensorNode).where(
            SensorNode.farmer_id == farmer.farmer_id,
            SensorNode.latitude == settings.SITE_LAT,
            SensorNode.longitude == settings.SITE_LON,
        )
    )
    if node is not None:
        return node, False
    node = SensorNode(
        farmer_id=farmer.farmer_id,
        location=NODE_LOCATION,
        latitude=settings.SITE_LAT,
        longitude=settings.SITE_LON,
    )
    session.add(node)
    session.flush()
    return node, True


def seed(session: Session) -> list[str]:
    """Create the seed records if missing. Returns human-readable log lines."""
    log = []

    admin, admin_pw = get_or_create_farmer(session, "Admin", ADMIN_PHONE, "admin")
    if admin_pw:
        log.append(f"Created admin {admin.farmer_id} phone={ADMIN_PHONE} password={admin_pw}")
    else:
        log.append(f"Admin {admin.farmer_id} already exists")

    farmer, farmer_pw = get_or_create_farmer(session, FARMER_NAME, FARMER_PHONE, "farmer")
    if farmer_pw:
        log.append(f"Created farmer {farmer.farmer_id} phone={FARMER_PHONE} password={farmer_pw}")
    else:
        log.append(f"Farmer {farmer.farmer_id} already exists")

    node, created = get_or_create_node(session, farmer)
    log.append(f"{'Created' if created else 'Node already exists:'} node {node.node_id} "
               f"at ({node.latitude}, {node.longitude})")

    session.commit()
    return log


def main():
    from backend.db import SessionLocal

    with SessionLocal() as session:
        for line in seed(session):
            print(line)
    print("Note: passwords are shown only when an account is first created.")


if __name__ == "__main__":
    main()
