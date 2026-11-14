"""Interface and behaviour of the sub300 model functions."""

import copy
import json

import pytest

from plants.sub300 import model


def test_summary_is_json_serialisable(summary):
    text = json.dumps(summary)
    assert json.loads(text)["performance"]["heat_rate"] > 0


def test_summary_schema(summary):
    assert set(summary) >= {
        "performance", "turbine", "extractions", "heaters", "condenser",
        "pumps", "states", "power",
    }
    assert [e["no"] for e in summary["extractions"]] == list(range(1, 9))
    kinds = [h["type"] for h in summary["heaters"]]
    assert kinds.count("surface") == 7 and kinds.count("deaerator") == 1
    for key in ("m", "p", "T", "h", "s", "x"):
        assert key in summary["states"]["main_steam"]


def test_steam_quality_only_in_two_phase_region(summary):
    assert summary["states"]["main_steam"]["x"] is None
    assert summary["states"]["feed_pump_out"]["x"] is None
    assert 0.85 < summary["states"]["lp_exhaust"]["x"] < 1
    assert summary["states"]["hotwell"]["x"] == pytest.approx(0, abs=1e-9)


def test_specified_heater_terminal_differences_are_met(summary, params):
    spec = {h["label"]: h for h in params["heaters"]}
    for h in summary["heaters"]:
        if h["type"] != "surface":
            continue
        assert h["ttd_u"] == pytest.approx(spec[h["label"]]["ttd_u"], abs=1e-6)
        assert h["ttd_l"] == pytest.approx(spec[h["label"]]["ttd_l"], abs=1e-6)


def test_build_model_does_not_modify_params(params):
    before = copy.deepcopy(params)
    model.build_model(params)
    assert params == before


def test_higher_back_pressure_raises_heat_rate(params, summary):
    changed = copy.deepcopy(params)
    changed["condenser"]["p"] = 0.0080
    _, result = model.run(changed)
    assert result["condenser"]["p"] == pytest.approx(0.0080)
    assert result["performance"]["heat_rate"] > summary["performance"]["heat_rate"] * 1.005
    assert result["performance"]["gross_power"] == pytest.approx(
        params["rating"]["gross_power"]
    )


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p["turbine"]["sections"][0].pop("h_out"), "exactly one"),
    (lambda p: p["heaters"][0].update(extraction="XX"), "unknown extraction"),
    (lambda p: p["feed_pump"].update(drive="motor"), "drive"),
    (lambda p: p["turbine"].update(reheat_after="LP5"), "reheat_after"),
    (lambda p: p["heaters"][4].update(type="surface"), "deaerator"),
])
def test_invalid_parameters_are_rejected(params, mutate, message):
    changed = copy.deepcopy(params)
    mutate(changed)
    with pytest.raises(model.ModelError, match=message):
        model.build_model(changed)


def test_fixed_efficiencies_reproduce_design_and_respond_physically(
    params, summary
):
    frozen = model.with_fixed_efficiencies(params, summary)
    assert all("h_out" not in s for s in frozen["turbine"]["sections"])
    _, same = model.run(frozen)
    assert same["performance"]["heat_rate"] == pytest.approx(
        summary["performance"]["heat_rate"], rel=1e-7
    )
    # more reheat pressure loss must cost heat rate once the expansion line
    # is free to move
    frozen["reheat"]["pr"] = 0.86
    _, worse = model.run(frozen)
    assert worse["performance"]["heat_rate"] > summary["performance"]["heat_rate"]
