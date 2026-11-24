"""End-to-end test of the MCP server through an MCP client (in process)."""

import asyncio

from mcp import Client

from heatbalance_mcp import server


async def _session():
    async with Client(server.server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        derived = await client.call_tool("derive_model", {
            "base": "sub300",
            "name": "e2e",
            "patches": [
                {"path": "condenser.p", "value": 0.0065, "reason": "summer"},
                {"path": "turbine.sections[LP5].eta_s", "value": 0.78,
                 "reason": "retrofit"},
            ],
        })
        design = await client.call_tool("solve_design", {"model": "e2e"})
        part = await client.call_tool(
            "solve_offdesign", {"model": "e2e", "conditions": {"load": 0.5}}
        )
        exported = await client.call_tool("export_model", {"model": "e2e"})
        unknown = await client.call_tool("solve_design", {"model": "missing"})
        return tools, derived, design, part, exported, unknown


def test_full_agent_workflow():
    tools, derived, design, part, exported, unknown = asyncio.run(_session())
    assert {
        "list_reference_plants", "list_models", "get_plant_data",
        "derive_model", "export_model", "solve_design", "solve_offdesign",
        "load_sweep", "get_results", "compare",
    } <= tools
    assert derived.structured_content["status"] == "created"
    d = design.structured_content
    assert d["status"] == "solved" and d["issues"]["errors"] == []
    p = part.structured_content
    assert p["status"] == "solved"
    assert p["performance"]["load"] == 0.5
    assert p["performance"]["heat_rate"] > d["performance"]["heat_rate"]
    assert "P2:" in exported.structured_content["yaml"]
    assert unknown.is_error
    assert "unknown model" in unknown.content[0].text
