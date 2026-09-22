"""Bearer token verifier for JEV_MCP_TOKEN.

TDD — define behavior before implementation.
"""
import pytest

from jev_mcp.bearer import BearerTokenVerifier, load_bearer_token


class TestBearerTokenVerifier:
    async def test_correct_token_returns_access_token(self):
        from mcp.server.auth.provider import AccessToken

        v = BearerTokenVerifier(expected_token="secret-abc")
        result = await v.verify_token("secret-abc")
        assert result is not None
        assert result.token == "secret-abc"

    async def test_wrong_token_returns_none(self):
        v = BearerTokenVerifier(expected_token="secret-abc")
        result = await v.verify_token("wrong")
        assert result is None

    async def test_empty_token_rejected(self):
        v = BearerTokenVerifier(expected_token="secret-abc")
        assert await v.verify_token("") is None

    async def test_case_sensitive(self):
        v = BearerTokenVerifier(expected_token="Secret-Abc")
        assert await v.verify_token("secret-abc") is None

    async def test_timing_safe_comparison(self):
        # Just verify no exception with equal-length vs unequal-length tokens
        v = BearerTokenVerifier(expected_token="a" * 64)
        assert await v.verify_token("b" * 64) is None
        assert await v.verify_token("a" * 64) is not None


class TestLoadBearerToken:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        # Env var is read first
        monkeypatch.setenv("JEV_MCP_TOKEN", "from-env")
        # Even if file exists, env wins
        token_file = tmp_path / "token"
        token_file.write_text("from-file")
        token_file.chmod(0o600)
        monkeypatch.setattr("jev_mcp.bearer._TOKEN_FILE", str(token_file))
        assert load_bearer_token() == "from-env"

    def test_falls_back_to_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JEV_MCP_TOKEN", raising=False)
        token_file = tmp_path / "token"
        token_file.write_text("from-file\n")
        token_file.chmod(0o600)
        monkeypatch.setattr("jev_mcp.bearer._TOKEN_FILE", str(token_file))
        assert load_bearer_token() == "from-file"

    def test_no_token_anywhere_returns_none(self, monkeypatch):
        monkeypatch.delenv("JEV_MCP_TOKEN", raising=False)
        monkeypatch.setattr("jev_mcp.bearer._TOKEN_FILE", "/nonexistent/path/token")
        assert load_bearer_token() is None