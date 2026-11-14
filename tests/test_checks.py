"""The checks must find deliberately injected faults."""

import copy

import pytest

from checks.balance import check_balance, component_balances, system_balance
from checks.issues import ERROR, WARNING, Issue, errors
from checks.ranges import check_ranges
from plants.sub300 import model


# ---- balance ---------------------------------------------------------------
def test_system_balance_items(network, summary):
    system = system_balance(network)
    perf = summary["performance"]
    assert system["energy_in_items"]["boiler (heat)"] == pytest.approx(
        perf["heat_input_boiler"] * 1e6
    )
    assert system["energy_out_items"]["grid"] == pytest.approx(
        perf["gross_power"] * 1e6
    )
    # heat rejected to the cooling water
    assert system["energy_out_items"]["material streams (net)"] == pytest.approx(
        summary["condenser"]["duty"] * 1e6
    )


def test_every_process_component_is_balanced(network):
    records = component_balances(network)
    labels = {r["component"] for r in records}
    assert {"HP1", "H1", "DEA", "condenser", "feed pump", "generator",
            "cycle closer", "shaft"} <= labels
    assert "grid" not in labels


def test_balance_detects_energy_and_mass_faults(params):
    nw = model.solve(model.build_model(params))
    assert check_balance(nw)["issues"] == []

    drain = nw.get_conn("drain_H2")
    drain.h.val_SI += 50e3  # J/kg
    issues = check_balance(nw)["issues"]
    checks = {(i.check, i.location) for i in issues}
    assert ("component_energy_balance", "H2") in checks
    assert ("component_energy_balance", "H2 drain valve") in checks
    assert all(i.severity == ERROR for i in issues)
    drain.h.val_SI -= 50e3

    ext = nw.get_conn("ext_H5")
    ext.m.val_SI *= 1.01
    checks = {(i.check, i.location) for i in check_balance(nw)["issues"]}
    assert ("component_mass_balance", "extraction LP1") in checks
    assert ("component_mass_balance", "H5 extraction line") in checks


# ---- ranges ----------------------------------------------------------------
def _heater(summary, label):
    return next(h for h in summary["heaters"] if h["label"] == label)


def _set_perf(key, value):
    def mutate(s):
        s["performance"][key] = value
    return mutate


def _swap_extraction_pressures(s):
    a, b = s["extractions"][2], s["extractions"][3]
    a["p"], b["p"] = b["p"], a["p"]


FAULTS = [
    (_set_perf("heat_rate", 9000.0), "heat_rate", "performance", ERROR),
    (_set_perf("feedwater_T", 230.0), "feedwater_T", "final feedwater", ERROR),
    (_set_perf("exhaust_x", None), "exhaust_quality", "LP exhaust", ERROR),
    (_set_perf("steam_rate", 3.6), "steam_rate", "performance", WARNING),
    (lambda s: _heater(s, "H1").update(ttd_u=8.0), "heater_ttd_u", "H1", ERROR),
    (lambda s: _heater(s, "H6").update(ttd_u=-1.0), "heater_ttd_u", "H6", ERROR),
    (lambda s: _heater(s, "H3").update(ttd_l=0.5), "heater_ttd_l", "H3", ERROR),
    (lambda s: _heater(s, "H7").update(T_drain=95.0),
     "heater_drain_subcooled", "H7", ERROR),
    (lambda s: _heater(s, "H5").update(T_water_out=100.0),
     "heater_water_rise", "H5", ERROR),
    (lambda s: _heater(s, "DEA").update(x_out=None),
     "deaerator_outlet_saturated", "DEA", ERROR),
    (lambda s: s["turbine"]["cylinders"]["HP"].update(eta_s=0.70),
     "cylinder_eta", "HP cylinder", ERROR),
    (lambda s: s["turbine"]["sections"][3].update(eta_s=1.02),
     "section_eta_physical", "IP2", ERROR),
    (_swap_extraction_pressures, "extraction_pressure_order",
     "extraction 3 -> 4", ERROR),
    (lambda s: s["extractions"][5].update(m=-1.0),
     "extraction_flow_positive", "extraction 6 (H6)", ERROR),
    (lambda s: s["condenser"].update(p=4.9), "condenser_pressure",
     "condenser", WARNING),
    (lambda s: s["pumps"].update(bfpt_steam_fraction=0.10),
     "bfpt_steam_fraction", "BFPT", WARNING),
]


@pytest.mark.parametrize("mutate, check, location, severity", FAULTS)
def test_ranges_detect_fault(summary, mutate, check, location, severity):
    faulty = copy.deepcopy(summary)
    mutate(faulty)
    hits = [
        i for i in check_ranges(faulty)
        if i.check == check and i.location == location
    ]
    assert len(hits) == 1, [str(i) for i in check_ranges(faulty)]
    issue = hits[0]
    assert issue.severity == severity
    assert issue.problem and issue.hint and len(issue.expected) == 2


def test_range_limits_can_be_overridden(summary):
    issues = check_ranges(summary, limits={"heat_rate": (8000.0, 8300.0)})
    assert [i.check for i in errors(issues)] == ["heat_rate"]


def test_issue_serialisation():
    issue = Issue("c", "p", "loc", 1.5, (0.0, 1.0), "K", ERROR, "look here")
    d = issue.to_dict()
    assert d["expected"] == [0.0, 1.0] and d["severity"] == "error"
    assert "loc" in str(issue) and "look here" in str(issue)
