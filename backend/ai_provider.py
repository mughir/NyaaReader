"""
Centralized AI Provider Client and Rule Engine.

All outbound HTTP calls to external AI providers (OpenCode, OpenRouter, OpenAI, etc.)
route through `call_ai_provider` in this module. Provider-specific customization rules
(such as required session affinity headers, attribution headers, User-Agent, and payload tweaks)
are maintained centrally here to easily accommodate future AI provider requirements.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Iterator, List, Optional, Type
from urllib.parse import urlparse

logger = logging.getLogger("novel-reader.ai_provider")

OPENCODE_SESSION_HEADER = "x-opencode-session"


# ==============================================================================
# Provider Rules
# ==============================================================================

class BaseAIProviderRule:
    """Base class for provider-specific customizations."""
    name: str = "generic"

    @classmethod
    def matches(cls, base_url: str) -> bool:
        """Return True if this rule applies to the given base_url."""
        return True

    @classmethod
    def build_headers(
        cls,
        base_url: str,
        api_key: Optional[str] = None,
        session_id: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Construct HTTP headers for this provider."""
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
            "User-Agent": "HermesAgent/3.1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    @classmethod
    def transform_payload(cls, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Hook to adjust payload for provider idiosyncrasies."""
        return payload


class OpenCodeProviderRule(BaseAIProviderRule):
    """
    OpenCode (opencode.ai Zen / Go / Free) provider rule.

    Requirements:
    - User-Agent / Referer identifying client.
    - 'x-opencode-session' header on EVERY request (starting 09/06) for
      backend affinity and prompt cache warming. Requests missing it may error.
    """
    name: str = "opencode"

    @classmethod
    def matches(cls, base_url: str) -> bool:
        return is_opencode_endpoint(base_url)

    @classmethod
    def build_headers(
        cls,
        base_url: str,
        api_key: Optional[str] = None,
        session_id: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = super().build_headers(
            base_url, api_key=api_key, session_id=session_id, extra_headers=extra_headers
        )
        # Required by OpenCode for backend routing / prompt cache affinity
        headers[OPENCODE_SESSION_HEADER] = session_id or f"nyaa-{uuid.uuid4().hex[:16]}"
        return headers


class OpenRouterProviderRule(BaseAIProviderRule):
    """OpenRouter (openrouter.ai) provider rule."""
    name: str = "openrouter"

    @classmethod
    def matches(cls, base_url: str) -> bool:
        if not base_url:
            return False
        return "openrouter.ai" in base_url.lower()

    @classmethod
    def build_headers(
        cls,
        base_url: str,
        api_key: Optional[str] = None,
        session_id: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = super().build_headers(
            base_url, api_key=api_key, session_id=session_id, extra_headers=extra_headers
        )
        headers["HTTP-Referer"] = "https://github.com/mughir/NyaaReader"
        headers["X-Title"] = "NyaaReader"
        return headers


# Ordered list of provider rules to check (first match wins)
_PROVIDER_RULES: List[Type[BaseAIProviderRule]] = [
    OpenCodeProviderRule,
    OpenRouterProviderRule,
    BaseAIProviderRule,  # fallback generic rule
]


def register_provider_rule(rule_class: Type[BaseAIProviderRule]) -> None:
    """Register a custom provider rule at the top of the chain for future providers."""
    _PROVIDER_RULES.insert(0, rule_class)


def get_provider_rule(base_url: str) -> Type[BaseAIProviderRule]:
    """Find the matching provider rule for base_url."""
    clean = (base_url or "").strip()
    for rule in _PROVIDER_RULES:
        if rule.matches(clean):
            return rule
    return BaseAIProviderRule


def is_opencode_endpoint(url: Optional[str]) -> bool:
    """True when url addresses the OpenCode relay (opencode.ai)."""
    if not url:
        return False
    try:
        raw = str(url).strip()
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        return host == "opencode.ai" or host.endswith(".opencode.ai")
    except Exception:
        return "opencode.ai" in str(url).lower()


def build_relay_headers(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    session_id: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Construct appropriate request headers using the active provider rule."""
    rule = get_provider_rule(base_url or "")
    return rule.build_headers(
        base_url=base_url or "",
        api_key=api_key,
        session_id=session_id,
        extra_headers=extra_headers,
    )


# ==============================================================================
# Centralized API Call Function
# ==============================================================================

def call_ai_provider(
    base_url: str,
    endpoint: str = "/chat/completions",
    api_key: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    timeout: int = 180,
    stream: bool = False,
    method: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    """
    Single centralized gateway for all outbound HTTP requests to AI providers.

    All translation generations, health checks, model inquiries, and streaming calls
    pass through this function. It automatically selects the corresponding provider rule,
    applies provider headers (e.g. x-opencode-session), transforms the payload if needed,
    and executes via urllib.request.
    """
    clean_base = (base_url or "").strip().rstrip("/")
    clean_endpoint = (endpoint or "").strip()
    if clean_endpoint and not clean_endpoint.startswith("/"):
        clean_endpoint = "/" + clean_endpoint
    url = f"{clean_base}{clean_endpoint}"

    rule = get_provider_rule(clean_base)
    headers = rule.build_headers(
        base_url=clean_base,
        api_key=api_key,
        session_id=session_id,
        extra_headers=extra_headers,
    )
    transformed_payload = rule.transform_payload(payload)

    resolved_method = method
    if resolved_method is None:
        resolved_method = "POST" if transformed_payload is not None else "GET"

    data_bytes = None
    if transformed_payload is not None:
        data_bytes = json.dumps(transformed_payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers=headers,
        method=resolved_method,
    )

    if stream:
        return _execute_stream(req, timeout=timeout)
    else:
        return _execute_sync(req, timeout=timeout)


def _execute_sync(req: urllib.request.Request, timeout: int) -> Any:
    """Execute synchronous request and parse JSON response."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        if not body.strip():
            return {}
        return json.loads(body)


def _execute_stream(req: urllib.request.Request, timeout: int) -> Iterator[str]:
    """Execute streaming request and yield SSE text deltas."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            line_str = line.decode("utf-8").strip()
            if not line_str or not line_str.startswith("data:"):
                continue
            data_str = line_str[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk_json = json.loads(data_str)
                delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except Exception:
                continue