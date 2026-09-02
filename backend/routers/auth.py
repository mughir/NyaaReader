"""
Authentication and Health endpoints.
"""
import hmac
import os
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from database import get_db_session
from security import (
    _COOKIE_NAME,
    _SESSION_TTL,
    _auth_enabled,
    _auth_password,
    _login_guard_check,
    _login_guard_fail,
    _login_guard_success,
    _make_session_token,
)

router = APIRouter(tags=["auth"])


@router.get("/api/health")
async def health_check():
    return {"status": "healthy"}


@router.post("/api/auth/login")
async def login(body: dict, response: Response, request: Request,
                db: Session = Depends(get_db_session)):
    client_ip = request.client.host if request.client else "unknown"
    _login_guard_check(client_ip)
    pw = (body.get("password") or "").strip()
    if not _auth_enabled(db):
        return {"status": "disabled"}
    if hmac.compare_digest(pw, _auth_password(db)):
        _login_guard_success(client_ip)
        token = _make_session_token()
        secure = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes") \
                 or (request.url.scheme == "https")
        response.set_cookie(_COOKIE_NAME, token, max_age=_SESSION_TTL,
                            httponly=True, samesite="lax", secure=secure)
        return {"status": "ok"}
    _login_guard_fail(client_ip)
    raise HTTPException(status_code=401, detail="Wrong password")


@router.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(_COOKIE_NAME)
    return {"status": "ok"}


@router.get("/api/auth/status")
async def auth_status(db: Session = Depends(get_db_session)):
    """Whether password auth is enabled."""
    return {"enabled": _auth_enabled(db)}
