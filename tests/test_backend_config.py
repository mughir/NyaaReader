"""
backend/main.py's /api/config: the masked-secret round-trip that truncated
the login password to its own last 4 characters, and the env-clearing bug
that left a revoked key live until restart.
"""
import os

import main as app_module


class TestMaskedSecretGuard:
    """GET /api/config masks every secret to its last 4 chars. Saving Settings
    must never accept that mask back as a "new value" — the frontend keeps
    it out of editable inputs, and the backend guards independently in case
    it ever does leak through."""

    def test_setting_a_password_then_reading_it_back_is_masked(self, client):
        r = client.put("/api/config", json={"auth_password": "hunter2secret"})
        assert r.status_code == 200

        cfg = client.get("/api/config").json()
        assert cfg["auth_password"] == "cret"
        assert cfg["auth_password_set"] is True

    def test_echoing_the_mask_back_does_not_truncate_the_password(self, client):
        client.put("/api/config", json={"auth_password": "hunter2secret"})
        mask = client.get("/api/config").json()["auth_password"]

        r = client.put("/api/config", json={"auth_password": mask, "backup_keep": 14})
        assert r.status_code == 200
        assert app_module._auth_password() == "hunter2secret", \
            "the 4-char mask must be recognised as an echo and ignored, not saved as the real password"

    def test_a_genuinely_new_password_still_applies(self, client):
        client.put("/api/config", json={"auth_password": "hunter2secret"})
        client.put("/api/config", json={"auth_password": "a-brand-new-password"})
        assert app_module._auth_password() == "a-brand-new-password"

    def test_clearing_the_password_works(self, client):
        client.put("/api/config", json={"auth_password": "something"})
        client.put("/api/config", json={"auth_password__clear": True})
        assert app_module._auth_password() == ""


class TestEnvClearedOnKeyRemoval:
    """Clearing a key in Settings must remove it from os.environ, or the
    revoked credential stays live (and get_translator() keeps using it)
    until the process restarts."""

    def test_clearing_a_key_removes_it_from_the_environment(self, client):
        client.put("/api/config", json={"fallback_api_key": "sk-live-secret-value"})
        assert os.environ.get("FALLBACK_API_KEY") == "sk-live-secret-value"

        client.put("/api/config", json={"fallback_api_key__clear": True})
        assert os.environ.get("FALLBACK_API_KEY") is None

    def test_a_merely_empty_field_does_not_wipe_a_dotenv_supplied_key(self, monkeypatch):
        """A key supplied only via .env (never saved through Settings) must
        keep working — _apply_config_to_env() must not treat "nothing to
        apply" the same as "the user asked to clear this"."""
        monkeypatch.setenv("FALLBACK_API_KEY", "from-dotenv")
        app_module._apply_config_to_env()  # the startup path: nothing "cleared"
        assert os.environ.get("FALLBACK_API_KEY") == "from-dotenv"
