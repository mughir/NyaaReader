"""
Security and authentication management for NyaaReader.
"""
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets as _secrets
import sys
import time as _time
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("novel-reader.security")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")) if not os.name == "nt" else Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_SESSION_TTL = 30 * 24 * 3600  # 30 days
_COOKIE_NAME = "nyaa_session"

# Brute-force guard: per-IP failed-login tracking with exponential backoff.
_login_attempts = {}  # ip -> {"fails": int, "locked_until": float}


def _get_main_attr(name: str, fallback):
    main_mod = sys.modules.get("main")
    if main_mod is not None and hasattr(main_mod, name):
        return getattr(main_mod, name)
    return fallback


def _login_guard_check(client_ip: str):
    rec = _login_attempts.get(client_ip)
    if rec and rec["locked_until"] > _time.time():
        wait = int(rec["locked_until"] - _time.time())
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {wait}s")


def _login_guard_fail(client_ip: str):
    rec = _login_attempts.get(client_ip, {"fails": 0, "locked_until": 0})
    rec["fails"] += 1
    # exponential: 5s, 20s, 60s, 4m, 15m, 1h cap
    wait = min(3600, 5 * (4 ** (rec["fails"] - 1)))
    rec["locked_until"] = _time.time() + wait
    _login_attempts[client_ip] = rec
    # keep the dict small: drop entries idle > 1h
    if len(_login_attempts) > 500:
        cutoff = _time.time() - 3600
        for k in [k for k, v in _login_attempts.items() if v["locked_until"] < cutoff]:
            _login_attempts.pop(k, None)


def _login_guard_success(client_ip: str):
    _login_attempts.pop(client_ip, None)


def _auth_enabled(db=None) -> bool:
    from services.config_service import _get_config
    cfg = _get_config()
    return bool((cfg.get("auth_password") or "").strip())


def _auth_password(db=None) -> str:
    from services.config_service import _get_config
    return (_get_config().get("auth_password") or "")


def _sign_session(token: str) -> str:
    return hmac.new(_SESSION_SECRET().encode(), token.encode(), hashlib.sha256).hexdigest()


def _SESSION_SECRET() -> str:
    """Persistent HMAC secret for cookie signing (stored in data dir)."""
    f = DATA_DIR / "session_secret"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    s = _secrets.token_hex(32)
    try:
        f.write_text(s, encoding="utf-8")
    except OSError as e:
        logger.warning(f"could not write session secret to disk: {e}")
    return s


def _rotate_session_secret():
    """Rotate the cookie-signing secret → invalidates EVERY outstanding session
    token. Called on password change (and password removal) so a leaked cookie
    stops working immediately instead of lingering up to _SESSION_TTL."""
    f = DATA_DIR / "session_secret"
    try:
        f.write_text(_secrets.token_hex(32), encoding="utf-8")
    except OSError as e:
        logger.warning(f"could not rotate session secret: {e}")


def _make_session_token() -> str:
    token = _secrets.token_hex(32)
    return f"{token}.{int(_time.time())}.{_sign_session(token)}"


def _verify_session(cookie: str) -> bool:
    if not cookie:
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    token, ts, sig = parts
    try:
        ts_i = int(ts)
    except ValueError:
        return False
    if _time.time() - ts_i > _SESSION_TTL:
        return False
    expect = hmac.new(_SESSION_SECRET().encode(), token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


async def _require_auth(request: Request):
    """FastAPI dependency: 401 redirect to /login when auth is on and no valid session."""
    from database import SessionLocal as _SL
    _db = _SL()
    try:
        check_fn = _get_main_attr("_auth_enabled", _auth_enabled)
        if not check_fn(_db):
            return None  # auth off — allow
    finally:
        _db.close()
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and _verify_session(cookie):
        return None
    raise HTTPException(status_code=401, detail="Login required")


_auth_flag_cache = {"value": None, "ts": 0.0}


def _auth_enabled_cached() -> bool:
    """_auth_enabled() hits the DB; the auth middleware runs on EVERY request.
    Cache the flag for a few seconds so config reads stay cheap."""
    now = _time.time()
    cache = _get_main_attr("_auth_flag_cache", _auth_flag_cache)
    if cache["ts"] < now - 5:
        try:
            check_fn = _get_main_attr("_auth_enabled", _auth_enabled)
            cache["value"] = check_fn()
            cache["ts"] = now
        except Exception as e:
            logger.warning(f"auth-enabled check failed, keeping previous state: {e}")
            if cache["value"] is None:
                cache["value"] = True
    return cache["value"]
