"""Application settings, read from the .env file at the repo root.

Secrets (database password, JWT secret, Africa's Talking key) are never
hard-coded; they must be present in .env or in the environment. On the
hosted server (render.yaml) they are environment variables, with no .env.
"""
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    DATABASE_URL: str
    # Only pytest uses it (backend/tests/conftest.py refuses to run without
    # it), so a server that never runs the tests may leave it unset.
    TEST_DATABASE_URL: str = ""

    # Auth
    JWT_SECRET: str
    JWT_EXPIRE_MINUTES: int

    # Africa's Talking SMS
    AT_USERNAME: str
    AT_API_KEY: str
    AT_SENDER_ID: str = ""  # optional: empty means Africa's Talking's default sender
    # On (the default): SMS are logged, not sent. Set SMS_DRY_RUN=false in .env to send.
    SMS_DRY_RUN: bool = True
    # Optional shared secret for the delivery-report callback URL
    # (.../sms/delivery-report?token=...). Empty: no token required.
    SMS_CALLBACK_TOKEN: str = ""

    # Farm site and local timezone
    SITE_LAT: float = -0.70
    SITE_LON: float = 36.60
    TIMEZONE: str = "Africa/Nairobi"

    # Risk-level bands for any 0-1 risk score (dashboard, fusion, history):
    # low < RISK_MEDIUM_FROM <= medium < RISK_HIGH_FROM <= high.
    RISK_MEDIUM_FROM: float = 0.33
    RISK_HIGH_FROM: float = 0.66

    # Fusion: tau(r) = clip(FUSION_TAU0 - FUSION_K * (r - 0.5), FUSION_TAU_MIN, FUSION_TAU_MAX)
    FUSION_TAU0: float = 0.60
    FUSION_K: float = 0.40
    FUSION_TAU_MIN: float = 0.35
    FUSION_TAU_MAX: float = 0.85

    # The hourly risk-scoring job (backend/risk_job.py), started with the API.
    RISK_JOB_ENABLED: bool = True
    RISK_JOB_INTERVAL_MINUTES: int = 60

    @field_validator("DATABASE_URL", "TEST_DATABASE_URL")
    @classmethod
    def _use_the_psycopg_driver(cls, url: str) -> str:
        """Accept the plain postgres:// URLs hosts hand out (Neon, Render).

        SQLAlchemy reads postgresql:// as "use psycopg2", which this project
        does not install; it uses psycopg 3, so the URL must say +psycopg.
        """
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
