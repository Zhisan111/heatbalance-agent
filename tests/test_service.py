"""Workspace service behind the MCP server."""

import json

import pytest
import yaml

from heatbalance_mcp.service import ServiceError, Workspace


@pytest.fixture(scope="module")
def ws():
    workspace = Workspace()
    workspace.derive_model("sub300", [
        {"path": "condenser.p", "value": 0.0065, "reason": "summer cooling water"},
        {"path": "turbine.sections[LP5].eta_s", "value": 0.78, "reason": "retrofit"},
    ], name="summer")
    return workspace


def test_reference_plants(ws):
    plants = {p["id"]: p for p in ws.list_reference_plants()}
    assert set(plants) == {"sub300", "en16093727"}
    assert len(plants["sub300"]["heaters"]) == 8
    assert plants["en16093727"]["feed_pump_drive"] == "motor"
    data = ws.get_plant_data("sub300", "references")
    assert "S1" in data["references"]


def test_derived_model_records_changes(ws):
    changes = ws.get_plant_data("summer", "changes")
    assert changes["base"] == "sub300"
    assert [c["path"] for c in changes["changes"]] == [
        "condenser.p", "turbine.sections[LP5].eta_s"
    ]
    exported = yaml.safe_load(ws.export_model("summer")["yaml"])
    assert exported["meta"]["derived_from"] == "sub300"
    assert exported["parameters"]["condenser"]["p"]["ref"] == "P1"
    assert "retrofit" in exported["assumptions"]["P2"]["rationale"]


def test_chained_derivation_continues_patch_numbering(ws):
    res = ws.derive_model("summer", [
        {"path": "heaters[H8].ttd_u", "value": 3.5, "reason": "fouling"}
    ], name="summer-fouled")
    assert res["status"] == "created"
    assert res["changes"][0]["id"] == "P3"
    assert len(ws.get_plant_data("summer-fouled", "changes")["changes"]) == 3


def test_rejected_derivation_creates_no_model(ws):
    before = {m["id"] for m in ws.list_models()}
    res = ws.derive_model("sub300", [
        {"path": "condenser.p", "value": 6.5, "reason": "kPa by mistake"}
    ])
    assert res["status"] == "rejected" and res["model"] is None
    assert res["input_issues"]["errors"][0]["check"] == "input_condenser_p"
    assert {m["id"] for m in ws.list_models()} == before


def test_solve_design_returns_compact_results_with_issues(ws):
    res = ws.solve_design("summer")
    assert res["status"] == "solved"
    assert res["performance"]["gross_power"] == pytest.approx(300.0)
    assert res["issues"]["errors"] == []
    assert res["balance"]["max_energy_residual_rel"] < 1e-8
    assert len(res["extractions"]) == 8
    assert len(json.dumps(res)) < 4000  # compact for agents


def test_offdesign_sweep_results_and_compare(ws):
    od = ws.solve_offdesign("summer", {"load": 0.5})
    assert od["status"] == "solved" and od["case"] == "od1"
    assert od["performance"]["load"] == pytest.approx(0.5)
    sweep = ws.load_sweep("summer", [1.0, 0.75, 0.5])
    hr = [p["heat_rate"] for p in sweep["points"]]
    assert hr == sorted(hr) and sweep["sweep_issues"]["warnings"] == []
    detail = ws.get_results("summer", "od1", ["condenser", "states"])
    assert detail["condenser"]["p"] == pytest.approx(0.0065)
    assert "main_steam" in detail["states"]
    ws.solve_design("sub300")
    cmp = {r["quantity"]: r for r in ws.compare("sub300", "summer")["rows"]}
    assert cmp["heat_rate"]["delta"] > 0
    assert "extraction H8 m" in cmp


def test_offdesign_solves_design_first(ws):
    ws.derive_model("sub300", [
        {"path": "reheat.T", "value": 540.0, "reason": "test"}
    ], name="hot")
    res = ws.solve_offdesign("hot", {"load": 0.75})
    assert res["status"] == "solved"
    assert "design" in ws.list_models()[-1]["cases"]


def test_infeasible_operating_point_is_reported_as_error(ws):
    res = ws.solve_offdesign("summer", {"load": 0.75, "pressure": 9.0})
    assert [e["check"] for e in res["issues"]["errors"]] == ["throttle_pr"]


@pytest.mark.parametrize("call, message", [
    (lambda ws: ws.solve_design("nope"), "unknown model"),
    (lambda ws: ws.get_results("summer", "od99"), "unknown case"),
    (lambda ws: ws.get_results("summer", "design", ["bogus"]), "unknown fields"),
    (lambda ws: ws.solve_offdesign("summer", {"speed": 1}), "unknown operating"),
    (lambda ws: ws.load_sweep("summer", [0.1]), "between 0.2"),
    (lambda ws: ws.load_sweep("summer", None, {"load": 0.5}), "must not contain"),
    (lambda ws: ws.derive_model("sub300", []), "at least one patch"),
    (lambda ws: ws.derive_model("sub300", [{"path": "x", "value": 1, "reason": "r"}]), "unknown key"),
    (lambda ws: ws.derive_model("sub300", [{"path": "condenser.p", "value": 0.006, "reason": "r"}], name="summer"), "already exists"),
])
def test_invalid_requests_raise(ws, call, message):
    with pytest.raises(ServiceError, match=message):
        call(ws)
