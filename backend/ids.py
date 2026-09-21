"""Prefixed string IDs, e.g. FRM-3F9A1C07B2E4.

Each table has its own prefix so an ID tells you what kind of record it is.
The part after the dash is 12 random hex characters (48 bits) taken from a
UUID4, which makes collisions practically impossible at this project's scale.
"""
import uuid

FARMER = "FRM"
SENSOR_NODE = "NODE"
SENSOR_READING = "RD"
WEATHER_READING = "WX"
RISK_SCORE = "RSK"
LEAF_IMAGE = "IMG"
DIAGNOSIS_RESULT = "DX"
ALERT = "ALR"
SYMPTOM_REPORT = "SYM"

PREFIXES = {
    FARMER, SENSOR_NODE, SENSOR_READING, WEATHER_READING, RISK_SCORE,
    LEAF_IMAGE, DIAGNOSIS_RESULT, ALERT, SYMPTOM_REPORT,
}


def new_id(prefix: str) -> str:
    """Return a new ID such as 'RD-1A2B3C4D5E6F' for the given prefix."""
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown ID prefix: {prefix!r}")
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"
