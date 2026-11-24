"""Off-design (part-load) operation of the sub300 unit."""

import json

import pytest

from checks.balance import check_balance
from plants.sub300 import model
from plants.sub300.handcalc import hand_calc_offdesign

# Each case is solved by TESPy and by the independent Flügel/Stodola hand
# calculation; they share only the parameters and CoolProp.
CASES = [
    {"load": 0.75},
    {"load": 0.5, "pressure": "sliding"},
    {"load": 0.4, "pressure": "constant"},
    {"load": 0.6, "condenser": {"cw_T_in": 25.0}},
    {"main_steam_flow": 150.0, "pressure": 11.0},
    {"gross_power": 200.0, "condenser": {"p": 0.008}},
]


def test_design_state_is_json_serialisable(state):
    restored = json.loads(json.dumps(state))
    assert restored["summary"]["performance"]["gross_power"] == pytest.approx(300.0)


@pytest.mark.parametrize("mode", ["sliding", "curve", "constant"])
def test_full_load_off_design_reproduces_design(params, state, summary, mode):
    nw = model.solve_offdesign(params, state, {"load": 1.0, "pressure": mode})
    result = model.summarize(nw)
    for key in ("heat_rate", "main_steam_flow", "feedwater_T", "exhaust_h"):
        assert result["performance"][key] == pytest.approx(
            summary["performance"][key], rel=1e-7
        ), key
    for a, b in zip(result["extractions"], summary["extractions"]):
        assert a["p"] == pytest.approx(b["p"], rel=1e-7)
        assert a["m"] == pytest.approx(b["m"], rel=1e-6)
    for a, b in zip(result["leakages"], summary["leakages"]):
        assert a["m"] == pytest.approx(b["m"], rel=1e-6)
    assert result["performance"]["mechanical_loss"] == pytest.approx(
        summary["performance"]["mechanical_loss"], rel=1e-7
    )
    assert result["operation"]["throttle_pr"] == pytest.approx(1.0, abs=1e-8)


@pytest.mark.parametrize("conditions", CASES)
def test_off_design_matches_hand_calculation(params, state, conditions):
    nw = model.solve_offdesign(params, state, conditions)
    assert check_balance(nw)["issues"] == []
    result = model.summarize(nw)
    ref = hand_calc_offdesign(params, conditions)
    perf, op = result["performance"], result["operation"]
    assert perf["gross_power"] == pytest.approx(ref["gross_power"], rel=1e-6)
    assert perf["heat_rate"] == pytest.approx(ref["heat_rate"], rel=1e-6)
    assert perf["main_steam_flow"] == pytest.approx(ref["main_steam_flow"], rel=1e-6)
    assert op["p_main"] == pytest.approx(ref["p_main"], rel=1e-6)
    assert op["p_turbine_inlet"] == pytest.approx(ref["p_turbine_inlet"], rel=1e-6)
    assert perf["feedwater_T"] == pytest.approx(ref["feedwater_T"], abs=1e-4)
    assert result["condenser"]["p"] == pytest.approx(ref["condenser_p"], rel=1e-6)
    assert result["turbine"]["hot_reheat"]["p"] == pytest.approx(
        ref["hot_reheat_p"], rel=1e-6
    )
    for e in result["extractions"]:
        assert e["m"] == pytest.approx(ref["extraction_flows"][e["heater"]], rel=1e-5)
    for sec in result["turbine"]["sections"]:
        assert sec["p_out"] == pytest.approx(
            ref["section_p_out"][sec["label"]], rel=1e-6
        )
    for lk in result["leakages"]:
        assert lk["m"] == pytest.approx(ref["leakage_flows"][lk["label"]], rel=1e-5)


def test_sweep_trends(sweep):
    loads = [s["operation"]["load"] for s in sweep]
    assert loads == pytest.approx([1.0, 0.9, 0.75, 0.5, 0.4])
    heat_rate = [s["performance"]["heat_rate"] for s in sweep]
    feedwater = [s["performance"]["feedwater_T"] for s in sweep]
    assert heat_rate == sorted(heat_rate)
    assert feedwater == sorted(feedwater, reverse=True)
    for a, b in zip(sweep, sweep[1:]):
        for ea, eb in zip(a["extractions"], b["extractions"]):
            assert eb["p"] < ea["p"]


def test_pressure_curve_and_throttling(params, sweep):
    for s in sweep:
        op = s["operation"]
        assert op["pressure_mode"] == "curve"
        assert op["p_main"] == pytest.approx(
            model.pressure_curve(params, op["load"]), rel=1e-6
        )
        assert op["throttle_pr"] <= 1 + 1e-9


def test_mechanical_loss_is_constant(sweep):
    losses = [s["performance"]["mechanical_loss"] for s in sweep]
    assert losses == pytest.approx([losses[0]] * len(losses), rel=1e-7)


def test_leakage_falls_with_pressure(params, state):
    sliding = model.summarize(
        model.solve_offdesign(params, state, {"load": 0.5, "pressure": "sliding"})
    )
    constant = model.summarize(
        model.solve_offdesign(params, state, {"load": 0.5, "pressure": "constant"})
    )
    for a, b in zip(sliding["leakages"], constant["leakages"]):
        assert a["m"] < b["m"]


def test_pressure_curve_interpolation(params):
    assert model.pressure_curve(params, 1.0) == pytest.approx(16.7)
    assert model.pressure_curve(params, 0.3) == pytest.approx(8.4772)
    mid = model.pressure_curve(params, 0.875)
    assert mid == pytest.approx((13.2738 + 16.7) / 2)


@pytest.mark.parametrize("conditions, message", [
    ({"load": 0.5, "main_steam_flow": 100.0}, "only one"),
    ({"load": 0.1}, "between"),
    ({"pressure": "floating"}, "pressure must be"),
    ({"main_steam_flow": 100.0, "pressure": "curve"}, "needs load"),
    ({"condenser": {"T": 20.0}}, "condenser must be"),
    ({"speed": 3000}, "unknown"),
])
def test_invalid_conditions_are_rejected(params, state, conditions, message):
    with pytest.raises(model.ModelError, match=message):
        model.solve_offdesign(params, state, conditions)


def test_design_state_requires_design_network(params, state):
    nw = model.solve_offdesign(params, state, {"load": 0.8})
    with pytest.raises(model.ModelError, match="design mode"):
        model.design_state(nw)
