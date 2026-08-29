"""
_auth_enabled_cached() runs on EVERY request via the auth_guard middleware.
The old code defaulted to `False` ("auth disabled") on ANY exception from the
underlying DB read — a transient DB error, not an actual configuration
change, silently dropped the login requirement for every request in the next
5-second cache window, even with a password configured. A security gate must
fail CLOSED (require login) on an error, never open.
"""
import main as app_module


class TestAuthFailsClosedOnError:
    def test_a_transient_error_does_not_disable_auth(self, monkeypatch):
        app_module._auth_flag_cache["value"] = True
        app_module._auth_flag_cache["ts"] = 0.0  # force a re-check

        def boom():
            raise RuntimeError("simulated transient DB error")
        monkeypatch.setattr(app_module, "_auth_enabled", boom)

        result = app_module._auth_enabled_cached()

        assert result is True, \
            "an error checking auth state must never be treated as 'auth is disabled'"

    def test_an_error_on_the_very_first_check_ever_still_requires_login(self, monkeypatch):
        """Cold start: nothing has ever been read successfully yet."""
        app_module._auth_flag_cache["value"] = None
        app_module._auth_flag_cache["ts"] = 0.0

        def boom():
            raise RuntimeError("simulated DB error at startup")
        monkeypatch.setattr(app_module, "_auth_enabled", boom)

        result = app_module._auth_enabled_cached()

        assert result is True, "with no prior known-good state, an error must default to REQUIRING login"

    def test_a_successful_read_still_updates_the_cached_value(self, monkeypatch):
        app_module._auth_flag_cache["value"] = True
        app_module._auth_flag_cache["ts"] = 0.0
        monkeypatch.setattr(app_module, "_auth_enabled", lambda *a, **k: False)

        assert app_module._auth_enabled_cached() is False

    def test_a_failed_check_leaves_ts_stale_so_the_next_request_retries(self, monkeypatch):
        app_module._auth_flag_cache["value"] = True
        app_module._auth_flag_cache["ts"] = 0.0
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("still down")
        monkeypatch.setattr(app_module, "_auth_enabled", boom)

        app_module._auth_enabled_cached()
        app_module._auth_enabled_cached()

        assert len(calls) == 2, \
            "a failed check must not be cached for the normal 5s window — retry immediately so recovery is fast"
