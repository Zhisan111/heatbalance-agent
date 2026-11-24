"""Patch language and input plausibility checks."""

import pytest

from checks.inputs import check_inputs
from heatbalance_mcp.patches import PatchError, apply_patches
from plants.sub300 import model


@pytest.fixture(scope="module")
def doc():
    return model.load_design_data()


@pytest.mark.parametrize("name", ["design_data.yaml", "validation_en16093727.yaml"])
def test_reference_data_pass_input_checks(name):
    params = model.load_params(model.DATA_FILE.with_name(name))
    assert check_inputs(params) == []


def test_patches_record_provenance(doc):
    patched, log = apply_patches(doc, [
        {"path": "condenser.p", "value": 0.0065, "reason": "summer"},
        {"path": "heaters[H1].ttd_u", "value": -1.0, "reason": "new heater"},
        {"path": "turbine.sections[LP4].h_out", "op": "delete", "reason": "free"},
        {"path": "turbine.sections[LP4].eta_s", "value": 0.9, "reason": "free"},
        {"path": "operation.pressure_curve[0].p", "value": 9.0, "reason": "curve"},
    ])
    p = patched["parameters"]
    assert p["condenser"]["p"] == {"value": 0.0065, "unit": "MPa", "ref": "P1"}
    h1 = next(h for h in p["heaters"] if h["label"] == "H1")
    assert h1["ttd_u"] == -1.0 and h1["ref"][-1] == "P2"
    lp4 = next(s for s in p["turbine"]["sections"] if s["label"] == "LP4")
    assert "h_out" not in lp4 and lp4["eta_s"] == 0.9
    assert p["operation"]["pressure_curve"][0]["p"] == 9.0
    assert [c["id"] for c in log] == ["P1", "P2", "P3", "P4", "P5"]
    assert log[0]["old"] == 0.0049 and log[0]["new"] == 0.0065
    assert "summer" in patched["assumptions"]["P1"]["rationale"]
    # the original document is untouched
    assert doc["parameters"]["condenser"]["p"]["value"] == 0.0049
    params = model._strip(p)
    assert params["condenser"]["p"] == 0.0065 and check_inputs(params) == []


@pytest.mark.parametrize("patch, message", [
    ({"path": "condenser.q", "value": 1, "reason": "r"}, "unknown key"),
    ({"path": "heaters[H9].ttd_u", "value": 1, "reason": "r"}, "available labels"),
    ({"path": "condenser.p", "value": 0.006}, "reason is required"),
    ({"path": "condenser.p", "value": "low", "reason": "r"}, "expected a number"),
    ({"path": "condenser.p", "value": 0.006, "reason": "r", "op": "add"}, "op must be"),
    ({"path": "condenser.p", "op": "delete", "reason": "r"}, "cannot be deleted"),
    ({"path": "heaters[H1]", "value": 1, "reason": "r"}, "must end at a value"),
    ({"path": "heaters[H1].ttd_u", "value": 1, "reason": "r", "x": 1}, "unknown patch fields"),
    ({"path": "turbine..eta_s", "value": 1, "reason": "r"}, "invalid path"),
])
def test_malformed_patches_are_rejected(doc, patch, message):
    with pytest.raises(PatchError, match=message):
        apply_patches(doc, [patch])


INPUT_FAULTS = [
    ("condenser.p", 4.9, "input_condenser_p"),
    ("turbine.eta_gen", 98.7, "input_efficiency"),
    ("turbine.sections[HP2].h_out", 2900.0, "input_section_efficiency"),
    ("turbine.sections[IP2].p_out", 2.0, "input_expansion_pressure"),
    ("heaters[H5].ttd_u", 25.0, "input_heater_ttd_u"),
    ("reheat.pr", 0.5, "input_reheat_pr"),
    ("feed_pump.p_out", 15.0, "input_feed_pump_p"),
    ("condensate_pump.p_out", 0.5, "input_condensate_pump_p"),
    ("leakages[hp_ip_gland].fraction", 1.74, "input_leakage"),
    ("condenser.cw_T_in", 35.0, "input_condenser_cooling"),
    ("operation.pressure_curve[0].p", 15.0, "input_pressure_curve"),
]


@pytest.mark.parametrize("path, value, check", INPUT_FAULTS)
def test_input_checks_detect_fault(doc, path, value, check):
    patched, _ = apply_patches(doc, [{"path": path, "value": value, "reason": "test"}])
    issues = check_inputs(model._strip(patched["parameters"]))
    assert check in {i.check for i in issues if i.severity == "error"}, [
        str(i) for i in issues
    ]
