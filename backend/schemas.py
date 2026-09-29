"""Request and response shapes for the API (pydantic v2).

Phone numbers are the farmer's identity here: he logs in with his phone, and
SMS alerts go to the same number. So every phone number entering the system
is normalised to one canonical form, +2547XXXXXXXX or +2541XXXXXXXX, no
matter how it was typed.
"""
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Kenyan mobile numbers are 9 digits after the 254 country code and start
# with 7 (Safaricom, Airtel and others) or 1 (newer Safaricom ranges).
LOCAL_NUMBER_LENGTH = 9
VALID_FIRST_DIGITS = ("7", "1")
PHONE_ERROR = (
    "Phone number must be a Kenyan mobile number, e.g. 0712345678, "
    "0112345678, 254712345678 or +254712345678"
)


def normalise_phone(raw: str) -> str:
    """Return a phone number as +254XXXXXXXXX, or raise ValueError.

    Accepts the four forms people actually type: 0712345678, 0112345678,
    254712345678, +254712345678, with or without spaces, dashes or brackets.
    """
    if not isinstance(raw, str):
        raise ValueError(PHONE_ERROR)

    digits = re.sub(r"[\s\-()]", "", raw.strip())
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        raise ValueError(PHONE_ERROR)

    if digits.startswith("254"):
        local = digits[3:]
    elif digits.startswith("0"):
        local = digits[1:]
    else:
        local = digits  # already bare, e.g. 712345678

    if len(local) != LOCAL_NUMBER_LENGTH or local[0] not in VALID_FIRST_DIGITS:
        raise ValueError(PHONE_ERROR)

    return f"+254{local}"


class FarmerRegister(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone_number: str
    # bcrypt only reads the first 72 bytes of a password, so a longer one
    # would be silently truncated. Better to say so than to pretend.
    password: str = Field(min_length=8, max_length=72)
    farm_id: str | None = Field(default=None, max_length=32)

    @field_validator("phone_number")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_phone(value)


class FarmerLogin(BaseModel):
    phone_number: str
    password: str

    @field_validator("phone_number")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_phone(value)


class FarmerOut(BaseModel):
    """A farmer as the API returns them. Note: no password_hash."""

    model_config = ConfigDict(from_attributes=True)

    farmer_id: str
    name: str
    phone_number: str
    role: str
    farm_id: str | None
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the token expires


class SensorNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    farmer_id: str
    location: str
    latitude: float
    longitude: float
    last_sync_time: datetime | None
