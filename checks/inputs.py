"""Input plausibility of a parameter set, before anything is solved.

:func:`check_inputs` takes plain parameters (``model.load_params`` format)
and returns :class:`checks.issues.Issue` records. Errors mean the model
should not be solved: the values are physically impossible or far outside
the validity of the model. Catching them here gives an agent a precise
message instead of a non-converging solver.
"""

from __future__ import annotations

from CoolProp.CoolProp import PropsSI

from checks.issues import ERROR, WARNING, Issue

FLUID = "water"


def _h_pT(p, T):
    return PropsSI("H", "P", p * 1e6, "T", T + 273.15, FLUID) / 1e3


def _eta(p_in, h_in, p_out, h_out):
    s = PropsSI("S", "P", p_in * 1e6, "H", h_in * 1e3, FLUID)
    h_s = PropsSI("H", "P", p_out * 1e6, "S", s, FLUID) / 1e3
    return (h_in - h_out) / (h_in - h_s)


def check_inputs(p: dict) -> list[Issue]:
    """Check a plain parameter set; see the module docstring."""
    issues: list[Issue] = []

    def check(name, value, location, rng, unit, severity, problem, hint):
        lo, hi = rng
        if value is None or (lo is not None and value < lo) or (
            hi is not None and value > hi
        ):
            issues.append(Issue(
                check=name, problem=problem, location=location,
                value=None if value is None else float(value),
                expected=(lo, hi), unit=unit, severity=severity, hint=hint,
            ))

    # ---- efficiencies --------------------------------------------------------------
    effs = [
        ("turbine.eta_mech", p["turbine"]["eta_mech"], (0.97, 1.0)),
        ("turbine.eta_gen", p["turbine"]["eta_gen"], (0.95, 1.0)),
        ("condensate_pump.eta_s", p["condensate_pump"]["eta_s"], (0.5, 0.95)),
        ("feed_pump.eta_s", p["feed_pump"]["eta_s"], (0.5, 0.95)),
    ]
    if p["feed_pump"].get("drive", "turbine") == "turbine":
        effs.append(("feed_pump.bfpt.eta_s", p["feed_pump"]["bfpt"]["eta_s"], (0.5, 0.95)))
    for sec in p["turbine"]["sections"]:
        if "eta_s" in sec:
            effs.append((f"turbine.sections[{sec['label']}].eta_s", sec["eta_s"], (0.5, 0.95)))
    for loc, value, rng in effs:
        check("input_efficiency", value, loc, (0.0, 1.0), "-", ERROR,
              "efficiency must be between 0 and 1",
              "Efficiencies are fractions, not percent.")
        if 0 < value <= 1:
            check("input_efficiency_typical", value, loc, rng, "-", WARNING,
                  "efficiency is unusual for this component",
                  "Check the value against the component type.")

    # ---- live steam, reheat, condenser -------------------------------------------------
    ms, rh, cond = p["main_steam"], p["reheat"], p["condenser"]
    check("input_main_steam_p", ms["p"], "main_steam.p", (5.0, 22.0), "MPa",
          ERROR, "main steam pressure is outside the subcritical range of "
          "the model", "Units are MPa (absolute).")
    for loc, T in (("main_steam.T", ms["T"]), ("reheat.T", rh["T"])):
        check("input_steam_T", T, loc, (450.0, 620.0), "degC", ERROR,
              "steam temperature is outside the range of the model",
              "Units are degC.")
    check("input_reheat_pr", rh["pr"], "reheat.pr", (0.80, 1.0), "-", ERROR,
          "reheat pressure ratio (hot / cold) is implausible",
          "pr is outlet / inlet pressure, typically 0.88-0.92.")
    check("input_condenser_p", cond["p"], "condenser.p", (0.002, 0.030),
          "MPa", ERROR, "condenser pressure is implausible",
          "Units are MPa: 4.9 kPa is 0.0049.")
    check("input_condenser_ttd", cond["ttd_u"], "condenser.ttd_u",
          (1.0, 15.0), "K", WARNING, "condenser terminal difference is unusual",
          "Typical values are 3-6 K.")
    if cond["cw_T_in"] + cond["ttd_u"] + 1 > PropsSI(
        "T", "P", cond["p"] * 1e6, "Q", 0, FLUID
    ) - 273.15:
        issues.append(Issue(
            check="input_condenser_cooling", location="condenser",
            problem="cooling water is too warm for the condenser pressure",
            value=float(cond["cw_T_in"]), expected=(None, None), unit="degC",
            severity=ERROR,
            hint="Saturation temperature at condenser pressure must exceed "
            "cooling water inlet temperature plus terminal difference.",
        ))

    # ---- expansion line ------------------------------------------------------------------
    sections = p["turbine"]["sections"]
    p_in, h_in = ms["p"], _h_pT(ms["p"], ms["T"])
    for k, sec in enumerate(sections):
        loc = f"turbine.sections[{sec['label']}]"
        last = k == len(sections) - 1
        p_out = cond["p"] if sec["p_out"] == "condenser" else sec["p_out"]
        if not isinstance(p_out, (int, float)) or p_out <= 0 or p_out >= p_in:
            issues.append(Issue(
                check="input_expansion_pressure",
                problem="section outlet pressure must be positive and below "
                "its inlet pressure", location=f"{loc}.p_out",
                value=float(p_out) if isinstance(p_out, (int, float)) else None,
                expected=(0.0, float(p_in)), unit="MPa", severity=ERROR,
                hint="Extraction pressures must fall along the expansion; "
                "check the order and the units (MPa).",
            ))
            return issues  # the rest of the line is meaningless
        if last and sec["p_out"] != "condenser":
            issues.append(Issue(
                check="input_expansion_pressure",
                problem="the last section must exhaust to 'condenser'",
                location=f"{loc}.p_out", value=float(p_out),
                expected=(None, None), unit="MPa", severity=ERROR,
                hint="Set p_out of the last section to 'condenser'.",
            ))
        if "h_out" in sec:
            try:
                eta = _eta(p_in, h_in, p_out, sec["h_out"])
            except ValueError:
                eta = None
            check("input_section_efficiency", eta, f"{loc}.h_out",
                  (0.5, 0.999), "-", ERROR,
                  "outlet enthalpy implies an impossible or implausible "
                  "section efficiency",
                  "The outlet enthalpy does not fit the inlet state and "
                  "outlet pressure; units are kJ/kg.")
            h_out = sec["h_out"]
        else:
            s = PropsSI("S", "P", p_in * 1e6, "H", h_in * 1e3, FLUID)
            h_s = PropsSI("H", "P", p_out * 1e6, "S", s, FLUID) / 1e3
            h_out = h_in - sec.get("eta_s", 0.85) * (h_in - h_s)
        if sec["label"] == p["turbine"]["reheat_after"]:
            p_in = p_out * rh["pr"]
            h_in = _h_pT(p_in, rh["T"])
        else:
            p_in, h_in = p_out, h_out

    # ---- heaters --------------------------------------------------------------------------
    dea = None
    for h in p["heaters"]:
        loc = f"heaters[{h['label']}]"
        check("input_line_pr", h["line_pr"], f"{loc}.line_pr", (0.85, 1.0),
              "-", ERROR, "extraction line pressure ratio is implausible",
              "line_pr is heater shell pressure / extraction pressure.")
        if h["type"] == "deaerator":
            dea = h
            continue
        check("input_heater_ttd_u", h["ttd_u"], f"{loc}.ttd_u", (-5.0, 15.0),
              "K", ERROR, "upper terminal difference is implausible",
              "Values below about -3 K need a large desuperheating zone.")
        check("input_heater_ttd_l", h["ttd_l"], f"{loc}.ttd_l", (0.5, 20.0),
              "K", ERROR, "drain cooler approach is implausible",
              "Typical value 5.6 K.")
        check("input_heater_dp", h["dp_water"], f"{loc}.dp_water", (0.0, 1.0),
              "MPa", ERROR, "water-side pressure drop is implausible",
              "Units are MPa.")

    fp = p["feed_pump"]
    check("input_feed_pump_p", fp["p_out"], "feed_pump.p_out",
          (ms["p"] * 1.0, ms["p"] * 1.5), "MPa", ERROR,
          "feed pump discharge pressure must exceed main steam pressure",
          "Include boiler, heater and valve pressure losses.")
    if dea is not None:
        sec = next(s for s in sections if s["label"] == dea["extraction"])
        p_dea = sec["p_out"] * dea["line_pr"]
        check("input_condensate_pump_p", p["condensate_pump"]["p_out"],
              "condensate_pump.p_out", (p_dea + 0.2, None), "MPa", ERROR,
              "condensate pump discharge pressure is too low to feed the "
              "deaerator", "It must cover deaerator pressure, LP heater and "
              "control valve losses.")

    for lk in p.get("leakages", []):
        loc = f"leakages[{lk['label']}]"
        if "fraction" in lk:
            check("input_leakage", lk["fraction"], f"{loc}.fraction",
                  (0.0, 0.05), "-", ERROR,
                  "leakage fraction of main steam flow is implausible",
                  "Fractions, not percent: 1.7 % is 0.017.")

    curve = p.get("operation", {}).get("pressure_curve")
    if curve:
        pts = sorted((pt["load"], pt["p"]) for pt in curve)
        for (l0, p0), (l1, p1) in zip(pts, pts[1:]):
            if p1 < p0:
                issues.append(Issue(
                    check="input_pressure_curve",
                    problem="main steam pressure falls with rising load",
                    location="operation.pressure_curve", value=float(p1),
                    expected=(float(p0), None), unit="MPa", severity=ERROR,
                    hint="The curve must be non-decreasing in load.",
                ))
        if pts[-1][1] > ms["p"] * 1.05:
            issues.append(Issue(
                check="input_pressure_curve",
                problem="pressure curve exceeds the design main steam pressure",
                location="operation.pressure_curve", value=float(pts[-1][1]),
                expected=(None, float(ms["p"])), unit="MPa", severity=WARNING,
                hint="Keep the curve at or below the rated pressure.",
            ))
    return issues
