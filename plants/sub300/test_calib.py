"""Calibration test of the sub300 design point (THA).

Reference: independent classical heat balance on the same typical
parameters (``handcalc.py``, assumption A16). Power is compared at the main
steam flow found by the TESPy model.
"""

import pytest

from checks.balance import check_balance
from checks.issues import errors, warnings
from checks.ranges import check_ranges
from plants.sub300 import model
from plants.sub300.handcalc import hand_calc

# Known, explained deviations from typical practice (see README):
# S1's first extraction point splits the HP expansion unevenly, the HP
# cylinder as a whole is plausible.
EXPECTED_WARNINGS = {("section_eta_typical", "HP2")}


@pytest.fixture(scope="module")
def reference(params):
    return hand_calc(params)


@pytest.fixture(scope="module")
def tolerances():
    cal = model.load_design_data()["calibration"]
    return cal["tolerance_power"]["value"], cal["tolerance_heat_rate"]["value"]


def test_design_point_reaches_rated_power(summary, params):
    rated = params["rating"]["gross_power"]
    assert summary["performance"]["gross_power"] == pytest.approx(rated, rel=1e-9)


def test_power_matches_reference(summary, reference, tolerances):
    perf = summary["performance"]
    p_ref = perf["main_steam_flow"] * reference["specific_power"]
    deviation = perf["gross_power"] / p_ref - 1
    assert abs(deviation) <= tolerances[0], f"power deviation {deviation:.4%}"


def test_heat_rate_matches_reference(summary, reference, tolerances):
    deviation = summary["performance"]["heat_rate"] / reference["heat_rate"] - 1
    assert abs(deviation) <= tolerances[1], f"heat rate deviation {deviation:.4%}"


def test_extraction_flows_match_reference(summary, reference, tolerances):
    for e in summary["extractions"]:
        ref = reference["extraction_fractions"][e["heater"]]
        assert e["fraction"] == pytest.approx(ref, rel=tolerances[0]), e["heater"]
    assert summary["pumps"]["bfpt_steam_fraction"] == pytest.approx(
        reference["bfpt_fraction"], rel=tolerances[0]
    )


def test_conservation(network):
    report = check_balance(network)
    assert report["issues"] == [], "\n".join(map(str, report["issues"]))
    assert report["max_mass_residual"] < 1e-6  # kg/s
    assert report["max_energy_residual_rel"] < 1e-8
    assert abs(report["system"]["energy_residual_rel"]) < 1e-8
    assert abs(report["system"]["mass_residual"]) < 1e-6


def test_ranges_report_no_errors(summary):
    issues = check_ranges(summary)
    assert errors(issues) == [], "\n".join(map(str, errors(issues)))
    found = {(i.check, i.location) for i in warnings(issues)}
    assert found == EXPECTED_WARNINGS, "\n".join(map(str, issues))


def test_public_order_of_magnitude(summary):
    """Loose comparison with public figures of comparable units (S4, S5)."""
    plaus = model.load_design_data()["plausibility"]
    perf = summary["performance"]
    fw = plaus["final_feedwater_T"]
    assert abs(perf["feedwater_T"] - fw["value"]) <= fw["tolerance"]
    # a design heat rate must beat the test heat rate of an aged unit, but
    # not by more than about 10 %
    test_hr = plaus["test_heat_rate_corrected"]["value"]
    assert 0.90 * test_hr < perf["heat_rate"] < test_hr
