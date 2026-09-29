"""Passwords, tokens, and who is allowed to see what.

Three things live here:

1. Password hashing with passlib + bcrypt. bcrypt salts each password itself,
   so two farmers with the same password get different hashes, and it is
   deliberately slow, which makes guessing expensive.
2. JWT access tokens. The token carries the farmer_id and an expiry time,
   signed with JWT_SECRET. Nothing secret is stored in it: a token is a
   claim about who you are, not a store of data.
3. One ownership check, used everywhere. The farmer may read only his own
   records; an admin may read any. Because every table reaches a farmer by a
   different route (a reading through its node, a diagnosis through its
   image), that routing is written down once, in OWNER_LOOKUPS, rather than
   repeated in each endpoint where it could be got wrong.
"""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import get_db
from backend.models import (
    Alert,
    DiagnosisResult,
    Farmer,
    LeafImage,
    RiskScore,
    SensorNode,
    SensorReading,
    SymptomReport,
)

ALGORITHM = "HS256"
ROLE_ADMIN = "admin"
ROLE_FARMER = "farmer"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# auto_error=False so a missing header produces our own 401 with a clear
# message, rather than FastAPI's default.
bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


# --- passwords ---------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password for storage. The salt is part of the returned hash."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


# --- tokens -----------------------------------------------------------


def create_access_token(farmer_id: str, expires_minutes: int | None = None) -> str:
    """Sign a token for this farmer. Expiry comes from config unless given."""
    minutes = settings.JWT_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": farmer_id,  # the subject: who this token is for
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Verify signature and expiry, returning the payload. 401 if either fails."""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired, please log in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise CREDENTIALS_ERROR


# --- logging in --------------------------------------------------------


def authenticate(session: Session, phone_number: str, password: str) -> Farmer | None:
    """Return the farmer if the phone and password match, else None.

    The caller must not say which half was wrong: that would let someone test
    whether a phone number is registered.
    """
    farmer = session.scalar(select(Farmer).where(Farmer.phone_number == phone_number))
    if farmer is None:
        return None
    if not verify_password(password, farmer.password_hash):
        return None
    return farmer


# --- dependencies -----------------------------------------------------


def get_current_farmer(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Farmer:
    """The farmer behind the Bearer token on this request."""
    if credentials is None:
        raise CREDENTIALS_ERROR

    payload = decode_access_token(credentials.credentials)
    farmer_id = payload.get("sub")
    if not farmer_id:
        raise CREDENTIALS_ERROR

    farmer = db.get(Farmer, farmer_id)
    if farmer is None:
        # Valid signature, but the account is gone (e.g. deleted since).
        raise CREDENTIALS_ERROR
    return farmer


def require_admin(farmer: Farmer = Depends(get_current_farmer)) -> Farmer:
    """Allow only admin accounts through."""
    if farmer.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return farmer


# --- ownership --------------------------------------------------------

# How each kind of record reaches the farmer who owns it. Written once here,
# so no endpoint has to work it out for itself.
OWNER_LOOKUPS = {
    Farmer: lambda session, obj: obj.farmer_id,
    SensorNode: lambda session, obj: obj.farmer_id,
    LeafImage: lambda session, obj: obj.farmer_id,
    SymptomReport: lambda session, obj: obj.farmer_id,
    SensorReading: lambda session, obj: session.get(SensorNode, obj.node_id).farmer_id,
    RiskScore: lambda session, obj: session.get(SensorNode, obj.node_id).farmer_id,
    Alert: lambda session, obj: session.get(SensorNode, obj.node_id).farmer_id,
    DiagnosisResult: lambda session, obj: session.get(LeafImage, obj.image_id).farmer_id,
}


def owner_id(session: Session, obj) -> str:
    """The farmer_id that owns this record, whatever kind of record it is."""
    lookup = OWNER_LOOKUPS.get(type(obj))
    if lookup is None:
        raise TypeError(f"No ownership rule for {type(obj).__name__}")
    return lookup(session, obj)


def owns(session: Session, farmer: Farmer, obj) -> bool:
    """Does this farmer own the record? Admins count as owning everything."""
    if farmer.role == ROLE_ADMIN:
        return True
    return owner_id(session, obj) == farmer.farmer_id


def get_owned_or_404(session: Session, model, primary_key: str, farmer: Farmer):
    """Fetch a record the farmer is allowed to see, or raise 404.

    Someone else's record answers 404, not 403, on purpose: a 403 would
    confirm that the ID exists, letting an outsider map the system by
    guessing IDs. As far as this farmer is concerned, it is not there.
    """
    obj = session.get(model, primary_key)
    if obj is None or not owns(session, farmer, obj):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{model.__name__} not found",
        )
    return obj
