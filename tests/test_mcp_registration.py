"""Verify FastMCP tool registration: names + descriptions.

The POSITIVE/NEGATIVE/EXAMPLE three-part description is the whole point of "tools
with attitude" — without it agents have no guidance on when to use which tool.

These tests verify that after register_tools(mcp), the FastMCP instance
exposes 6 tools with the expected names and non-empty POSITIVE/NEGATIVE/EXAMPLE
descriptions.
"""
import pytest
from mcp.server.fastmcp import FastMCP

from jev_mcp.server import register_tools


@pytest.fixture
def mcp() -> FastMCP:
    m = FastMCP("jev-mcp-test")
    register_tools(m)
    return m


def _tool_dict(mcp, name: str) -> dict:
    """Look up a registered tool by name. FastMCP stores them in _tool_manager."""
    return mcp._tool_manager._tools[name]


class TestToolRegistration:
    def test_six_tools_registered(self, mcp: FastMCP):
        names = set(mcp._tool_manager._tools.keys())
        assert names == {"classify", "check", "rate", "batch_judge", "system_one_raw", "health"}

    @pytest.mark.parametrize(
        "tool_name",
        ["classify", "check", "rate", "batch_judge", "system_one_raw", "health"],
    )
    def test_each_tool_has_description(self, mcp: FastMCP, tool_name: str):
        tool = _tool_dict(mcp, tool_name)
        assert tool.description, f"{tool_name} description is empty"

    @pytest.mark.parametrize(
        "tool_name",
        ["classify", "check", "rate", "batch_judge"],
    )
    def test_core_tools_have_negative_section(self, mcp: FastMCP, tool_name: str):
        """The NEGATIVE section is the agent-misuse guardrail."""
        tool = _tool_dict(mcp, tool_name)
        assert "NEVER" in tool.description, f"{tool_name} missing NEVER guidance"

    def test_batch_judge_negative_mentions_deterministic(self, mcp: FastMCP):
        """batch_judge NEGATIVE must warn against deterministic logic (per user review 2026-09-22)."""
        desc = _tool_dict(mcp, "batch_judge").description
        # Should mention at least one of these deterministic logic indicators
        keywords = ["arithmetic", "counting", "deterministic", "<10 lines", "ground truth"]
        assert any(k in desc for k in keywords), (
            f"batch_judge NEGATIVE missing deterministic-logic warning. "
            f"Got: {desc[:300]}"
        )

    def test_batch_judge_negative_mentions_cap_20(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "batch_judge").description
        assert "20" in desc

    def test_batch_judge_positive_mentions_isolation(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "batch_judge").description
        assert "INDEPENDENT" in desc.upper() or "isolation" in desc.lower()

    @pytest.mark.parametrize(
        "tool_name",
        ["classify", "check", "rate", "batch_judge"],
    )
    def test_core_tools_have_example(self, mcp: FastMCP, tool_name: str):
        tool = _tool_dict(mcp, tool_name)
        assert "EXAMPLE" in tool.description, f"{tool_name} missing EXAMPLE"

    def test_classify_description_specifics(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "classify").description
        assert "classify" in desc.lower()
        assert "category" in desc.lower() or "categor" in desc.lower() or "one of" in desc.lower()

    def test_check_description_specifics(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "check").description
        assert "yes" in desc.lower() or "no" in desc.lower() or "binary" in desc.lower()

    def test_rate_description_specifics(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "rate").description
        assert "score" in desc.lower() or "rubric" in desc.lower()

    def test_batch_judge_description_warns_no_bundling(self, mcp: FastMCP):
        # The whole point of batch_judge is preventing state bundling
        desc = _tool_dict(mcp, "batch_judge").description
        assert "independ" in desc.lower() or "bundl" in desc.lower() or "separate" in desc.lower()

    def test_system_one_raw_described_as_escape_hatch(self, mcp: FastMCP):
        desc = _tool_dict(mcp, "system_one_raw").description
        assert "escape" in desc.lower() or "advanced" in desc.lower() or "raw" in desc.lower()

    @pytest.mark.parametrize(
        "tool_name",
        ["classify", "check", "rate", "batch_judge", "system_one_raw", "health"],
    )
    def test_fourth_section_is_contract_not_positive(self, mcp: FastMCP, tool_name: str):
        """The 4th section (after EXAMPLE) describes the return shape — must be [CONTRACT],
        not [POSITIVE] which is reserved for when-to-use. Per user review 2026-09-22.
        Prevents silent regressions where a contributor renames it back.
        """
        desc = _tool_dict(mcp, tool_name).description
        import re
        # Section markers look like [POSITIVE] / [NEGATIVE] / [EXAMPLE] / [CONTRACT]
        # at the start of a line.
        markers = re.findall(r"^\[([A-Z]+)\]", desc, re.MULTILINE)
        assert len(markers) == 4, f"{tool_name} should have exactly 4 sections, got {len(markers)}: {markers}"
        assert markers[0] == "POSITIVE", f"{tool_name} 1st should be [POSITIVE], got [{markers[0]}]"
        assert markers[1] == "NEGATIVE", f"{tool_name} 2nd should be [NEGATIVE], got [{markers[1]}]"
        assert markers[2] == "EXAMPLE", f"{tool_name} 3rd should be [EXAMPLE], got [{markers[2]}]"
        assert markers[3] == "CONTRACT", f"{tool_name} 4th should be [CONTRACT], got [{markers[3]}]"


class TestToolArgs:

    def test_classify_args(self, mcp: FastMCP):
        params = _tool_dict(mcp, "classify").parameters
        assert "properties" in params
        props = params["properties"]
        assert "text" in props
        assert "options" in props

    def test_check_args(self, mcp: FastMCP):
        params = _tool_dict(mcp, "check").parameters
        assert "text" in params["properties"]
        assert "condition" in params["properties"]

    def test_rate_args(self, mcp: FastMCP):
        params = _tool_dict(mcp, "rate").parameters
        assert "text" in params["properties"]
        assert "levels" in params["properties"]

    def test_batch_judge_args(self, mcp: FastMCP):
        params = _tool_dict(mcp, "batch_judge").parameters
        assert "items" in params["properties"]
        assert "criteria" in params["properties"]

    def test_health_no_args(self, mcp: FastMCP):
        params = _tool_dict(mcp, "health").parameters
        # Health takes no required args
        assert params.get("properties", {}) == {} or all(
            p not in params.get("required", []) for p in params.get("properties", {})
        )
class TestTransportSecurityDefaults:
    """Server must accept connections to its Tailscale IP (100.86.140.31).

    Bug found 2026-09-22: FastMCP's default allowed_hosts = ['127.0.0.1:*',
    'localhost:*', '[::1]:*']. Without override, clients connecting to the
    Tailscale IP get 421 Misdirected Request (DNS rebinding protection).
    """

    def test_allowed_hosts_includes_tailscale_ip(self):
        from jev_mcp.server import create_mcp
        from mcp.server.transport_security import TransportSecuritySettings

        m = create_mcp()
        hosts = m.settings.transport_security.allowed_hosts
        assert "100.86.140.31:*" in hosts, (
            f"Tailscale IP missing from allowed_hosts: {hosts}. "
            f"Clients will get 421 when connecting to 100.86.140.31:8765."
        )
        assert "127.0.0.1:*" in hosts, "loopback should be allowed for same-machine dev"

    def test_origins_include_tailscale_ip(self):
        from jev_mcp.server import create_mcp
        m = create_mcp()
        origins = m.settings.transport_security.allowed_origins
        assert "http://100.86.140.31:*" in origins

    def test_dns_rebinding_protection_still_on(self):
        # We allow our Tailscale IP but keep the security feature enabled to block
        # anything else (e.g. random Host headers from attackers on the LAN).
        from jev_mcp.server import create_mcp
        m = create_mcp()
        assert m.settings.transport_security.enable_dns_rebinding_protection is True
