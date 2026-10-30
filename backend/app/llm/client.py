from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from ..config import Settings
from ..errors import SourceError, Status
from ..http import RateLimiter, request_json

KeyResolver = Callable[[], Awaitable[str]]


class LlmClient:
    """JSON-only attribution client for Azure OpenAI, OpenAI, or the official Gemini REST API."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, key_resolver: KeyResolver | None = None):
        self.s = settings
        self.http = http
        self._key_resolver = key_resolver
        self._key: str | None = None
        self._key_lock = asyncio.Lock()
        self._limiter = RateLimiter(max(1, settings.llm_requests_per_minute))

    @property
    def configured(self) -> bool:
        return self.s.llm_configured

    @property
    def model_id(self) -> str:
        return f"{self.s.llm_provider}:{self.s.llm_model}"

    async def _api_key(self) -> str:
        async with self._key_lock:
            if self._key is None:
                if self.s.llm_api_key:
                    self._key = self.s.llm_api_key
                elif self._key_resolver is not None:
                    self._key = await self._key_resolver()
                else:
                    raise SourceError(Status.NOT_CONFIGURED,
                                      "LLM API key not configured (LLM_API_KEY or Key Vault secret)", "llm")
        return self._key

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        if not self.configured:
            raise SourceError(Status.NOT_CONFIGURED, "LLM code analysis is not enabled or not configured", "llm")
        key = await self._api_key()
        base = self.s.llm_endpoint_url
        if self.s.llm_provider == "azure_openai":
            body: dict[str, Any] = {"messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
            url = f"{base}/openai/deployments/{quote(self.s.llm_model, safe='')}/chat/completions"
            params = {"api-version": self.s.llm_api_version}
            headers = {"api-key": key}
            data, _ = await request_json(self.http, "POST", url, source="llm", params=params, json=body,
                                         headers=headers, limiter=self._limiter)
            return self._openai_content(data)
        if self.s.llm_provider == "openai":
            body = {"messages": messages, "temperature": 0, "response_format": {"type": "json_object"},
                    "model": self.s.llm_model}
            url = f"{base}/chat/completions"
            params = None
            headers = {"Authorization": f"Bearer {key}"}
            data, _ = await request_json(self.http, "POST", url, source="llm", params=params, json=body,
                                         headers=headers, limiter=self._limiter)
            return self._openai_content(data)
        return await self._gemini_json(base, key, messages)

    @staticmethod
    def _openai_content(data: Any) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SourceError(Status.ERROR, "Unexpected response shape from chat completions", "llm") from None
        if not isinstance(content, str):
            raise SourceError(Status.ERROR, "Chat completion returned no text content", "llm")
        return content

    async def _gemini_json(self, base: str, key: str, messages: list[dict[str, str]]) -> str:
        """Call Gemini generateContent directly so the shared HTTP timeout/rate/error handling remains in force."""
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        contents = [{"role": "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m.get("role") != "system"]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        version_base = base if base.rstrip("/").endswith("/v1beta") else f"{base}/v1beta"
        url = f"{version_base}/models/{quote(self.s.llm_model, safe='')}:generateContent"
        try:
            data, _ = await request_json(self.http, "POST", url, source="llm.gemini", json=body,
                                         headers={"x-goog-api-key": key}, limiter=self._limiter)
        except SourceError as e:
            # Gemini commonly returns HTTP 400 for an invalid API key. Do not echo provider text or the key.
            if e.http_status == 400 and "api key" in e.message.lower():
                raise SourceError(Status.AUTH_ERROR, "Gemini API key was rejected", "llm.gemini", e.http_status) from e
            raise
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(part["text"] for part in parts if isinstance(part.get("text"), str))
        except (KeyError, IndexError, TypeError):
            raise SourceError(Status.ERROR, "Unexpected response shape from Gemini generateContent", "llm.gemini") from None
        if not content:
            raise SourceError(Status.ERROR, "Gemini generateContent returned no text content", "llm.gemini")
        return content
