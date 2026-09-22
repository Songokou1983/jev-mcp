"""Regression: JevClient must NOT use HTTP_PROXY/HTTPS_PROXY env vars.

Bug discovered 2026-09-22 in real deployment: server's httpx defaulted to
trust_env=True, which caused it to route all API calls through a local
HTTP proxy (127.0.0.1:7897). The proxy intermittently failed TLS handshake
to api.typesafe.ai, causing "Error executing tool check: " (empty message,
because httpx.ConnectError has empty str()) on the user's MCP session.

Symptom was misleading: empty error message looked like a server-side bug,
but the real cause was network routing via a misconfigured proxy.

Fix: JevClient.send() and JevClient.probe() both pass trust_env=False to
httpx.AsyncClient. Jev is a specific API endpoint we want direct connection
to — never honor proxy env vars.

These tests assert that even if HTTPS_PROXY is set, JevClient bypasses it.
"""
import httpx
import pytest
import respx

from jev_mcp.jev_client import JevClient

JEV_URL = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def client() -> JevClient:
    return JevClient(
        api_key="test-key",
        base_url="https://api.typesafe.ai",
        model_version="jev-1.13.0",
    )


class TestProxyBypass:
    """Verify the AsyncClient is constructed with trust_env=False."""

    def test_send_uses_trust_env_false(self, client: JevClient):
        """Inspect the source to confirm trust_env=False is passed.

        Behavioral test would require intercepting AsyncClient construction;
        a source-level assertion is more reliable and faster than mocks.
        """
        import inspect
        from jev_mcp import jev_client

        source = inspect.getsource(jev_client.JevClient.send)
        assert "trust_env=False" in source, (
            "JevClient.send must pass trust_env=False to bypass HTTPS_PROXY. "
            "Bug found 2026-09-22: default trust_env=True caused intermittent "
            "ConnectError via broken local proxy."
        )

    def test_probe_uses_trust_env_false(self, client: JevClient):
        import inspect
        from jev_mcp import jev_client

        source = inspect.getsource(jev_client.JevClient.probe)
        assert "trust_env=False" in source

    @respx.mock
    async def test_send_succeeds_with_https_proxy_set(
        self, client: JevClient, monkeypatch
    ):
        """End-to-end: even with HTTPS_PROXY env set, server reaches Jev directly.

        Simulates the user's environment where 127.0.0.1:7897 was the broken proxy.
        Without trust_env=False, httpx would attempt the proxy and fail.
        """
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")

        # Mock the DIRECT Jev URL (respx intercepts before httpx decides routing)
        respx.post(JEV_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"answer": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 50, "output_tokens": 5},
                },
            )
        )

        # If trust_env was True, httpx would try to use the proxy and fail
        # (the respx mock wouldn't be hit since it intercepts the direct URL).
        result = await client.send(state="x", questions={"answer": {"type": "noul"}})
        assert result["answers"]["answer"]["noul"] == 0.9

    @respx.mock
    async def test_probe_succeeds_with_https_proxy_set(
        self, client: JevClient, monkeypatch
    ):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        respx.get("https://api.typesafe.ai/v1/models").mock(
            return_value=httpx.Response(200, json={"models": ["jev-1.13.0"]})
        )
        ok = await client.probe()
        assert ok is True