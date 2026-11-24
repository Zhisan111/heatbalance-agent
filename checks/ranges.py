"""Plausibility ranges for a reheat steam turbine cycle with feed heating.

:func:`check_ranges` takes the result dictionary of a plant model (the
schema produced by ``plants.sub300.model.summarize``) and returns a list of
:class:`checks.issues.Issue` records: what is wrong, where, the actual value,
the expected range and where to look.

Severity
--------
``error``    physically impossible or far outside engineering practice
``warning``  unusual for this class of unit, worth an explanation

The default limits describe wet-cooled subcritical single-reheat units of
the 300 MW class with a turbine-driven feed pump at rated load. Some limits
move with load (:func:`limits_for_load`); the load is read from
``summary["operation"]["load"]``. All limits are engineering rules of thumb,
not design data of a specific plant; pass ``limits`` to override.

:func:`check_sweep` checks a series of part-load results for consistent
trends.
"""

from __future__ import annotations

import copy

from checks.issues import ERROR, WARNING, Issue

DEFAULT_LIMITS = {
    # gross turbine heat rate, kJ/kWh
    "heat_rate": (7600.0, 8300.0),
    # main steam per gross output, kg/kWh
    "steam_rate": (2.85, 3.25),
    # final feedwater temperature, degC
    "feedwater_T": (255.0, 290.0),
    # hot reheat pressure / main steam pressure
    "reheat_pressure_ratio": (0.16, 0.25),
    # hot reheat pressure / cold reheat pressure
    "reheat_pressure_recovery": (0.85, 0.95),
    # isentropic efficiency of whole cylinders
    "cylinder_eta": {"HP": (0.80, 0.92), "IP": (0.87, 0.95), "LP": (0.82, 0.93)},
    # isentropic efficiency of single sections between extraction points;
    # the upper physical bound is exclusive
    "section_eta_physical": (0.50, 1.00),
    "section_eta_typical": (0.70, 0.95),
    # exhaust steam quality
    "exhaust_quality": (0.88, 0.97),
    # condenser
    "condenser_pressure": (0.003, 0.012),  # MPa
    "condenser_ttd_u": (2.0, 10.0),  # K
    "cooling_water_rise": (6.0, 14.0),  # K
    # single extraction flow / main steam flow
    "extraction_fraction": (0.01, 0.10),
    # single leakage flow and sum of all leakage flows / main steam flow
    "leakage_fraction": (0.0, 0.03),
    "leakage_fraction_total": (0.0, 0.04),
    # heater shell pressure / extraction pressure
    "extraction_line_pr": (0.90, 1.00),
    # heaters; upper terminal difference depends on the pressure level
    "heater_ttd_u": {"HP": (-3.0, 3.0), "LP": (0.0, 6.0)},  # K
    "heater_ttd_l": (3.0, 12.0),  # K
    "heater_water_rise": (10.0, 45.0),  # K
    "deaerator_pressure": (0.4, 1.3),  # MPa
    # feed pump
    "bfpt_steam_fraction": (0.025, 0.060),
    "feed_pump_power_fraction": (0.015, 0.030),
    # off design
    # turbine inlet pressure / main steam pressure (main steam valves)
    "throttle_pr": (0.3, 1.0),
    # exhaust volume flow / design exhaust volume flow; below about 0.3 the
    # last stages start to ventilate
    "exhaust_volume_ratio": (0.3, None),
    # pressure difference driving the HP heater drains into the deaerator,
    # MPa; must overcome the elevation of the deaerator (assumed ~15 m)
    "drain_to_deaerator_dp": (0.15, None),
}


def limits_for_load(load: float) -> dict:
    """Default limits adjusted to a load fraction (1.0 = rated).

    Only quantities that shift systematically with load are changed; the
    shifts are linear in (1 - load) and are rules of thumb that keep the
    sliding pressure operation of the class between 40 and 100 % load
    inside the limits.
    """
    lim = copy.deepcopy(DEFAULT_LIMITS)
    u = max(0.0, 1.0 - load)
    lo, hi = lim["heat_rate"]
    lim["heat_rate"] = (lo, hi * (1 + 0.30 * u))
    lo, hi = lim["steam_rate"]
    lim["steam_rate"] = (lo, hi + 0.40 * u)
    lo, hi = lim["feedwater_T"]
    lim["feedwater_T"] = (lo - 75.0 * u, hi)
    lo, hi = lim["reheat_pressure_ratio"]
    lim["reheat_pressure_ratio"] = (lo - 0.10 * u, hi)
    lo, hi = lim["exhaust_quality"]
    lim["exhaust_quality"] = (lo, hi + 0.04 * u)
    lo, hi = lim["deaerator_pressure"]
    lim["deaerator_pressure"] = (max(0.15, lo * load), hi)
    lo, hi = lim["bfpt_steam_fraction"]
    lim["bfpt_steam_fraction"] = (lo - 0.015 * u, hi)
    lo, hi = lim["feed_pump_power_fraction"]
    lim["feed_pump_power_fraction"] = (lo - 0.012 * u, hi)
    lo, hi = lim["cooling_water_rise"]
    lim["cooling_water_rise"] = (lo * min(load, 1.0), hi)
    lo, hi = lim["condenser_ttd_u"]
    lim["condenser_ttd_u"] = (lo * min(load, 1.0), hi)
    return lim


def _outside(value, limits) -> bool:
    lo, hi = limits
    if value is None:
        return True
    return (lo is not None and value < lo) or (hi is not None and value > hi)


def check_ranges(summary: dict, limits: dict | None = None) -> list[Issue]:
    """Check a model summary against plausibility ranges.

    Parameters
    ----------
    summary : dict
        Result of ``summarize(network)``.
    limits : dict, optional
        Entries replacing those of :data:`DEFAULT_LIMITS`.

    Returns
    -------
    list of Issue
        Empty if nothing is out of range.
    """
    load = summary.get("operation", {}).get("load", 1.0)
    lim = limits_for_load(load)
    lim.update(limits or {})
    issues: list[Issue] = []

    def check(name, value, location, unit, severity, problem, hint, rng=None):
        rng = lim[name] if rng is None else rng
        if _outside(value, rng):
            issues.append(Issue(
                check=name, problem=problem, location=location,
                value=None if value is None else float(value),
                expected=tuple(rng), unit=unit, severity=severity, hint=hint,
            ))

    perf = summary["performance"]
    turb = summary["turbine"]

    # ---- operation (off design) ------------------------------------------------------
    op = summary.get("operation", {})
    if "throttle_pr" in op:
        if op["throttle_pr"] > 1 + 1e-6:
            issues.append(Issue(
                check="throttle_pr",
                problem="the turbine needs a higher inlet pressure than the "
                "main steam pressure: the operating point is infeasible",
                location="main steam valve", value=float(op["throttle_pr"]),
                expected=(None, 1.0), unit="-", severity=ERROR,
                hint="The requested main steam pressure is below the sliding "
                "pressure for this flow; raise the pressure, lower the flow "
                "or use pressure mode 'sliding'.",
            ))
        else:
            check(
                "throttle_pr", op["throttle_pr"], "main steam valve", "-",
                WARNING, "main steam valves throttle heavily",
                "Throttling at part load costs heat rate; check the pressure "
                "curve or use sliding pressure.",
            )
    design_vol = op.get("design_exhaust_volume_flow")
    if design_vol and "exhaust_volume_flow" in perf:
        check(
            "exhaust_volume_ratio", perf["exhaust_volume_flow"] / design_vol,
            "LP exhaust", "-", WARNING,
            "exhaust volume flow is low: risk of last stage ventilation, "
            "constant section efficiencies are no longer realistic",
            "Results at this load are outside the validity of the model "
            "(A18); raise the load or the back pressure.",
        )

    # ---- overall performance ---------------------------------------------------
    check(
        "heat_rate", perf["heat_rate"], "performance", "kJ/kWh", ERROR,
        "gross heat rate is outside the plausible range for this unit class",
        "Check the energy balance first, then cylinder efficiencies, "
        "condenser pressure, final feedwater temperature and whether the "
        "feed pump drive and generator losses are counted consistently.",
    )
    check(
        "steam_rate", perf["steam_rate"], "performance", "kg/kWh", WARNING,
        "main steam flow per unit output is unusual",
        "Compare main and reheat steam conditions and the extraction flows "
        "with the design data; a low value often means missing extractions.",
    )
    check(
        "feedwater_T", perf["feedwater_T"], "final feedwater", "degC", ERROR,
        "final feedwater temperature is outside the plausible range",
        "Check the pressure of the top extraction, its line pressure loss "
        "and the upper terminal difference of the last HP heater.",
    )
    main, cold, hot = turb["main_steam"], turb["cold_reheat"], turb["hot_reheat"]
    check(
        "reheat_pressure_ratio", hot["p"] / main["p"], "hot_reheat", "-",
        WARNING, "reheat pressure is unusual relative to main steam pressure",
        "Check the HP exhaust pressure; typical reheat pressure is about "
        "18-22 % of main steam pressure.",
    )
    check(
        "reheat_pressure_recovery", hot["p"] / cold["p"], "reheater", "-",
        WARNING, "reheat system pressure loss is unusual",
        "Typical loss over cold reheat line, reheater and hot reheat line "
        "is about 10 %.",
    )

    # ---- turbine -------------------------------------------------------------------
    for name, cyl in turb["cylinders"].items():
        rng = lim["cylinder_eta"].get(name)
        if rng is None:
            continue
        check(
            "cylinder_eta", cyl["eta_s"], f"{name} cylinder", "-", ERROR,
            f"{name} cylinder isentropic efficiency is implausible",
            "Check inlet and outlet pressure and enthalpy of the cylinder; "
            "for the LP cylinder also the exhaust enthalpy (exhaust loss) "
            "and condenser pressure.",
            rng=rng,
        )
    lo_phys, hi_phys = lim["section_eta_physical"]
    for sec in turb["sections"]:
        eta = sec["eta_s"]
        if not lo_phys <= eta < hi_phys:
            issues.append(Issue(
                check="section_eta_physical",
                problem="section isentropic efficiency is physically "
                "impossible or far too low",
                location=sec["label"], value=float(eta),
                expected=(lo_phys, hi_phys), unit="-", severity=ERROR,
                hint="The specified outlet enthalpy does not fit the outlet "
                "pressure: check both values and their units.",
            ))
        else:
            check(
                "section_eta_typical", eta, sec["label"], "-", WARNING,
                "section isentropic efficiency is unusual although the "
                "cylinder as a whole may be fine",
                "Extraction enthalpy or pressure of this section may be "
                "inconsistent with its neighbours (e.g. taken at a "
                "different location); compare with the cylinder efficiency.",
            )
    check(
        "exhaust_quality", perf["exhaust_x"], "LP exhaust", "-", ERROR,
        "exhaust steam quality is outside the plausible range"
        if perf["exhaust_x"] is not None
        else "exhaust steam is not in the wet region",
        "Check condenser pressure, reheat temperature and the efficiency of "
        "the last LP section.",
    )

    # ---- extraction points ---------------------------------------------------------
    ext = sorted(summary["extractions"], key=lambda e: e["no"])
    for a, b in zip(ext, ext[1:]):
        if not b["p"] < a["p"]:
            issues.append(Issue(
                check="extraction_pressure_order",
                problem="extraction pressures do not fall along the expansion",
                location=f"extraction {a['no']} -> {b['no']}",
                value=float(b["p"]), expected=(None, float(a["p"])),
                unit="MPa", severity=ERROR,
                hint="Check the order of the turbine sections and the "
                "pressure assigned to each extraction.",
            ))
    for e in ext:
        loc = f"extraction {e['no']} ({e['heater']})"
        if e["m"] <= 0:
            issues.append(Issue(
                check="extraction_flow_positive",
                problem="extraction flow is zero or negative",
                location=loc, value=float(e["m"]), expected=(0.0, None),
                unit="kg/s", severity=ERROR,
                hint="The heater receives more heat from cascaded drains "
                "than it can use: check terminal differences and the "
                "pressure of this and the neighbouring extractions.",
            ))
        else:
            check(
                "extraction_fraction", e["fraction"], loc, "-", WARNING,
                "extraction flow is unusual relative to main steam flow",
                "Check the feedwater temperature rise of the heater and the "
                "drain routing of the neighbouring heaters.",
            )

    # ---- leakages ---------------------------------------------------------------------
    leakages = summary.get("leakages", [])
    for lk in leakages:
        loc = f"leakage {lk['label']}"
        check(
            "leakage_fraction", lk["fraction"], loc, "-", WARNING,
            "leakage flow is unusual relative to main steam flow",
            "Valve stem and gland leakages are typically below 2 % of the "
            "main steam flow each; check the unit of the specified flow.",
        )
        if lk["p_destination"] > lk["p_source"] * (1 + 1e-9):
            issues.append(Issue(
                check="leakage_pressure",
                problem="leakage flows towards a higher pressure",
                location=loc, value=float(lk["p_destination"]),
                expected=(None, float(lk["p_source"])), unit="MPa",
                severity=ERROR,
                hint="The destination must be at a lower pressure than the "
                "source: check source and destination of this leakage.",
            ))
    if leakages:
        check(
            "leakage_fraction_total", sum(lk["fraction"] for lk in leakages),
            "leakages", "-", WARNING,
            "total leakage flow is unusual relative to main steam flow",
            "Check for leakages counted twice or given in the wrong unit.",
        )

    # ---- feedwater heaters ----------------------------------------------------------
    heaters = summary["heaters"]
    dea_index = next(
        (k for k, h in enumerate(heaters) if h["type"] == "deaerator"), None
    )
    for k, h in enumerate(heaters):
        loc = h["label"]
        level = "LP" if dea_index is None or k < dea_index else "HP"
        check(
            "extraction_line_pr", h["p_shell"] / h["p_extraction"], loc, "-",
            WARNING, "extraction line pressure loss is unusual",
            "Typical loss between turbine flange and heater shell is 3-8 %.",
        )
        rise = h["T_water_out"] - h["T_water_in"]
        if rise <= 0:
            issues.append(Issue(
                check="heater_water_rise",
                problem="feedwater is not heated in this heater",
                location=loc, value=float(rise), expected=(0.0, None),
                unit="K", severity=ERROR,
                hint="Check the extraction pressure order and the terminal "
                "differences of this heater and the one upstream.",
            ))
        else:
            check(
                "heater_water_rise", rise, loc, "K", WARNING,
                "feedwater temperature rise of the heater is unusual",
                "Uneven distribution of the temperature rise: check the "
                "extraction pressures of this and the neighbouring heaters.",
            )
        if h["type"] == "deaerator":
            check(
                "deaerator_pressure", h["p_shell"], loc, "MPa", WARNING,
                "deaerator pressure is unusual",
                "Check which extraction feeds the deaerator and its line "
                "pressure loss.",
            )
            if h.get("x_out") is None or abs(h["x_out"]) > 1e-6:
                issues.append(Issue(
                    check="deaerator_outlet_saturated",
                    problem="deaerator outlet is not saturated liquid",
                    location=loc, value=h.get("x_out"), expected=(0.0, 0.0),
                    unit="-", severity=ERROR,
                    hint="The deaerator outlet must be specified as "
                    "saturated liquid at shell pressure.",
                ))
            continue
        dest = h.get("drain_to")
        dest_h = next((x for x in heaters if x["label"] == dest), None)
        if dest_h is not None and dest_h["type"] == "deaerator":
            check(
                "drain_to_deaerator_dp", h["p_shell"] - dest_h["p_shell"],
                loc, "MPa", WARNING,
                "pressure difference may be too small to drain the HP "
                "heaters into the deaerator",
                "At low load the HP heater drains are usually switched to "
                "the condenser; this is not modelled.",
            )
        check(
            "heater_ttd_u", h["ttd_u"], loc, "K", ERROR,
            f"upper terminal difference is implausible for an {level} heater",
            "Check the shell pressure (extraction pressure and line loss) "
            "and the feedwater outlet temperature; negative values need a "
            "desuperheating zone and sufficiently superheated steam.",
            rng=lim["heater_ttd_u"][level],
        )
        check(
            "heater_ttd_l", h["ttd_l"], loc, "K", ERROR,
            "drain cooler approach (lower terminal difference) is implausible",
            "Check the drain outlet temperature against the feedwater inlet "
            "temperature; a heater without drain cooler should be modelled "
            "with saturated drains instead.",
        )
        if h["T_drain"] > h["T_sat"] + 1e-6:
            issues.append(Issue(
                check="heater_drain_subcooled",
                problem="drain temperature is above saturation temperature "
                "of the shell",
                location=loc, value=float(h["T_drain"]),
                expected=(None, float(h["T_sat"])), unit="degC",
                severity=ERROR,
                hint="Feedwater enters hotter than the shell saturation "
                "temperature allows: check extraction pressure order and "
                "terminal differences.",
            ))

    # ---- condenser and pumps --------------------------------------------------------
    cond = summary["condenser"]
    check(
        "condenser_pressure", cond["p"], "condenser", "MPa", WARNING,
        "condenser pressure is unusual for a wet-cooled unit",
        "Check the unit (MPa vs kPa) and the cooling water temperature.",
    )
    check(
        "condenser_ttd_u", cond["ttd_u"], "condenser", "K", WARNING,
        "condenser terminal temperature difference is unusual",
        "Check cooling water outlet temperature against the saturation "
        "temperature at condenser pressure.",
    )
    check(
        "cooling_water_rise", cond["cw_T_out"] - cond["cw_T_in"], "condenser",
        "K", WARNING, "cooling water temperature rise is unusual",
        "Check cooling water inlet temperature, condenser pressure and "
        "terminal difference.",
    )
    pumps = summary["pumps"]
    if pumps.get("feed_pump_drive") == "turbine":
        check(
            "bfpt_steam_fraction", pumps["bfpt_steam_fraction"], "BFPT", "-",
            WARNING, "feed pump turbine steam consumption is unusual",
            "Check feed pump discharge pressure and efficiency, BFPT "
            "efficiency, steam source and exhaust pressure.",
        )
    check(
        "feed_pump_power_fraction",
        pumps["feed_pump_power"] / perf["gross_power"], "feed pump", "-",
        WARNING, "feed pump power is unusual relative to gross output",
        "Check feed pump discharge pressure, suction state and efficiency.",
    )
    return issues


def check_sweep(summaries: list[dict]) -> list[Issue]:
    """Check trends over a series of operating points of one unit.

    Heat rate should rise and final feedwater temperature and extraction
    pressures should fall as load decreases. Points are sorted by load.
    """
    pts = sorted(summaries, key=lambda s: -s["operation"]["load"])
    issues = []
    for a, b in zip(pts, pts[1:]):
        la, lb = a["operation"]["load"], b["operation"]["load"]
        loc = f"load {la:.2f} -> {lb:.2f}"
        if lb >= la:
            continue
        pairs = [
            ("heat_rate", a["performance"]["heat_rate"],
             b["performance"]["heat_rate"], +1, "kJ/kWh",
             "heat rate does not rise as load falls",
             "Check throttling, condenser pressure and the efficiencies "
             "used off design."),
            ("feedwater_T", a["performance"]["feedwater_T"],
             b["performance"]["feedwater_T"], -1, "degC",
             "final feedwater temperature does not fall with load",
             "Check the top extraction pressure off design."),
        ]
        for name, va, vb, sign, unit, problem, hint in pairs:
            if sign * (vb - va) <= 0:
                issues.append(Issue(
                    check=f"sweep_{name}", problem=problem, location=loc,
                    value=float(vb),
                    expected=(va, None) if sign > 0 else (None, va),
                    unit=unit, severity=WARNING, hint=hint,
                ))
        for ea, eb in zip(a["extractions"], b["extractions"]):
            if eb["p"] >= ea["p"]:
                issues.append(Issue(
                    check="sweep_extraction_pressure",
                    problem="extraction pressure does not fall with load",
                    location=f"{loc}, extraction {ea['no']}",
                    value=float(eb["p"]), expected=(None, float(ea["p"])),
                    unit="MPa", severity=WARNING,
                    hint="With constant flow capacity the extraction "
                    "pressures scale roughly with the steam flow; check "
                    "the cone law references.",
                ))
    return issues
