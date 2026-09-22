"""JevClient: httpx wrapper for /v1/systemone with tenacity retry.

Tests use respx to mock httpx routes — no real API calls.

TDD — these tests define the behavior before implementation.
"""
import httpx
import pytest
import respx

from jev_mcp.jev_client import JevAPIError, JevClient


JEV_URL = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def client() -> JevClient:
    return JevClient(
        api_key="test-key",
        base_url="https://api.typesafe.ai",
        model_version="jev-1.13.0",
    )


class TestFormatRequest:
    """Server-format request body per docs.typesafe.ai/api.md."""

    def test_basic_choice(self, client: JevClient):
        req = client.format_request(
            state="hello",
            questions={
                "department": {
                    "type": "choice",
                    "instructions": "Which team?",
                    "criteria": {"a": "first", "b": "second"},
                }
            },
        )
        assert req["state"] == "hello"
        assert req["model"] == "jev-1.13.0"
        assert "department" in req["questions"]
        assert req["questions"]["department"]["type"] == "choice"

    def test_multiple_questions(self, client: JevClient):
        req = client.format_request(
            state={"x": 1},
            questions={
                "q1": {"type": "noul"},
                "q2": {"type": "score", "criteria": ["a", "b"]},
            },
        )
        assert set(req["questions"].keys()) == {"q1", "q2"}

    def test_model_is_pinned(self, client: JevClient):
        # Even if caller passes model, it should be overridden
        req = client.format_request(
            state="x",
            questions={"q": {"type": "noul"}},
            model_override="some-other-model",
        )
        assert req["model"] == "jev-1.13.0"

    def test_dict_state_passed_through(self, client: JevClient):
        req = client.format_request(
            state={"command": "rm -rf /"},
            questions={"q": {"type": "noul"}},
        )
        assert req["state"] == {"command": "rm -rf /"}


class TestSend:
    """POST /v1/systemone with retry on 429/529, no retry on 4xx."""

    @respx.mock
    async def test_success(self, client: JevClient):
        respx.post(JEV_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {
                        "q": {"type": "noul", "noul": 0.9}
                    },
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                },
            )
        )
        result = await client.send(
            state="hi",
            questions={"q": {"type": "noul", "instructions": "yes?"}},
        )
        assert result["answers"]["q"]["noul"] == 0.9
        assert result["usage"]["input_tokens"] == 100

    @respx.mock
    async def test_authorization_header(self, client: JevClient):
        route = respx.post(JEV_URL).mock(
            return_value=httpx.Response(
                200,
                json={"model": "m", "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}},
            )
        )
        await client.send(state="x", questions={"q": {"type": "noul"}})
        assert route.calls.last.request.headers["authorization"] == "Bearer test-key"

    @respx.mock
    async def test_retry_on_429(self, client: JevClient):
        route = respx.post(JEV_URL).mock(
            side_effect=[
                httpx.Response(429, json={"error": "rate limit"}),
                httpx.Response(
                    200,
                    json={
                        "model": "m",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                ),
            ]
        )
        result = await client.send(state="x", questions={"q": {"type": "noul"}})
        assert route.call_count == 2
        assert result["answers"]["q"]["noul"] == 0.5

    @respx.mock
    async def test_retry_on_529(self, client: JevClient):
        route = respx.post(JEV_URL).mock(
            side_effect=[
                httpx.Response(529, text="overloaded"),
                httpx.Response(
                    200,
                    json={
                        "model": "m",
                        "answers": {"q": {"type": "noul", "noul": 0.7}},
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                ),
            ]
        )
        result = await client.send(state="x", questions={"q": {"type": "noul"}})
        assert route.call_count == 2

    @respx.mock
    async def test_no_retry_on_4xx_other(self, client: JevClient):
        # 401 should NOT be retried
        route = respx.post(JEV_URL).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(JevAPIError) as exc_info:
            await client.send(state="x", questions={"q": {"type": "noul"}})
        assert route.call_count == 1
        assert exc_info.value.status == 401

    @respx.mock
    async def test_422_raises(self, client: JevClient):
        respx.post(JEV_URL).mock(
            return_value=httpx.Response(422, json={"error": "unprocessable"})
        )
        with pytest.raises(JevAPIError) as exc_info:
            await client.send(state="x", questions={"q": {"type": "noul"}})
        assert exc_info.value.status == 422

    @respx.mock
    async def test_retry_gives_up_after_3(self, client: JevClient):
        # tenacity default for our client is 3 retries → 4 total attempts
        route = respx.post(JEV_URL).mock(
            return_value=httpx.Response(429, json={"error": "rate"})
        )
        with pytest.raises(JevAPIError):
            await client.send(state="x", questions={"q": {"type": "noul"}})
        # 4 total attempts (1 + 3 retries)
        assert route.call_count == 4


class TestProbe:
    """Probe: cheap GET /v1/models to verify API reachable + key valid (no inference cost)."""

    @respx.mock
    async def test_success(self, client: JevClient):
        route = respx.get("https://api.typesafe.ai/v1/models").mock(
            return_value=httpx.Response(200, json={"models": ["jev-1.13.0"]})
        )
        ok = await client.probe()
        assert ok is True
        assert route.call_count == 1

    @respx.mock
    async def test_authorization_header_sent(self, client: JevClient):
        route = respx.get("https://api.typesafe.ai/v1/models").mock(
            return_value=httpx.Response(200, json={})
        )
        await client.probe()
        assert route.calls.last.request.headers["authorization"] == "Bearer test-key"

    @respx.mock
    async def test_401_returns_false(self, client: JevClient):
        respx.get("https://api.typesafe.ai/v1/models").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        ok = await client.probe()
        assert ok is False

    @respx.mock
    async def test_network_error_returns_false(self, client: JevClient):
        respx.get("https://api.typesafe.ai/v1/models").mock(
            side_effect=httpx.ConnectError("network down")
        )
        ok = await client.probe()
        assert ok is False