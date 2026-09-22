"""JevClient: POST to /v1/systemone with tenacity retry on 429/529.

Per docs.typesafe.ai/api.md:
- POST /v1/systemone, Authorization: Bearer <API_KEY>
- body: {"state", "model", "questions": {<name>: {type, instructions, criteria}}}
- model is hardcoded to jev-1.13.0; client overrides are ignored
- Retry 429 (rate limit) and 529 (overloaded) with exponential backoff
- 4xx validation errors (401, 422) propagate without retry
"""
from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


SYSTEM_ONE_PATH = "/v1/systemone"
MODELS_PATH = "/v1/models"
PINNED_MODEL_VERSION = "jev-1.13.0"


class JevAPIError(Exception):
    """Raised for non-retryable API errors or retry exhaustion."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Jev API error {status}: {body[:200]}")


class _RetryableStatus(Exception):
    """Internal: signals a retryable HTTP status to tenacity."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"retryable status {status}")


class JevClient:
    """Async HTTP client for Jev System One API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.typesafe.ai",
        model_version: str = PINNED_MODEL_VERSION,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.timeout = timeout
        self.max_retries = max_retries

    def format_request(
        self,
        state: Any,
        questions: dict[str, dict],
        model_override: str | None = None,
    ) -> dict:
        """Build request body. model is always pinned, override ignored."""
        return {
            "state": state,
            "model": self.model_version,  # always pin, ignore override
            "questions": questions,
        }

    async def send(self, state: Any, questions: dict[str, dict]) -> dict:
        """POST /v1/systemone. Returns parsed JSON body. Retries on 429/529."""
        body = self.format_request(state=state, questions=questions)
        url = f"{self.base_url}{SYSTEM_ONE_PATH}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Tenacity wrapper around the single-attempt function
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries + 1),
                wait=wait_exponential(multiplier=0.5, max=4.0),
                retry=retry_if_exception_type(_RetryableStatus),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                        resp = await client.post(url, json=body, headers=headers)
                    if resp.status_code in (429, 529):
                        raise _RetryableStatus(resp.status_code)
                    if resp.status_code >= 400:
                        raise JevAPIError(resp.status_code, resp.text)
                    return resp.json()
        except _RetryableStatus as e:
            # Retries exhausted on a retryable status → wrap as API error
            raise JevAPIError(e.status, f"retry exhausted: {e}")
        # Unreachable; type checkers need it
        raise JevAPIError(0, "retry exhausted")

    async def probe(self) -> bool:
        """Cheap connectivity + key validity check via GET /v1/models (no inference cost).

        Returns True if API responds 2xx; False on any error (auth, network, timeout).
        """
        url = f"{self.base_url}{MODELS_PATH}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(url, headers=headers)
            return 200 <= resp.status_code < 300
        except httpx.HTTPError:
            return False