"""Explicit proxy support (JEV_API_PROXY) + transport-error resilience.

Context (2026-10-21): direct connections to api.typesafe.ai started returning
HTTP 451 ("Typesafe is not available in your region") from this host's egress
IP. The only working path is the local Clash mixed port (127.0.0.1:7897).

The pre-existing proxy_bypass test keeps `trust_env=False` (env proxies are
still ignored by accident-proofing), so the proxy must be opt-in and explicit:
JevClient(proxy=...) → httpx.AsyncClient(proxy=...).

That path is intermittently flaky (~2% ConnectError), so transport errors are
now retried in both send() and probe().
"""
import inspect

import httpx
import pytest
import respx

from jev_mcp import jev_client
from jev_mcp.jev_client import JevClient

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MODELS_URL = "https://api.typesafe.ai/v1/models"


class TestExplicitProxy:
    def test_send_passes_explicit_proxy(self):
        source = inspect.getsource(jev_client.JevClient.send)
        assert "proxy=self.proxy" in source, (
            "JevClient.send must forward the explicitly configured proxy to httpx."
        )

    def test_probe_passes_explicit_proxy(self):
        source = inspect.getsource(jev_client.JevClient.probe)
        assert "proxy=self.proxy" in source

    def test_proxy_defaults_to_none(self):
        c = JevClient(api_key="k")
        assert c.proxy is None

    def test_proxy_is_stored(self):
        c = JevClient(api_key="k", proxy="http://127.0.0.1:7897")
        assert c.proxy == "http://127.0.0.1:7897"

    def test_empty_proxy_becomes_none(self):
        c = JevClient(api_key="k", proxy="")
        assert c.proxy is None

    def test_server_reads_jev_api_proxy_env(self):
        from jev_mcp import server

        source = inspect.getsource(server.init)
        assert 'os.environ.get("JEV_API_PROXY")' in source, (
            "init() must read the proxy from the JEV_API_PROXY env var."
        )


class TestTransportErrorRetry:
    @respx.mock
    async def test_send_retries_transport_error(self):
        """A transient ConnectError must not fail the call (proxy path is flaky)."""
        c = JevClient(api_key="k", proxy="http://127.0.0.1:7897")
        route = respx.post(JEV_URL)
        route.side_effect = [
            httpx.ConnectError("connection failed"),
            httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"answer": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 50, "output_tokens": 5},
                },
            ),
        ]
        result = await c.send(state="x", questions={"answer": {"type": "noul"}})
        assert result["answers"]["answer"]["noul"] == 0.9
        assert route.call_count == 2

    @respx.mock
    async def test_send_raises_api_error_when_transport_keeps_failing(self):
        c = JevClient(api_key="k", max_retries=1, proxy="http://127.0.0.1:7897")
        respx.post(JEV_URL).mock(side_effect=httpx.ConnectError("nope"))
        with pytest.raises(jev_client.JevAPIError) as exc:
            await c.send(state="x", questions={"answer": {"type": "noul"}})
        assert exc.value.status == 0
        assert "transport error" in exc.value.body

    @respx.mock
    async def test_probe_retries_transport_error(self):
        c = JevClient(api_key="k", proxy="http://127.0.0.1:7897")
        route = respx.get(MODELS_URL)
        route.side_effect = [
            httpx.ConnectError("connection failed"),
            httpx.Response(200, json={"models": ["jev-1.13.0"]}),
        ]
        assert await c.probe() is True
        assert route.call_count == 2

    @respx.mock
    async def test_probe_returns_false_after_exhausting_retries(self):
        c = JevClient(api_key="k", proxy="http://127.0.0.1:7897")
        respx.get(MODELS_URL).mock(side_effect=httpx.ConnectError("nope"))
        assert await c.probe() is False
