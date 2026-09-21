"""ORM models: the 10 tables described in the project README.

Conventions used throughout:
- Primary keys are prefixed strings from backend.ids (e.g. FRM-..., RD-...).
- Every timestamp is timezone-aware (Postgres timestamptz) and stored in UTC.
  Conversion to Africa/Nairobi happens only for daily aggregation and display.
- "Enum" columns are plain strings guarded by CHECK constraints, which are
  simpler to change in a migration than native Postgres ENUM types.
- Columns are NOT NULL unless the design marks them nullable.
"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend import ids

ID = String(32)  # column type shared by every ID / foreign-key column


def id_default(prefix):
    """Column default that generates a new prefixed ID on insert."""
    return lambda: ids.new_id(prefix)


class Base(DeclarativeBase):
    pass


class Farmer(Base):
    __tablename__ = "farmers"
    __table_args__ = (
        CheckConstraint("role IN ('farmer', 'admin')", name="ck_farmers_role"),
    )

    farmer_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.FARMER))
    name: Mapped[str] = mapped_column(String(100))
    phone_number: Mapped[str] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(10))
    # Plain label, not a foreign key: there is no farms table. Nullable
    # because an admin account does not belong to a farm.
    farm_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SensorNode(Base):
    __tablename__ = "sensor_nodes"

    node_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.SENSOR_NODE))
    farmer_id: Mapped[str] = mapped_column(ID, ForeignKey("farmers.farmer_id"))
    location: Mapped[str] = mapped_column(String(200))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    # Null until the node sends its first reading.
    last_sync_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    # A node can report only one reading per timestamp. This makes re-sending
    # the same reading (e.g. after a weak-network retry) safe to reject.
    __table_args__ = (
        UniqueConstraint("node_id", "timestamp", name="uq_sensor_readings_node_timestamp"),
    )

    reading_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.SENSOR_READING))
    node_id: Mapped[str] = mapped_column(ID, ForeignKey("sensor_nodes.node_id"))
    temperature: Mapped[float] = mapped_column(Float)  # degrees C
    humidity: Mapped[float] = mapped_column(Float)  # relative humidity, %
    leaf_wetness: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WeatherReading(Base):
    __tablename__ = "weather_readings"
    __table_args__ = (
        CheckConstraint(
            "source IN ('archive', 'historical_forecast', 'forecast')",
            name="ck_weather_readings_source",
        ),
        # Postgres normally treats NULLs as all different in a UNIQUE
        # constraint, so two identical 'archive' rows (forecast_issued_at is
        # NULL) would both be accepted. NULLS NOT DISTINCT (Postgres 15+)
        # makes NULL count as a value here, so those duplicates are rejected.
        UniqueConstraint(
            "source", "latitude", "longitude", "timestamp", "forecast_issued_at",
            name="uq_weather_readings_source_point_time",
            postgresql_nulls_not_distinct=True,
        ),
    )

    weather_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.WEATHER_READING))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    temperature: Mapped[float] = mapped_column(Float)  # degrees C
    humidity: Mapped[float] = mapped_column(Float)  # relative humidity, %
    # When the forecast was issued; null for 'archive' (observed) data.
    forecast_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskScore(Base):
    __tablename__ = "risk_scores"
    __table_args__ = (
        CheckConstraint("method IN ('hutton', 'learned')", name="ck_risk_scores_method"),
        CheckConstraint("data_source IN ('sensor', 'api')", name="ck_risk_scores_data_source"),
        CheckConstraint("horizon_hours IN (0, 24, 48)", name="ck_risk_scores_horizon_hours"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_risk_scores_score_range"),
    )

    score_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.RISK_SCORE))
    node_id: Mapped[str] = mapped_column(ID, ForeignKey("sensor_nodes.node_id"))
    method: Mapped[str] = mapped_column(String(10))
    data_source: Mapped[str] = mapped_column(String(10))
    horizon_hours: Mapped[int] = mapped_column(Integer)  # 0 = today, 24 / 48 = forecast
    score: Mapped[float] = mapped_column(Float)  # 0..1
    # Null means "insufficient data" (fewer than 20 of 24 hourly values),
    # which is deliberately different from False.
    hutton_day: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hutton_period: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    for_date: Mapped[date] = mapped_column(Date)  # local (Africa/Nairobi) calendar day
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeafImage(Base):
    __tablename__ = "leaf_images"

    image_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.LEAF_IMAGE))
    farmer_id: Mapped[str] = mapped_column(ID, ForeignKey("farmers.farmer_id"))
    file_path: Mapped[str] = mapped_column(String(500))
    capture_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DiagnosisResult(Base):
    __tablename__ = "diagnosis_results"
    __table_args__ = (
        CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="ck_diagnosis_results_risk_level"),
    )

    result_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.DIAGNOSIS_RESULT))
    # UNIQUE: each image is diagnosed exactly once.
    image_id: Mapped[str] = mapped_column(ID, ForeignKey("leaf_images.image_id"), unique=True)
    disease_label: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float)
    probabilities: Mapped[dict] = mapped_column(JSONB)  # e.g. {"late_blight": 0.81, ...}
    risk_score_used: Mapped[float] = mapped_column(Float)  # r fed into the fusion rule
    threshold_used: Mapped[float] = mapped_column(Float)  # tau(r) that resulted
    risk_level: Mapped[str] = mapped_column(String(10))
    late_blight_flagged: Mapped[bool] = mapped_column(Boolean)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReadingContribution(Base):
    """Link table: which sensor readings fed into a diagnosis."""
    __tablename__ = "reading_contributions"

    reading_id: Mapped[str] = mapped_column(ID, ForeignKey("sensor_readings.reading_id"), primary_key=True)
    result_id: Mapped[str] = mapped_column(ID, ForeignKey("diagnosis_results.result_id"), primary_key=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(
            "alert_type IN ('high_risk', 'diagnosis', 'low_risk')",
            name="ck_alerts_alert_type",
        ),
    )

    alert_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.ALERT))
    node_id: Mapped[str] = mapped_column(ID, ForeignKey("sensor_nodes.node_id"))
    result_id: Mapped[str | None] = mapped_column(ID, ForeignKey("diagnosis_results.result_id"), nullable=True)
    risk_score_id: Mapped[str | None] = mapped_column(ID, ForeignKey("risk_scores.score_id"), nullable=True)
    alert_type: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivery_status: Mapped[str] = mapped_column(String(30))
    # Null when the SMS provider rejected the message and returned no ID.
    provider_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SymptomReport(Base):
    __tablename__ = "symptom_reports"

    report_id: Mapped[str] = mapped_column(ID, primary_key=True, default=id_default(ids.SYMPTOM_REPORT))
    farmer_id: Mapped[str] = mapped_column(ID, ForeignKey("farmers.farmer_id"))
    node_id: Mapped[str] = mapped_column(ID, ForeignKey("sensor_nodes.node_id"))
    onset_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
