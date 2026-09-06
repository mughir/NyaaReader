"""
Configuration and model relay health-checking services for NyaaReader.
"""
import asyncio
import json as _json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime as _dt
from typing import Optional

from ai_provider import call_ai_provider

logger = logging.getLogger("novel-reader.config")

# config field -> environment variable that actually powers the translator
_CONFIG_ENV = (
    ("gemini_api_key", "GEMINI_API_KEY"),
    ("fallback_api_key", "FALLBACK_API_KEY"),
    ("fallback_base_url", "FALLBACK_BASE_URL"),
    ("fallback_model", "FALLBACK_MODEL"),
    ("fallback_model_2", "FALLBACK_MODEL_2"),
    ("fallback_2_base_url", "FALLBACK_2_BASE_URL"),
    ("fallback_2_api_key", "FALLBACK_2_API_KEY"),
)

_relay_health_cache: dict = {}


def _get_config():
    """AppConfig singleton row as a dict (seeded from env on first use)."""
    from database import SessionLocal
    from models import AppConfig
    db = SessionLocal()
    try:
        cfg = db.query(AppConfig).filter(AppConfig.id == 1).first()
        if not cfg:
            cfg = AppConfig(id=1)
            cfg.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
            cfg.fallback_api_key = os.getenv("FALLBACK_API_KEY", "")
            cfg.fallback_base_url = os.getenv("FALLBACK_BASE_URL", "https://opencode.ai/zen/go/v1")
            cfg.fallback_model = os.getenv("FALLBACK_MODEL", "deepseek-v4-flash")
            cfg.fallback_model_2 = os.getenv("FALLBACK_MODEL_2", "gpt-5.6-luna")
            cfg.fallback_2_base_url = os.getenv("FALLBACK_2_BASE_URL", "")
            cfg.fallback_2_api_key = os.getenv("FALLBACK_2_API_KEY", "")
            db.add(cfg)
            db.commit()
        return {
            "gemini_api_key": cfg.gemini_api_key or "",
            "fallback_api_key": cfg.fallback_api_key or "",
            "fallback_base_url": cfg.fallback_base_url or "",
            "fallback_model": cfg.fallback_model or "",
            "fallback_model_2": cfg.fallback_model_2 or "",
            "fallback_2_base_url": cfg.fallback_2_base_url or "",
            "fallback_2_api_key": cfg.fallback_2_api_key or "",
            "backup_enabled": bool(cfg.backup_enabled),
            "backup_interval_hours": cfg.backup_interval_hours or 24,
            "backup_keep": cfg.backup_keep or 14,
            "auth_password": cfg.auth_password or "",
        }
    finally:
        db.close()


def _apply_config_to_env(cleared=None):
    """Push DB config into os.environ so get_translator() picks it up, and reset
    the translator singleton so the next call rebuilds with the new keys."""
    cfg = _get_config()
    cleared = set(cleared or ())
    for field, env_name in _CONFIG_ENV:
        val = (cfg.get(field) or "").strip()
        if val:
            os.environ[env_name] = val
        elif field in cleared:
            os.environ.pop(env_name, None)
    import translator as _tr
    _tr._translator_instance = None


def _chat_completions_sync(base: str, key: str, model: str, timeout: int = 25) -> str:
    """Send one tiny chat message ('hi') and return the reply content."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
        "temperature": 0,
    }
    data = call_ai_provider(
        base_url=base,
        endpoint="/chat/completions",
        api_key=key,
        payload=payload,
        session_id="nyaa-healthcheck",
        timeout=timeout,
    )
    content = ""
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        content = ""
    return content.strip()


def _config_health_check_sync(p: dict) -> dict:
    """Blocking implementation of the config health check (call via to_thread)."""
    # Test primary relay
    key = (p.get("api_key") or os.getenv("FALLBACK_API_KEY", "")).strip()
    base = (p.get("base_url") or os.getenv("FALLBACK_BASE_URL", "https://opencode.ai/zen/go/v1")).strip().rstrip("/")
    model1 = (p["model"] if "model" in p else os.getenv("FALLBACK_MODEL", "")).strip()
    model2 = (p["model_2"] if "model_2" in p else os.getenv("FALLBACK_MODEL_2", "")).strip()

    key_ok = True
    available = []
    try:
        data = call_ai_provider(
            base_url=base,
            endpoint="/models",
            method="GET",
            api_key=key,
            session_id="nyaa-healthcheck",
            timeout=25,
        )
        lst = data if isinstance(data, list) else data.get("data", [])
        available = [m.get("id") if isinstance(m, dict) else str(m) for m in lst]
    except Exception as e:
        key_ok = False
        return {
            "key_ok": False,
            "message": f"Could not reach relay at {base}: {e}",
            "models": {"model": False, "model_2": False},
        }

    def valid(name):
        if not name:
            return True  # blank tier 2 is allowed; blank model1 handled by caller
        n = name.strip().lower()
        return any(a.lower() == n for a in available) or any(n in a.lower() for a in available)

    m1_ok = valid(model1)
    m2_ok = valid(model2) if model2 else True
    message = "OK"
    if not m1_ok:
        message = f"Model '{model1}' not found on relay (cleared)"
    elif not m2_ok:
        message = f"Model 2 '{model2}' not found on relay (cleared)"

    chat_model = True
    chat_msg = "OK"
    chat_suggested = None
    if model1 and m1_ok:
        try:
            reply = _chat_completions_sync(base, key, model1)
            chat_model = bool(reply)
            if not chat_model:
                chat_msg = f"Model '{model1}' answered with empty content — check key/quota"
        except urllib.error.HTTPError as e:
            # urllib's default text ("HTTP Error 500") hides the relay's
            # useful JSON reason, which is especially misleading when /models
            # is public but /chat/completions requires model-scoped access.
            try:
                detail = e.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            detail = " ".join(detail.split())[:500]
            status = f"HTTP {e.code}"
            chat_msg = f"Model '{model1}' failed the test query: {status}"
            if detail:
                chat_msg += f" — {detail}"
            chat_model = False
            sugg = next((a for a in available if a.lower() == model1.lower()), None)
            if sugg and sugg != model1:
                chat_suggested = sugg
                chat_msg += f" Did you mean '{sugg}'? Model ids are case-sensitive."
        except Exception as e:
            chat_model = False
            chat_msg = f"Model '{model1}' failed the test query: {e}"
            sugg = next((a for a in available if a.lower() == model1.lower()), None)
            if sugg and sugg != model1:
                chat_suggested = sugg
                chat_msg += f" Did you mean '{sugg}'? Model ids are case-sensitive."
    elif not model1:
        chat_msg = "Model 1 not configured — no test query run"

    # Model 2's own relay
    m2_base = (p.get("fallback_2_base_url") or os.getenv("FALLBACK_2_BASE_URL", "")).strip().rstrip("/")
    m2_key = (p.get("fallback_2_api_key") or os.getenv("FALLBACK_2_API_KEY", "")).strip()
    m2_model = model2

    chat_model_2 = True
    chat_msg_2 = "OK"
    chat_suggested_2 = None
    fallback2_result = {}
    m2_separate = m2_base and m2_key and m2_base != base
    available2 = []
    if m2_separate:
        try:
            data = call_ai_provider(
                base_url=m2_base,
                endpoint="/models",
                method="GET",
                api_key=m2_key,
                session_id="nyaa-healthcheck",
                timeout=25,
            )
            lst = data if isinstance(data, list) else data.get("data", [])
            available2 = [m.get("id") if isinstance(m, dict) else str(m) for m in lst]
        except Exception as e:
            fallback2_result = {
                "key_ok": False,
                "message": f"Could not reach Model 2 relay at {m2_base}: {e}",
                "models": {"model": False},
            }
            key_ok = False
        else:
            def valid2(name):
                if not name:
                    return True
                n = name.strip().lower()
                return any(a.lower() == n for a in available2) or any(n in a.lower() for a in available2)
            m2_model_ok = valid2(m2_model)
            if not m2_model_ok:
                fallback2_result = {
                    "key_ok": True,
                    "message": f"Model 2 '{m2_model}' not found on its relay (cleared)",
                    "models": {"model": False},
                    "chat_ok": False,
                    "chat_message": f"Model 2 '{m2_model}' not found on its relay",
                }
            else:
                fallback2_result = {
                    "key_ok": True,
                    "models": {"model": m2_model_ok},
                    "available_count": len(available2),
                    "message": "OK",
                    "chat_ok": chat_model_2,
                    "chat_message": chat_msg_2,
                }
    if m2_separate and m2_model and fallback2_result.get("models", {}).get("model"):
        try:
            reply = _chat_completions_sync(m2_base, m2_key, m2_model)
            chat_model_2 = bool(reply)
            if not chat_model_2:
                chat_msg_2 = f"Model 2 '{m2_model}' answered with empty content — check key/quota"
        except Exception as e:
            chat_model_2 = False
            chat_msg_2 = f"Model 2 '{m2_model}' failed the test query: {e}"
            sugg2 = next((a for a in available2 if a.lower() == m2_model.lower()), None)
            if sugg2 and sugg2 != m2_model:
                chat_suggested_2 = sugg2
                chat_msg_2 += f" Did you mean '{sugg2}'? Model ids are case-sensitive."
        fallback2_result["chat_ok"] = chat_model_2
        fallback2_result["chat_message"] = chat_msg_2
        if chat_suggested_2:
            fallback2_result["chat_suggested"] = chat_suggested_2
    elif m2_model and m2_ok:
        try:
            reply = _chat_completions_sync(base, key, m2_model)
            chat_model_2 = bool(reply)
            if not chat_model_2:
                chat_msg_2 = f"Model 2 '{m2_model}' answered with empty content — check key/quota"
        except Exception as e:
            chat_model_2 = False
            chat_msg_2 = f"Model 2 '{m2_model}' failed the test query: {e}"
            sugg2 = next((a for a in available if a.lower() == m2_model.lower()), None)
            if sugg2 and sugg2 != m2_model:
                chat_suggested_2 = sugg2
                chat_msg_2 += f" Did you mean '{sugg2}'? Model ids are case-sensitive."
        fallback2_result = {"key_ok": True, "chat_ok": chat_model_2, "chat_message": chat_msg_2}
        if chat_suggested_2:
            fallback2_result["chat_suggested"] = chat_suggested_2

    return {
        "key_ok": key_ok,
        "models": {"model": m1_ok, "model_2": m2_ok},
        "chat_ok": chat_model,
        "chat_message": chat_msg,
        "chat_suggested": chat_suggested,
        "available_count": len(available),
        "message": message,
        "fallback_2": fallback2_result if fallback2_result else None,
    }


def _check_relay_health_bg():
    """Run the proactive relay/model health check."""
    try:
        result = _config_health_check_sync({})
    except Exception as e:
        logger.warning(f"relay health check itself failed: {e}")
        return
    result["checked_at"] = _dt.utcnow().isoformat()
    _relay_health_cache.clear()
    _relay_health_cache.update(result)
    if not result.get("key_ok"):
        logger.warning(f"relay health check: {result.get('message')}")
    elif not all(result.get("models", {}).values()):
        logger.warning(f"relay health check: {result.get('message')}")
    elif result.get("chat_ok") is False:
        logger.warning(f"relay health check: {result.get('chat_message')}")
    else:
        logger.info("relay health check: OK")
