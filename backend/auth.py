"""Password hashing. Login and JWT handling will be added in the auth task."""
import bcrypt


def hash_password(password: str) -> str:
    """Return a bcrypt hash (includes its own random salt)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
