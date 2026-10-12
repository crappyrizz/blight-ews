"""FastAPI application entry point.

Run locally from the repo root:
    .\\.venv\\Scripts\\python.exe -m uvicorn backend.app:app --reload
Interactive docs: http://127.0.0.1:8000/docs
"""
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import alerts, auth, cnn_service, dashboard, fusion, hutton, ids, ingest, risk_job
from backend.config import REPO_ROOT, settings
from backend.db import get_db
from backend.models import Farmer, LeafImage, SensorNode, SensorReading, SymptomReport
from backend.schemas import (
    MAX_BATCH_SIZE,
    AlertOut,
    DiagnosisOut,
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
    SymptomReportIn,
    SymptomReportOut,
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
    # The hourly risk job runs in a background thread for as long as the API
    # does (one API process only; scores are upserts, so a rerun is harmless).
    scheduler = risk_job.start_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


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
    node_id: str | None = Form(
        None, description="The sensor node at this plot; default: the farmer's only node"
    ),
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
    classifier: cnn_service.CNNClassifier = Depends(cnn_service.get_classifier),
):
    """Upload a leaf photo: store it, run the CNN, fuse with the weather risk.

    A photo the model cannot judge fairly (unreadable, too small, too dark,
    too blurry) gets 422 with a reason code and a message the app can show,
    and nothing is stored, so the farmer simply retakes it.
    """
    if capture_date is not None and capture_date.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="capture_date needs a timezone, e.g. 2026-10-12T09:30:00+03:00",
        )

    node = _node_for_photo(db, farmer, node_id)

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
    try:
        db.add(leaf_image)
        db.flush()
        # Fusion (backend/fusion.py): the weather risk r for the photo's own
        # day moves the late-blight threshold tau(r); the diagnosis row and
        # its 48 h of contributing sensor readings are written with the image.
        diagnosis, fused, contributions = fusion.diagnose(
            db, image_id, node.node_id if node else None, leaf_image.capture_date,
            prediction.probabilities,
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        path.unlink(missing_ok=True)  # no rows, so no orphan file either
        raise
    db.refresh(leaf_image)

    # SMS after the diagnosis is safely stored: a failed send can never lose it.
    sms_alert = None
    if node is not None:
        alert = alerts.send_diagnosis_alert(db, node, diagnosis)
        db.commit()
        sms_alert = None if alert is None else alert.delivery_status

    return ImageUploadOut(
        image=LeafImageOut.model_validate(leaf_image),
        prediction=PredictionOut(
            label=prediction.label,
            confidence=prediction.confidence,
            probabilities=prediction.probabilities,
        ),
        diagnosis=DiagnosisOut(
            result_id=diagnosis.result_id,
            label=fused.label,
            confidence=fused.confidence,
            risk_score_used=fused.risk_score_used,
            risk_basis=fused.risk_basis,
            risk_for_date=fused.risk_for_date,
            risk_note=fused.note,
            threshold_used=fused.threshold_used,
            late_blight_flagged=fused.late_blight_flagged,
            risk_level=fused.risk_level,
            summary=fused.summary,
            contributing_readings=contributions,
            sms_alert=sms_alert,
        ),
    )


def _node_for_photo(db: Session, farmer: Farmer, node_id: str | None) -> SensorNode | None:
    """The node whose weather applies to this photo.

    Given: it must be the farmer's (404 otherwise). Not given: the farmer's
    only node; with several the app must say which (422); with none, fusion
    falls back to the neutral prior and says so.
    """
    if node_id is not None:
        return auth.get_owned_or_404(db, SensorNode, node_id, farmer)
    nodes = db.scalars(select(SensorNode).where(SensorNode.farmer_id == farmer.farmer_id)).all()
    if len(nodes) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You have several sensor nodes; send node_id to say which plot the photo is from.",
        )
    return nodes[0] if nodes else None


@app.get("/images/{image_id}", response_model=LeafImageOut)
def get_image(
    image_id: str,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """One of the farmer's leaf images (404 for anyone else's, as for nodes)."""
    return auth.get_owned_or_404(db, LeafImage, image_id, farmer)


# --- dashboard, history and symptom reports (FR vii, viii) -------------


@app.get("/nodes/{node_id}/dashboard")
def node_dashboard(
    node_id: str,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """Latest reading, last 24 h, today's Hutton status, the 24 h / 48 h forecast,
    the current risk level and a one-sentence, decision-support summary."""
    node = auth.get_owned_or_404(db, SensorNode, node_id, farmer)
    return dashboard.build_dashboard(db, node)


MAX_HISTORY_DAYS = 365


@app.get("/nodes/{node_id}/history")
def node_history(
    node_id: str,
    days: int = Query(30, ge=1, le=MAX_HISTORY_DAYS),
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """Daily risk, diagnoses and alerts for the last `days` days, newest first (FR viii)."""
    node = auth.get_owned_or_404(db, SensorNode, node_id, farmer)
    return dashboard.build_history(db, node, days)


@app.post("/symptom-reports", response_model=SymptomReportOut, status_code=status.HTTP_201_CREATED)
def create_symptom_report(
    payload: SymptomReportIn,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """The farmer reports the day he first saw symptoms. Used to judge the warnings."""
    node = auth.get_owned_or_404(db, SensorNode, payload.node_id, farmer)
    today = datetime.now(hutton.LOCAL_TZ).date()  # the farmer's calendar day
    if payload.onset_date > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"onset_date cannot be in the future (today is {today})",
        )
    report = SymptomReport(farmer_id=farmer.farmer_id, node_id=node.node_id,
                           onset_date=payload.onset_date, notes=payload.notes)
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


@app.get("/symptom-reports", response_model=list[SymptomReportOut])
def list_symptom_reports(
    node_id: str | None = None,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """The farmer's own reports, newest onset first (optionally for one of his nodes)."""
    query = select(SymptomReport).where(SymptomReport.farmer_id == farmer.farmer_id)
    if node_id is not None:
        auth.get_owned_or_404(db, SensorNode, node_id, farmer)
        query = query.where(SymptomReport.node_id == node_id)
    return db.scalars(query.order_by(SymptomReport.onset_date.desc(),
                                     SymptomReport.reported_at.desc())).all()


# --- SMS: status on demand, and Africa's Talking delivery reports (FR vii) ----


@app.post("/nodes/{node_id}/status-sms", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
def status_sms(
    node_id: str,
    farmer: Farmer = Depends(auth.get_current_farmer),
    db: Session = Depends(get_db),
):
    """Text the farmer the plot's status: only when the risk is judged and low.

    409 (with a reason) when the risk is unknown, not low, or a status SMS
    went out in the last hour; the app shows the dashboard instead.
    """
    node = auth.get_owned_or_404(db, SensorNode, node_id, farmer)
    try:
        alert = alerts.send_status(db, node, datetime.now(hutton.LOCAL_TZ).date())
    except alerts.StatusRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": refused.reason, "message": refused.message},
        ) from None
    db.commit()
    db.refresh(alert)
    return alert


@app.post("/sms/delivery-report")
def sms_delivery_report(
    id: str = Form(..., description="Africa's Talking message id (ATXid_...)"),
    status_: str = Form(..., alias="status"),
    failureReason: str | None = Form(None),  # Africa's Talking's own field name
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Africa's Talking's delivery-report callback (form-encoded POST).

    Fields used: id, status (e.g. Success, Failed, Rejected, Buffered, Sent),
    failureReason (only on Failed/Rejected); phoneNumber, networkCode and
    retryCount are also sent and ignored. An unknown id still answers 200, so
    Africa's Talking does not keep retrying a message we did not send.
    """
    if settings.SMS_CALLBACK_TOKEN and not secrets.compare_digest(
        token or "", settings.SMS_CALLBACK_TOKEN
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad token")
    alert = alerts.apply_delivery_report(db, id, status_, failureReason)
    db.commit()
    return {"updated": alert is not None}
