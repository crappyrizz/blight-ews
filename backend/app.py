"""FastAPI application entry point.

Run locally from the repo root:
    .\\.venv\\Scripts\\python.exe -m uvicorn backend.app:app --reload
Interactive docs: http://127.0.0.1:8000/docs
"""
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import auth, ingest
from backend.config import settings
from backend.db import get_db
from backend.models import Farmer, SensorNode, SensorReading
from backend.schemas import (
    MAX_BATCH_SIZE,
    FarmerLogin,
    FarmerOut,
    FarmerRegister,
    IngestResult,
    ReadingBatch,
    ReadingOut,
    SensorNodeOut,
    TokenOut,
)

# Cap on how many readings one GET can return (a year of hourly data is
# about 8,760 rows, so this covers any realistic dashboard query).
MAX_READINGS_PER_QUERY = 10_000

app = FastAPI(title="Blight Early-Warning System")


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    """Report whether the API is up and can reach the database."""
    try:
        db.execute(select(1))
    except SQLAlchemyError:
        response.status_code = 503
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


# --- registration and login -------------------------------------------


@app.post("/auth/register", response_model=FarmerOut, status_code=status.HTTP_201_CREATED)
def register(payload: FarmerRegister, db: Session = Depends(get_db)):
    """Register a farmer. The phone number is already normalised by the schema."""
    existing = db.scalar(select(Farmer).where(Farmer.phone_number == payload.phone_number))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That phone number is already registered",
        )

    farmer = Farmer(
        name=payload.name,
        phone_number=payload.phone_number,
        password_hash=auth.hash_password(payload.password),
        role=auth.ROLE_FARMER,  # admins are created by seeding, not by signup
        farm_id=payload.farm_id,
    )
    db.add(farmer)
    try:
        db.commit()
    except IntegrityError:
        # Two registrations for the same number at the same moment: the
        # UNIQUE constraint decides, and the loser gets the same 409.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That phone number is already registered",
        )
    db.refresh(farmer)
    return farmer


@app.post("/auth/login", response_model=TokenOut)
def login(payload: FarmerLogin, db: Session = Depends(get_db)):
    """Exchange phone number and password for an access token."""
    farmer = auth.authenticate(db, payload.phone_number, payload.password)
    if farmer is None:
        # One message for both failures, so this cannot be used to find out
        # which numbers are registered.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect phone number or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(
        access_token=auth.create_access_token(farmer.farmer_id),
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
    )


@app.get("/me", response_model=FarmerOut)
def me(farmer: Farmer = Depends(auth.get_current_farmer)):
    """The logged-in farmer's own account."""
    return farmer


# --- nodes (the first records the ownership rule guards) ---------------


@app.get("/nodes", response_model=list[SensorNodeOut])
def list_nodes(
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """The farmer's own nodes. An admin sees every node."""
    query = select(SensorNode).order_by(SensorNode.node_id)
    if farmer.role != auth.ROLE_ADMIN:
        query = query.where(SensorNode.farmer_id == farmer.farmer_id)
    return db.scalars(query).all()


@app.get("/nodes/{node_id}", response_model=SensorNodeOut)
def get_node(
    node_id: str,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """One node, if it belongs to this farmer (or the caller is an admin)."""
    return auth.get_owned_or_404(db, SensorNode, node_id, farmer)


# --- sensor ingest (the firmware contract) -----------------------------


def _as_utc(moment: datetime) -> datetime:
    """Treat a query time with no timezone as UTC, so ranges are unambiguous."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


@app.post("/nodes/{node_id}/readings", response_model=IngestResult)
def post_readings(
    batch: ReadingBatch,
    node: SensorNode = Depends(auth.get_current_node),
    db: Session = Depends(get_db),
):
    """Accept a batch of readings from a sensor node (X-Node-Key required).

    Always answers 200 for a well-formed batch, even when every row was a
    duplicate or was rejected, so the node can clear its buffer on a 2xx and
    retry on anything else. See docs/firmware_contract.md.
    """
    if len(batch.readings) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"A batch may contain at most {MAX_BATCH_SIZE} readings",
        )
    return ingest.ingest_batch(db, node, batch.readings)


@app.get("/nodes/{node_id}/readings", response_model=list[ReadingOut])
def get_readings(
    node_id: str,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=MAX_READINGS_PER_QUERY, ge=1, le=MAX_READINGS_PER_QUERY),
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """A node's readings in time order (farmer JWT; own nodes only).

    `from` is inclusive and `to` is exclusive, so consecutive days can be
    fetched without overlapping.
    """
    auth.get_owned_or_404(db, SensorNode, node_id, farmer)

    query = select(SensorReading).where(SensorReading.node_id == node_id)
    if from_ is not None:
        query = query.where(SensorReading.timestamp >= _as_utc(from_))
    if to is not None:
        query = query.where(SensorReading.timestamp < _as_utc(to))
    query = query.order_by(SensorReading.timestamp).limit(limit)

    return db.scalars(query).all()


# --- admin -------------------------------------------------------------


@app.get("/admin/farmers", response_model=list[FarmerOut])
def list_farmers(
    admin: Farmer = Depends(auth.require_admin),
    db: Session = Depends(get_db),
):
    """All registered farmers. Admin only."""
    return db.scalars(select(Farmer).order_by(Farmer.created_at)).all()
