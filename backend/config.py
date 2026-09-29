"""Application settings, read from the .env file at the repo root.

Secrets (database password, JWT secret, Africa's Talking key) are never
hard-coded; they must be present in .env or in the environment.
"""
from pathlib import Path

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
    TEST_DATABASE_URL: str

    # Auth
    JWT_SECRET: str
    JWT_EXPIRE_MINUTES: int

    # Africa's Talking SMS
    AT_USERNAME: str
    AT_API_KEY: str
    AT_SENDER_ID: str = ""  # optional: empty means Africa's Talking's default sender

    # Farm site and local timezone
    SITE_LAT: float = -0.70
    SITE_LON: float = 36.60
    TIMEZONE: str = "Africa/Nairobi"


settings = Settings()
