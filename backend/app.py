"""FastAPI application entry point.

Run locally from the repo root:
    .\\.venv\\Scripts\\python.exe -m uvicorn backend.app:app --reload
Interactive docs: http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import auth, cnn_service, ids, ingest
from backend.config import REPO_ROOT, settings
from backend.db import get_db
from backend.models import Farmer, LeafImage, SensorNode, SensorReading
from backend.schemas import (
    MAX_BATCH_SIZE,
    FarmerLogin,
    FarmerOut,
    FarmerRegister,
    ImageUploadOut,
    IngestResult,
    LeafImageOut,
    PredictionOut,
    ReadingBatch,
    ReadingOut,
    SensorNodeOut,
    TokenOut,
)

# Cap on how many readings one GET can return (a year of hourly data is
# about 8,760 rows, so this covers any realistic dashboard query).
MAX_READINGS_PER_QUERY = 10_000

# Leaf photos are stored here, one folder per farmer (gitignored with data/).
UPLOAD_DIR = REPO_ROOT / "data" / "uploads"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # a full-resolution phone photo is 3-8 MB
SAVED_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the CNN once, not per request. If the model files are missing the
    # API still starts; only image upload answers 503.
    cnn_service.load_classifier()
    yield


app = FastAPI(title="Blight Early-Warning System", lifespan=lifespan)


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
        ) from None
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
        return moment.replace(tzinfo=UTC)
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


# --- leaf images and the CNN (FR iv, v) --------------------------------


def _stored_path(path) -> str:
    """Repo-relative path with forward slashes, as recorded in leaf_images."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


@app.post("/images", response_model=ImageUploadOut, status_code=status.HTTP_201_CREATED)
def upload_image(
    file: UploadFile = File(..., description="The leaf photo (JPEG or PNG)"),
    capture_date: datetime | None = Form(
        None, description="When the photo was taken, with timezone; default: now"
    ),
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
    classifier: cnn_service.CNNClassifier = Depends(cnn_service.get_classifier),
):
    """Upload a leaf photo, store it, and return the CNN's raw prediction.

    A photo the model cannot judge fairly (unreadable, too small, too dark,
    too blurry) gets 422 with a reason code and a message the app can show,
    and nothing is stored, so the farmer simply retakes it.
    """
    if capture_date is not None and capture_date.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="capture_date needs a timezone, e.g. 2026-10-12T09:30:00+03:00",
        )

    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The photo is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )

    try:
        image = cnn_service.load_image(data)
        cnn_service.check_quality(image)
    except cnn_service.ImageRejected as rejected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": rejected.reason, "message": rejected.message},
        ) from None

    prediction = classifier.predict(cnn_service.to_model_input(image))

    # Keep the file exactly as uploaded; the model saw a resized copy.
    image_id = ids.new_id(ids.LEAF_IMAGE)
    extension = SAVED_EXTENSIONS.get(cnn_service.image_format(data), ".img")
    farmer_dir = UPLOAD_DIR / farmer.farmer_id
    farmer_dir.mkdir(parents=True, exist_ok=True)
    path = farmer_dir / f"{image_id}{extension}"
    path.write_bytes(data)

    leaf_image = LeafImage(
        image_id=image_id,
        farmer_id=farmer.farmer_id,
        file_path=_stored_path(path),
        capture_date=capture_date or datetime.now(UTC),
    )
    db.add(leaf_image)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        path.unlink(missing_ok=True)  # no row, so no orphan file either
        raise
    db.refresh(leaf_image)

    # ===================== FUSION HOOK (later prompt) =====================
    # This is where the weather-based risk score r is combined with the CNN:
    # fusion.resolve_risk() picks today's risk for the farmer's node,
    # tau(r) shifts the late-blight threshold, and a diagnosis_results row is
    # written (label, confidence, probabilities, risk_score_used,
    # threshold_used, risk_level, late_blight_flagged). Until then the raw
    # prediction below is returned as-is and no diagnosis row is stored.
    # ======================================================================

    return ImageUploadOut(
        image=LeafImageOut.model_validate(leaf_image),
        prediction=PredictionOut(
            label=prediction.label,
            confidence=prediction.confidence,
            probabilities=prediction.probabilities,
        ),
    )


@app.get("/images/{image_id}", response_model=LeafImageOut)
def get_image(
    image_id: str,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """One of the farmer's leaf images (404 for anyone else's, as for nodes)."""
    return auth.get_owned_or_404(db, LeafImage, image_id, farmer)
