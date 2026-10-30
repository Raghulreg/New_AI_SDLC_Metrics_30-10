"""Gemini provider tests use mocked HTTP only; no real provider credential is used."""
import json

import httpx
import pytest
import respx

from app.config import Settings
from app.errors import SourceError, Status
from app.llm.client import LlmClient

GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_URL = f"{GEMINI_BASE}/v1beta/models/gemini-test:generateContent"
TEST_GEMINI_KEY = "gemini-test-key-not-real"
MESSAGES = [
    {"role": "system", "content": "Return JSON only."},
    {"role": "user", "content": "Classify hunk h1."},
]


def settings(**overrides) -> Settings:
    return Settings(llm_code_analysis_enabled=True, llm_provider="gemini", llm_model="gemini-test",
                    llm_api_key=TEST_GEMINI_KEY, **overrides)


@respx.mock
async def test_gemini_uses_official_generate_content_json_api():
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": '{"hunks":[]}'}, {"text": "\n"}]}}]}))
    async with httpx.AsyncClient() as http:
        content = await LlmClient(settings(), http).complete_json(MESSAGES)
    assert content == '{"hunks":[]}\n'
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["x-goog-api-key"] == TEST_GEMINI_KEY
    assert body["systemInstruction"]["parts"][0]["text"] == "Return JSON only."
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Classify hunk h1."}]}]
    assert body["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}


@respx.mock
async def test_gemini_allows_configured_endpoint_with_v1beta_once():
    route = respx.post("https://gemini.internal/v1beta/models/gemini-test:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}))
    async with httpx.AsyncClient() as http:
        assert await LlmClient(settings(llm_endpoint="https://gemini.internal/v1beta"), http).complete_json(MESSAGES) == "{}"
    assert route.call_count == 1


@respx.mock
async def test_gemini_invalid_key_is_safe_auth_error():
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(
        400, json={"error": {"message": "API key not valid. Please pass a valid API key."}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError) as error:
            await LlmClient(settings(), http).complete_json(MESSAGES)
    assert error.value.status == Status.AUTH_ERROR and error.value.http_status == 400
    assert error.value.message == "Gemini API key was rejected"
    assert TEST_GEMINI_KEY not in str(error.value)


@respx.mock
async def test_gemini_quota_and_malformed_response_are_safe_errors():
    route = respx.post(GEMINI_URL).mock(side_effect=[
        httpx.Response(429, json={"error": {"message": "Quota exceeded"}}),
        httpx.Response(429, json={"error": {"message": "Quota exceeded"}}),
        httpx.Response(429, json={"error": {"message": "Quota exceeded"}}),
        httpx.Response(200, json={"candidates": []}),
    ])
    async with httpx.AsyncClient() as http:
        client = LlmClient(settings(), http)
        with pytest.raises(SourceError) as quota:
            await client.complete_json(MESSAGES)
        with pytest.raises(SourceError) as malformed:
            await client.complete_json(MESSAGES)
    assert quota.value.status == Status.ERROR and "Rate limit exceeded" in quota.value.message
    assert malformed.value.status == Status.ERROR and "Unexpected response shape" in malformed.value.message
    assert route.call_count == 4  # shared HTTP client retries 429 twice before surfacing it


def test_gemini_is_configured_only_when_enabled_model_and_key_exist():
    assert not Settings(llm_provider="gemini", llm_model="gemini-test", llm_api_key=TEST_GEMINI_KEY).llm_configured
    assert not Settings(llm_code_analysis_enabled=True, llm_provider="gemini", llm_api_key=TEST_GEMINI_KEY).llm_configured
    assert settings().llm_configured
