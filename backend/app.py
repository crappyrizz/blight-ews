"""FastAPI application entry point.

Run locally from the repo root:
    .\\.venv\\Scripts\\python.exe -m uvicorn backend.app:app --reload
"""
from fastapi import Depends, FastAPI, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db import get_db

app = FastAPI(title="Blight Early-Warning System")


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    """Report whether the API is up and can reach the database."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = 503
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
