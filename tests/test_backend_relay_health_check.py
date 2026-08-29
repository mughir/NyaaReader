"""
Stage 3: relay/model health check now runs PROACTIVELY (on startup and after
every config save), not just when a user happens to open Settings and click
Save. Fixing that exposed a real pre-existing bug in the shared function
itself: _config_health_check_sync only ever read model/model_2 from the
payload it was passed, with NO env fallback (unlike key/base_url, which
already had one) -- so calling it with an empty payload (exactly what the new
proactive check does, to test whatever is ACTUALLY configured) silently
treated the model fields as blank, and a blank model always passes validation.
The model check was a complete no-op for any caller that didn't explicitly
supply model/model_2 -- which was every caller until now.
"""
import json
import io

import main as app_module
from database import SessionLocal
from models import AppConfig


def _fake_models_response(model_ids):
    body = json.dumps({"data": [{"id": m} for m in model_ids]}).encode()

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return body
    return _Resp()


class TestModelEnvFallback:
    def test_empty_payload_checks_the_env_configured_model_not_a_no_op(self, monkeypatch):
        monkeypatch.setenv("FALLBACK_MODEL", "totally-unknown-model")
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["deepseek-v4-flash", "gpt-5.6-luna"]))

        result = app_module._config_health_check_sync({})

        assert result["key_ok"] is True
        assert result["models"]["model"] is False, (
            "an env-configured model that isn't on the relay's list must be reported invalid, "
            "not silently skipped because the caller passed no explicit model")

    def test_empty_payload_accepts_a_model_that_is_on_the_relays_list(self, monkeypatch):
        monkeypatch.setenv("FALLBACK_MODEL", "deepseek-v4-flash")
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["deepseek-v4-flash"]))

        result = app_module._config_health_check_sync({})
        assert result["models"]["model"] is True

    def test_an_explicit_blank_model_is_still_always_valid_even_with_a_bad_env_value(self, monkeypatch):
        """The Settings save-time check sends model="" on purpose when the user
        clears the field -- that must keep meaning 'blank is fine', not get
        silently swapped for a leftover (possibly bad) env value."""
        monkeypatch.setenv("FALLBACK_MODEL", "totally-unknown-model")
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["deepseek-v4-flash"]))

        result = app_module._config_health_check_sync({"model": ""})
        assert result["models"]["model"] is True


class TestProactiveCheckCachesAndLogs(object):
    def test_check_relay_health_bg_populates_the_cache(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["deepseek-v4-flash"]))
        monkeypatch.setenv("FALLBACK_MODEL", "deepseek-v4-flash")

        app_module._check_relay_health_bg()

        assert app_module._relay_health_cache.get("checked_at") is not None
        assert app_module._relay_health_cache.get("key_ok") is True

    def test_a_failed_check_is_logged_as_a_warning(self, monkeypatch, caplog):
        import logging

        def boom(*a, **k):
            raise RuntimeError("relay unreachable")
        monkeypatch.setattr("urllib.request.urlopen", boom)

        with caplog.at_level(logging.WARNING, logger="novel-reader"):
            app_module._check_relay_health_bg()

        assert app_module._relay_health_cache.get("key_ok") is False
        assert "relay health check" in caplog.text


class TestHealthStatusEndpoint:
    def test_returns_the_cached_result(self, client, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["deepseek-v4-flash"]))
        monkeypatch.setenv("FALLBACK_MODEL", "deepseek-v4-flash")
        app_module._check_relay_health_bg()

        r = client.get("/api/config/health-status")
        assert r.status_code == 200
        assert r.json()["key_ok"] is True

    def test_before_any_check_has_run_it_reports_null_checked_at(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "_relay_health_cache", {})
        r = client.get("/api/config/health-status")
        assert r.json() == {"checked_at": None}


class TestPutConfigTriggersARecheck:
    def test_saving_config_refreshes_the_cached_health_status(self, client, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _fake_models_response(["some-model"]))
        app_module._relay_health_cache.clear()

        r = client.put("/api/config", json={"fallback_model": "some-model"})
        assert r.status_code == 200

        # TestClient runs BackgroundTasks before the request completes, so the
        # cache must already reflect the recheck by the time this returns.
        assert app_module._relay_health_cache.get("checked_at") is not None
