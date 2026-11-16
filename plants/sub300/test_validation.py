"""Validation of the model code against published design data (S6).

Case: 300 MW subcritical unit with seven heaters, 100 % THA, from
Yang et al., Energies 2023, 16(9), 3727 (``validation_en16093727.yaml``).

What is calibrated and what is predicted
----------------------------------------
Two quantities are not published and were fitted to one published value
each: condenser pressure (to the RH7 extraction flow) and the efficiency of
the last LP section (to gross power). The two leakage flows follow from
published mass balances; their routing is assumed (V10). Reheat flow and
heat rate then agree almost by construction, so the genuine predictions are
the extraction flows of the other six heaters.
"""

import copy
from pathlib import Path

import pytest
from CoolProp.CoolProp import PropsSI
from scipy.optimize import brentq

from checks.balance import check_balance
from checks.issues import errors, warnings
from checks.ranges import check_ranges
from plants.sub300 import model
from plants.sub300.handcalc import hand_calc

CASE = Path(model.__file__).with_name("validation_en16093727.yaml")

# The back pressure inferred from the published data lies outside the
# wet-cooling range and the cooling water side is a placeholder (V5).
EXPECTED_WARNINGS = {
    ("condenser_pressure", "condenser"),
    ("cooling_water_rise", "condenser"),
}
# Extraction flows that are predictions and the calibrated one. RH3 has
# its own, wider tolerance because it depends on the assumed leakage
# routing (V10).
PREDICTED = ["RH1", "RH2", "DEA", "RH5", "RH6"]
CALIBRATED = "RH7"


def _h(p, T):
    return PropsSI("H", "P", p * 1e6, "T", T + 273.15, "water") / 1e3


def _t_sat(p):
    return PropsSI("T", "P", p * 1e6, "Q", 0, "water") - 273.15


@pytest.fixture(scope="module")
def case():
    return model.load_design_data(CASE)


@pytest.fixture(scope="module")
def case_params():
    return model.load_params(CASE)


@pytest.fixture(scope="module")
def published(case):
    return model._strip(case["published"])


@pytest.fixture(scope="module")
def tol(case):
    return model._strip(case["tolerances"])


@pytest.fixture(scope="module")
def result(case_params):
    return model.run(case_params)


def published_heat_rate(pub, main_steam):
    """Gross heat rate (kJ/kWh) implied by the published Table 1 values."""
    heat = pub["main_steam_flow"] * (
        _h(main_steam["p"], main_steam["T"])
        - _h(pub["feedwater_p"], pub["feedwater_T"])
    ) + pub["reheat_steam_flow"] * (
        _h(pub["hot_reheat_p"], main_steam["T_reheat"])
        - _h(pub["cold_reheat_p"], pub["cold_reheat_T"])
    )
    return heat / pub["gross_power"] / 1e3 * 3600


def test_inputs_are_consistent_with_the_published_tables(case_params, published):
    """Derived inputs (enthalpies, turbine-side pressures) match Table 1/2."""
    table = {h["label"]: h for h in published["heaters"]}
    sections = {s["label"]: s for s in case_params["turbine"]["sections"]}
    for heater in case_params["heaters"]:
        row = table[heater["label"]]
        sec = sections[heater["extraction"]]
        assert sec["p_out"] * heater["line_pr"] == pytest.approx(row["p"], rel=2e-4)
        if heater["extraction"] == case_params["turbine"]["reheat_after"]:
            h_ref = _h(published["cold_reheat_p"], published["cold_reheat_T"])
        else:
            h_ref = _h(row["p"], row["T"])
        assert sec["h_out"] == pytest.approx(h_ref, abs=0.1)
    assert case_params["reheat"]["pr"] == pytest.approx(
        published["hot_reheat_p"] / published["cold_reheat_p"], rel=1e-4
    )
    top = case_params["heaters"][-1]
    assert top["ttd_u"] == pytest.approx(
        _t_sat(table[top["label"]]["p"]) - published["feedwater_T"], abs=0.01
    )


def test_power_matches_published_design(result, published, tol):
    deviation = result[1]["performance"]["gross_power"] / published["gross_power"] - 1
    assert abs(deviation) <= tol["gross_power"], f"{deviation:.4%}"


def test_heat_rate_matches_published_design(result, published, case_params, tol):
    main = dict(case_params["main_steam"], T_reheat=case_params["reheat"]["T"])
    reference = published_heat_rate(published, main)
    assert reference == pytest.approx(8192.6, abs=0.5)
    deviation = result[1]["performance"]["heat_rate"] / reference - 1
    assert abs(deviation) <= tol["heat_rate"], f"{deviation:.4%}"


def test_feedwater_temperature_and_reheat_flow(result, published, tol):
    perf = result[1]["performance"]
    assert perf["feedwater_T"] == pytest.approx(published["feedwater_T"], abs=0.1)
    assert perf["reheat_steam_flow"] == pytest.approx(
        published["reheat_steam_flow"], rel=tol["reheat_steam_flow"]
    )


def test_leakage_flows_follow_from_published_mass_balances(case_params, published):
    table = {h["label"]: h["m"] for h in published["heaters"]}
    bypass = (
        published["main_steam_flow"] - table["RH1"] - table["RH2"]
        - published["reheat_steam_flow"]
    )
    into_hp_drains = (
        published["main_steam_flow"] - published["condensate_flow"]
        - table["RH1"] - table["RH2"] - table["RH3"] - table["DEA"]
    )
    leaks = {lk["label"]: lk for lk in case_params["leakages"]}
    assert leaks["valve_stem"]["m"] == pytest.approx(into_hp_drains, abs=0.005)
    assert leaks["valve_stem"]["m"] + leaks["hp_ip_gland"]["m"] == pytest.approx(
        bypass, abs=0.005
    )


def test_extraction_flows_match_published_design(result, published, tol):
    table = {h["label"]: h["m"] for h in published["heaters"]}
    flows = {e["heater"]: e["m"] for e in result[1]["extractions"]}
    assert set(flows) == set(table)
    for label in PREDICTED + [CALIBRATED]:
        assert flows[label] == pytest.approx(
            table[label], rel=tol["extraction_flow"]
        ), label
    assert flows["RH3"] == pytest.approx(
        table["RH3"], rel=tol["extraction_flow_rh3"]
    )


def test_conservation_and_ranges(result):
    network, summary = result
    report = check_balance(network)
    assert report["issues"] == [], "\n".join(map(str, report["issues"]))
    issues = check_ranges(summary)
    assert errors(issues) == [], "\n".join(map(str, errors(issues)))
    assert {(i.check, i.location) for i in warnings(issues)} == EXPECTED_WARNINGS


def test_model_agrees_with_hand_calculation(result, case_params):
    reference = hand_calc(case_params)
    perf = result[1]["performance"]
    assert perf["gross_power"] == pytest.approx(reference["gross_power"], rel=1e-6)
    assert perf["heat_rate"] == pytest.approx(reference["heat_rate"], rel=1e-6)


def test_calibrated_values_are_reproducible(case_params, published):
    """Re-derive condenser pressure (V5) and last-section efficiency (V6)."""
    target_flow = next(
        h["m"] for h in published["heaters"] if h["label"] == CALIBRATED
    )

    def with_values(p_cond=None, eta=None):
        p = copy.deepcopy(case_params)
        if p_cond is not None:
            p["condenser"]["p"] = p_cond
        if eta is not None:
            p["turbine"]["sections"][-1]["eta_s"] = eta
        return p

    p_cond = brentq(
        lambda x: hand_calc(with_values(p_cond=x))["extraction_fractions"][CALIBRATED]
        * published["main_steam_flow"] - target_flow,
        0.004, 0.03,
    )
    eta = brentq(
        lambda x: hand_calc(with_values(p_cond=p_cond, eta=x))["gross_power"]
        - published["gross_power"],
        0.3, 1.0,
    )
    assert case_params["condenser"]["p"] == pytest.approx(p_cond, rel=0.01)
    assert case_params["turbine"]["sections"][-1]["eta_s"] == pytest.approx(
        eta, abs=0.005
    )


def test_effect_of_neglecting_leakages(case_params, published):
    """Without leakages the heat rate is 0.2-0.5 % high and RH3 is missed.

    The last-section efficiency is refitted to the published power, as it
    would be if leakages had never been modelled.
    """
    plain = copy.deepcopy(case_params)
    del plain["leakages"]

    def power(eta):
        plain["turbine"]["sections"][-1]["eta_s"] = eta
        return hand_calc(plain)["gross_power"]

    brentq(lambda x: power(x) - published["gross_power"], 0.3, 1.0)
    result = hand_calc(plain)
    main = dict(case_params["main_steam"], T_reheat=case_params["reheat"]["T"])
    deviation = result["heat_rate"] / published_heat_rate(published, main) - 1
    assert 0.002 < deviation < 0.005
    table = {h["label"]: h["m"] for h in published["heaters"]}
    rh3 = result["extraction_fractions"]["RH3"] * published["main_steam_flow"]
    assert rh3 / table["RH3"] - 1 > 0.10
