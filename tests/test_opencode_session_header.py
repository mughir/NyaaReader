"""
Tests for centralized AI provider call gateway and OpenCode session affinity header.

Verifies:
1. Target detection for OpenCode (opencode.ai).
2. Header builder applying 'x-opencode-session' on OpenCode and preserving custom session IDs.
3. Centralized call_ai_provider gateway routing all calls (sync & streaming).
4. Extensible provider rule engine for future AI provider rules.
5. OpenAIRelayTranslator and config_service requests carrying x-opencode-session when using OpenCode.
"""

import json
import urllib.request
from typing import Dict, Optional

import pytest

from ai_provider import (
    BaseAIProviderRule,
    OPENCODE_SESSION_HEADER,
    build_relay_headers,
    call_ai_provider,
    get_provider_rule,
    is_opencode_endpoint,
    register_provider_rule,
)
from translator import OpenAIRelayTranslator


class TestOpenCodeTargetDetection:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://opencode.ai/zen/go/v1", True),
            ("https://opencode.ai/zen/v1", True),
            ("http://opencode.ai/v1", True),
            ("https://api.opencode.ai/v1", True),
            ("opencode.ai", True),
            ("https://api.openai.com/v1", False),
            ("https://openrouter.ai/api/v1", False),
            ("https://api.deepseek.com/v1", False),
            ("http://localhost:11434/v1", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_opencode_endpoint(self, url, expected):
        assert is_opencode_endpoint(url) is expected


class TestRelayHeaders:
    def test_opencode_headers_contain_session(self):
        headers = build_relay_headers(
            base_url="https://opencode.ai/zen/go/v1",
            api_key="test-key",
            session_id="nyaa-novel-123",
        )
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"] == "HermesAgent/3.1.0"
        assert headers[OPENCODE_SESSION_HEADER] == "nyaa-novel-123"

    def test_opencode_headers_generate_default_session(self):
        headers = build_relay_headers(
            base_url="https://opencode.ai/zen/go/v1",
            api_key="test-key",
        )
        assert OPENCODE_SESSION_HEADER in headers
        assert headers[OPENCODE_SESSION_HEADER].startswith("nyaa-")

    def test_non_opencode_headers_omit_session(self):
        headers = build_relay_headers(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            session_id="nyaa-novel-123",
        )
        assert OPENCODE_SESSION_HEADER not in headers
        assert headers["Authorization"] == "Bearer test-key"


class TestCentralizedCallAiProvider:
    def test_sync_call_invokes_urllib_with_provider_rules(self, monkeypatch):
        captured_req = None

        def fake_urlopen(req, timeout=None):
            nonlocal captured_req
            captured_req = req
            body = json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode("utf-8")

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return body

            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        res = call_ai_provider(
            base_url="https://opencode.ai/zen/go/v1",
            endpoint="/chat/completions",
            api_key="sk-test",
            payload={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
            session_id="sess-custom-99",
            timeout=30,
        )

        assert res["choices"][0]["message"]["content"] == "hello"
        assert captured_req is not None
        assert captured_req.full_url == "https://opencode.ai/zen/go/v1/chat/completions"
        assert captured_req.headers["Authorization"] == "Bearer sk-test"
        assert captured_req.headers["X-opencode-session"] == "sess-custom-99"

    def test_stream_call_yields_deltas(self, monkeypatch):
        captured_req = None

        def fake_urlopen(req, timeout=None):
            nonlocal captured_req
            captured_req = req
            lines = [
                b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
                b'data: {"choices": [{"delta": {"content": " world"}}]}\n',
                b"data: [DONE]\n",
            ]

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def __iter__(self):
                    return iter(lines)

            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        stream = call_ai_provider(
            base_url="https://opencode.ai/zen/go/v1",
            endpoint="/chat/completions",
            api_key="sk-test",
            payload={"model": "deepseek-v4-flash", "messages": []},
            session_id="sess-stream-1",
            stream=True,
        )
        tokens = list(stream)

        assert tokens == ["Hello", " world"]
        assert captured_req.headers["X-opencode-session"] == "sess-stream-1"


class TestExtensibleProviderRules:
    def test_custom_provider_rule_registration(self):
        class MockProviderRule(BaseAIProviderRule):
            name = "mock_provider"

            @classmethod
            def matches(cls, base_url: str) -> bool:
                return "mock-ai.internal" in base_url

            @classmethod
            def build_headers(cls, base_url, api_key=None, session_id=None, extra_headers=None):
                h = super().build_headers(base_url, api_key, session_id, extra_headers)
                h["x-custom-rule"] = "rule-applied"
                return h

        register_provider_rule(MockProviderRule)

        rule = get_provider_rule("https://mock-ai.internal/v1")
        assert rule == MockProviderRule

        headers = build_relay_headers("https://mock-ai.internal/v1", api_key="test-key")
        assert headers["x-custom-rule"] == "rule-applied"


class TestOpenAIRelayTranslatorSessionHeader:
    def test_translator_generate_sends_session_header(self, monkeypatch):
        captured_req = None

        def fake_urlopen(req, timeout=None):
            nonlocal captured_req
            captured_req = req
            body = json.dumps({"choices": [{"message": {"content": "Translated text"}}]}).encode("utf-8")

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return body

            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        translator = OpenAIRelayTranslator(
            api_key="test-key",
            model="deepseek-v4-flash",
            base_url="https://opencode.ai/zen/go/v1",
            session_id="initial-sess",
        )

        res = translator._generate("Translate this", session_id="nyaa-novel-777")
        assert res == "Translated text"
        assert captured_req.headers["X-opencode-session"] == "nyaa-novel-777"


class TestConfigServiceHealthCheckHeaders:
    def test_config_health_check_sends_session_header_to_opencode(self, monkeypatch):
        from services.config_service import _chat_completions_sync, _config_health_check_sync

        requests = []

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            if req.full_url.endswith("/models"):
                body = json.dumps({"data": [{"id": "deepseek-v4-flash"}]}).encode("utf-8")
            else:
                body = json.dumps({"choices": [{"message": {"content": "hi there"}}]}).encode("utf-8")

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return body

            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        # Test chat query
        reply = _chat_completions_sync("https://opencode.ai/zen/go/v1", "test-key", "deepseek-v4-flash")
        assert reply == "hi there"
        assert any("X-opencode-session" in r.headers for r in requests)

        requests.clear()

        # Test health check probe
        hc = _config_health_check_sync({
            "api_key": "test-key",
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-v4-flash",
        })
        assert hc["key_ok"] is True
        assert len(requests) > 0
        for r in requests:
            assert "X-opencode-session" in r.headers
