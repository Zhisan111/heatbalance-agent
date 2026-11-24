"""MCP server exposing the heat balance workspace (stdio transport).

Run with ``heatbalance-mcp`` (console script) or
``python -m heatbalance_mcp.server``.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from heatbalance_mcp.service import ServiceError, Workspace

INSTRUCTIONS = """\
Heat balance models of steam power plants (TESPy), with automatic mass and
energy conservation checks and engineering plausibility checks.

Work from a validated reference plant, never from scratch:
1. list_reference_plants, then get_plant_data to read parameters and their
   sources (S*) and assumptions (A*, V*).
2. derive_model with patches; every patch needs a reason. Input errors are
   reported before anything is solved and the model is then not created.
3. solve_design, then solve_offdesign or load_sweep for part load.
4. Read the issues of every result: errors mean the result must not be
   used; warnings need an explanation. Fix by patching, not by ignoring.
5. compare against the reference, export_model to keep the result.

Units: pressure MPa (absolute), temperature degC, temperature difference K,
enthalpy kJ/kg, mass flow kg/s, power and heat MW, heat rate kJ/kWh.
Efficiencies and fractions are plain numbers (0.85, not 85).
"""

server = MCPServer("heatbalance-agent", instructions=INSTRUCTIONS)
workspace = Workspace()


def _call(func, *args, **kwargs):
    """Run a service call: stdout is reserved for the protocol."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return func(*args, **kwargs)
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
def list_reference_plants() -> list[dict[str, Any]]:
    """List the validated reference plants that models can be derived from."""
    return _call(workspace.list_reference_plants)


@server.tool()
def list_models() -> list[dict[str, Any]]:
    """List reference plants and derived models in this session with their solved cases."""
    return _call(workspace.list_models)


@server.tool()
def get_plant_data(model: str, part: str = "parameters") -> dict[str, Any]:
    """Read the data of a reference plant or derived model.

    part: "parameters" (values with unit and ref), "references" (public
    sources S*), "assumptions" (A*, V*, and P* for agent patches),
    "changes" (patch log of a derived model) or "all".
    """
    return _call(workspace.get_plant_data, model, part)


@server.tool()
def derive_model(
    base: str, patches: list[dict[str, Any]], name: str | None = None
) -> dict[str, Any]:
    """Create a new model from a reference plant or derived model by patches.

    Each patch: {"path": ..., "value": ..., "reason": ...}, optional
    "op": "set" (default) or "delete". Paths are relative to the parameters
    block, list items are selected by label, e.g.
      "condenser.p" (MPa), "heaters[H1].ttd_u" (K),
      "turbine.sections[LP5].eta_s", "turbine.sections[LP4].h_out" (kJ/kg),
      "leakages[hp_ip_gland].fraction", "feed_pump.p_out",
      "operation.pressure_curve[0].p".
    A section is defined by either h_out or eta_s: delete one, set the other.
    The model is only created if the patched parameters pass the input
    checks; otherwise status is "rejected" and input_issues explain why.
    """
    return _call(workspace.derive_model, base, patches, name)


@server.tool()
def export_model(model: str) -> dict[str, Any]:
    """Return the model's design data as YAML; patches appear as assumptions P*."""
    return _call(workspace.export_model, model)


@server.tool()
def solve_design(model: str) -> dict[str, Any]:
    """Solve the design point and run conservation and plausibility checks.

    Returns key performance, extraction data and issues (errors and
    warnings, each with location, value, expected range and a hint).
    """
    return _call(workspace.solve_design, model)


@server.tool()
def solve_offdesign(
    model: str, conditions: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Solve one part-load operating point (the design point is solved first if needed).

    conditions keys (all optional):
      exactly one of "load" (fraction of design gross power, 0.2-1.1),
        "gross_power" (MW) or "main_steam_flow" (kg/s); default load 1.0;
      "pressure": "sliding" | "curve" | "constant" | number (MPa);
      "condenser": {"p": MPa} or {"cw_T_in": degC}; default design back pressure.
    Returns the case id (for get_results/compare), results and issues.
    """
    return _call(workspace.solve_offdesign, model, conditions)


@server.tool()
def load_sweep(
    model: str,
    loads: list[float] | None = None,
    conditions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Solve a series of loads (default 1.0, 0.9, 0.75, 0.5, 0.4) with trend checks.

    conditions as for solve_offdesign but without the load keys.
    """
    return _call(workspace.load_sweep, model, loads, conditions)


@server.tool()
def get_results(
    model: str, case: str = "design", fields: list[str] | None = None
) -> dict[str, Any]:
    """Detailed results of a solved case.

    fields (default ["performance", "operation"]): operation, performance,
    turbine, extractions, leakages, heaters, condenser, pumps, states (every
    connection: m, p, T, h, s, x), power.
    """
    return _call(workspace.get_results, model, case, fields)


@server.tool()
def compare(
    model_a: str, model_b: str, case_a: str = "design", case_b: str = "design"
) -> dict[str, Any]:
    """Compare key results and extraction flows of two solved cases (b - a)."""
    return _call(workspace.compare, model_a, model_b, case_a, case_b)


def main() -> None:
    # keep TESPy's solver progress out of the host's server log
    logging.getLogger("tespy").setLevel(logging.WARNING)
    server.run("stdio")


if __name__ == "__main__":
    main()
