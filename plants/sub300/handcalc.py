"""Independent classical heat balance of the sub300 unit (no TESPy).

This is the textbook "series" calculation of a regenerative reheat cycle:
all steam and water states follow directly from the design data, the
extraction fractions follow from one energy balance per heater solved from
the top heater downwards, and power and heat rate follow from the specific
work of the turbine sections. Leakage flows (valve stem, glands) are
included as given fractions of the main steam flow.

It shares only the parameter file and the CoolProp property functions with
the TESPy model in ``model.py`` and is used as the calibration reference
(see assumption A16 in ``design_data.yaml``): agreement shows that the
TESPy network implements the intended topology and specifications. It does
NOT show that the typical parameters match a real plant.

Units: MPa, degC, kJ/kg; flows are fractions of the main steam flow unless
stated otherwise.
"""

from __future__ import annotations

import copy
import math

from CoolProp.CoolProp import PropsSI
from scipy.optimize import brentq

FLUID = "water"


def _h_pT(p, T):
    return PropsSI("H", "P", p * 1e6, "T", T + 273.15, FLUID) / 1e3


def _h_ps(p, s):
    return PropsSI("H", "P", p * 1e6, "S", s * 1e3, FLUID) / 1e3


def _s_ph(p, h):
    return PropsSI("S", "P", p * 1e6, "H", h * 1e3, FLUID) / 1e3


def _T_sat(p):
    return PropsSI("T", "P", p * 1e6, "Q", 0, FLUID) - 273.15


def _h_sat_liquid(p):
    return PropsSI("H", "P", p * 1e6, "Q", 0, FLUID) / 1e3


def _expand(p_in, h_in, p_out, eta_s):
    """Outlet enthalpy of an expansion with isentropic efficiency eta_s."""
    return h_in - eta_s * (h_in - _h_ps(p_out, _s_ph(p_in, h_in)))


def _pump(p_in, h_in, p_out, eta_s):
    """Outlet enthalpy of a pump with isentropic efficiency eta_s."""
    return h_in + (_h_ps(p_out, _s_ph(p_in, h_in)) - h_in) / eta_s


def hand_calc(params: dict, tol: float = 1e-12, max_iter: int = 100) -> dict:
    """Solve the design heat balance by sequential heater balances.

    Leakage flows that re-enter the turbine change the inlet state of a
    section, and leakages given in kg/s need the main steam flow, so the
    sequence is repeated until main steam flow and specific work settle
    (two or three passes).

    Parameters
    ----------
    params : dict
        Plain parameters as returned by ``plants.sub300.model.load_params``.

    Returns
    -------
    dict
        ``heat_rate`` (kJ/kWh), ``main_steam_flow`` (kg/s) and
        ``gross_power`` (MW; one is given by ``rating``, the other follows),
        ``specific_power`` (MW per kg/s main steam at generator terminals),
        ``extraction_fractions`` by heater label, ``leakage_fractions`` by
        leakage label, ``bfpt_fraction``, ``reheat_fraction``,
        ``exhaust_fraction``, ``feedwater_T`` (degC) and ``feedwater_h``
        (kJ/kg).
    """
    eta = params["turbine"]["eta_mech"] * params["turbine"]["eta_gen"]
    rating = params["rating"]
    # first guess, only relevant for leakages given in kg/s
    flow_main = rating.get("main_steam_flow") or rating["gross_power"] / 1.2
    mixed_inlet: dict = {}  # section label -> inlet enthalpy after mixing
    work_old = None
    for _ in range(max_iter):
        res = _one_pass(params, flow_main, mixed_inlet)
        specific_power = res["work"] * eta / 1e3  # MW per kg/s
        if "main_steam_flow" not in rating:
            flow_new = rating["gross_power"] / specific_power
        else:
            flow_new = flow_main
        done = (
            work_old is not None
            and abs(res["work"] / work_old - 1) < tol
            and abs(flow_new / flow_main - 1) < tol
        )
        work_old, flow_main, mixed_inlet = res["work"], flow_new, res["mixed_inlet"]
        if done:
            break
    else:
        raise RuntimeError("hand calculation did not converge")

    return {
        "heat_rate": res["heat"] / (res["work"] * eta) * 3600,
        "main_steam_flow": flow_main,
        "gross_power": flow_main * specific_power,
        "specific_power": specific_power,
        "extraction_fractions": res["alpha"],
        "leakage_fractions": res["beta"],
        "bfpt_fraction": res["alpha_bfpt"],
        "reheat_fraction": res["reheat_fraction"],
        "exhaust_fraction": res["exhaust_fraction"],
        "feedwater_T": res["T_fw"],
        "feedwater_h": res["h_fw"],
        "detail": res,
    }


def _one_pass(p: dict, flow_main: float, mixed_inlet: dict) -> dict:
    """One sequential heat balance for given section inlet enthalpies."""
    sections = p["turbine"]["sections"]
    heaters = p["heaters"]
    leaks = p.get("leakages", [])
    p_cond = p["condenser"]["p"]
    fp = p["feed_pump"]
    reheat_after = p["turbine"]["reheat_after"]

    # ---- expansion line ---------------------------------------------------------
    p_main = p["main_steam"]["p"]
    h_main = _h_pT(p_main, p["main_steam"]["T"])
    # the main steam valves throttle isenthalpically to the turbine inlet
    p_in = p["main_steam"].get("p_turbine", p_main)
    h_in = h_main
    point_h = {"main_steam": h_main}  # enthalpy at every tap point
    point_p = {"main_steam": p_main}  # pressure at every tap point
    line = []  # per section: inlet and outlet state
    reheat = None
    for sec in sections:
        h_up = h_in  # state arriving from upstream, before any mixing
        h_in = mixed_inlet.get(sec["label"], h_in)
        p_out = p_cond if sec["p_out"] == "condenser" else sec["p_out"]
        if "h_out" in sec:
            h_out = sec["h_out"]
        else:
            h_out = _expand(p_in, h_in, p_out, sec["eta_s"])
        line.append({
            "label": sec["label"], "h_up": h_up, "h_in": h_in,
            "p_in": p_in, "p_out": p_out, "h_out": h_out,
        })
        point_h[sec["label"]] = h_out
        point_p[sec["label"]] = p_out
        if sec["label"] == reheat_after:
            p_hot = p_out * p["reheat"]["pr"]
            h_hot = _h_pT(p_hot, p["reheat"]["T"])
            reheat = {"p_cold": p_out, "h_cold": h_out,
                      "p_hot": p_hot, "h_hot": h_hot}
            point_h["hot_reheat"] = h_hot
            point_p["hot_reheat"] = p_hot
            p_in, h_in = p_hot, h_hot
        else:
            p_in, h_in = p_out, h_out
    ext = {s["label"]: s for s in line}

    # ---- leakage flows (fractions of main steam flow) and their enthalpy ---------
    beta = {
        lk["label"]: lk["fraction"] if "fraction" in lk else lk["m"] / flow_main
        for lk in leaks
    }
    into: dict = {}  # destination -> (flow, enthalpy flow)
    out_of: dict = {}  # source -> flow
    leak_states = {}
    for lk in leaks:
        dest = lk["destination"]
        if dest == "condenser":
            p_dest = p_cond
        elif dest in ext:
            p_dest = ext[dest]["p_in"]
        else:
            h = next(x for x in heaters if x["label"] == dest)
            p_dest = ext[h["extraction"]]["p_out"] * h["line_pr"]
        leak_states[lk["label"]] = {
            "p_source": point_p[lk["source"]], "h": point_h[lk["source"]],
            "p_destination": p_dest,
        }
        b = beta[lk["label"]]
        m, mh = into.get(lk["destination"], (0.0, 0.0))
        into[lk["destination"]] = (m + b, mh + b * point_h[lk["source"]])
        out_of[lk["source"]] = out_of.get(lk["source"], 0.0) + b

    # ---- water and drain states (independent of the flows) -----------------------
    h_hotwell = _h_sat_liquid(p_cond)
    p_w = p["condensate_pump"]["p_out"]
    h_w = _pump(p_cond, h_hotwell, p_w, p["condensate_pump"]["eta_s"])
    T_w = PropsSI("T", "P", p_w * 1e6, "H", h_w * 1e3, FLUID) - 273.15
    pump_work = 0.0
    st = []  # per heater
    for h in heaters:
        e = ext[h["extraction"]]
        p_shell = e["p_out"] * h["line_pr"]
        rec = {"label": h["label"], "type": h["type"], "h_steam": e["h_out"],
               "h_w_in": h_w}
        if h["type"] == "deaerator":
            h_dea = _h_sat_liquid(p_shell)
            rec["h_w_out"] = h_dea
            p_w = fp["p_out"]
            h_w = _pump(p_shell, h_dea, p_w, fp["eta_s"])
            pump_work = h_w - h_dea
            T_w = PropsSI("T", "P", p_w * 1e6, "H", h_w * 1e3, FLUID) - 273.15
        else:
            rec["h_drain"] = _h_pT(p_shell, T_w + h["ttd_l"])
            p_w -= h["dp_water"]
            T_w = _T_sat(p_shell) - h["ttd_u"]
            h_w = _h_pT(p_w, T_w)
            rec["h_w_out"] = h_w
        st.append(rec)
    h_fw, T_fw, p_fw = h_w, T_w, p_w

    # ---- heater balances, boiler side first ---------------------------------------
    alpha = {}
    water = 1.0  # feedwater flow per unit main steam flow
    drain_m, drain_h = 0.0, 0.0
    for rec in reversed(st):
        leak_m, leak_mh = into.get(rec["label"], (0.0, 0.0))
        if rec["type"] == "deaerator":
            # mixing heater: water = steam + drains + leakage + condensate
            a = (
                water * rec["h_w_out"] - drain_m * drain_h - leak_mh
                - (water - drain_m - leak_m) * rec["h_w_in"]
            ) / (rec["h_steam"] - rec["h_w_in"])
            water = water - a - drain_m - leak_m
            drain_m, drain_h = 0.0, 0.0
        else:
            a = (
                water * (rec["h_w_out"] - rec["h_w_in"])
                - drain_m * (drain_h - rec["h_drain"])
                - (leak_mh - leak_m * rec["h_drain"])
            ) / (rec["h_steam"] - rec["h_drain"])
            drain_m, drain_h = drain_m + a + leak_m, rec["h_drain"]
        alpha[rec["label"]] = a
    # what is left of the drain cascade flows to the condenser
    cond_m, cond_mh = drain_m, drain_m * drain_h

    # ---- feed pump turbine --------------------------------------------------------
    alpha_bfpt = 0.0
    if fp.get("drive", "turbine") == "turbine":
        e = ext[fp["bfpt"]["extraction"]]
        h_bfpt_out = _expand(
            e["p_out"] * fp["bfpt"]["line_pr"], e["h_out"], p_cond,
            fp["bfpt"]["eta_s"]
        )
        alpha_bfpt = pump_work / (e["h_out"] - h_bfpt_out)
        cond_m += alpha_bfpt
        cond_mh += alpha_bfpt * h_bfpt_out

    # ---- turbine work -------------------------------------------------------------
    leaving = {s["label"]: out_of.get(s["label"], 0.0) for s in sections}
    for h in heaters:
        leaving[h["extraction"]] += alpha[h["label"]]
    if alpha_bfpt:
        leaving[fp["bfpt"]["extraction"]] += alpha_bfpt
    flow = 1.0 - out_of.get("main_steam", 0.0)
    work = 0.0
    reheat_fraction = None
    mixed = {}
    for s in line:
        leak_m, leak_mh = into.get(s["label"], (0.0, 0.0))
        if leak_m:
            mixed[s["label"]] = (flow * s["h_up"] + leak_mh) / (flow + leak_m)
            flow += leak_m
        s["flow_in"] = flow
        work += flow * (s["h_in"] - s["h_out"])
        flow -= leaving[s["label"]]
        if s["label"] == reheat_after:
            reheat_fraction = flow
            flow -= out_of.get("hot_reheat", 0.0)

    heat = (h_main - h_fw) + reheat_fraction * (
        reheat["h_hot"] - reheat["h_cold"]
    )
    leak_m, leak_mh = into.get("condenser", (0.0, 0.0))
    cond_m += flow + leak_m
    cond_mh += flow * line[-1]["h_out"] + leak_mh
    return {
        "work": work, "heat": heat, "alpha": alpha, "beta": beta,
        "alpha_bfpt": alpha_bfpt, "reheat_fraction": reheat_fraction,
        "exhaust_fraction": flow, "T_fw": T_fw, "h_fw": h_fw, "p_fw": p_fw,
        "h_main": h_main, "p_main": p_main, "mixed_inlet": mixed,
        "line": line, "reheat": reheat, "leaks": leak_states,
        # heat rejected in the condenser per unit main steam flow, kJ/kg
        "q_condenser": cond_mh - cond_m * h_hotwell,
    }


# --------------------------------------------------------------------------
# off-design
# --------------------------------------------------------------------------
def _v_ph(p, h):
    """Specific volume (m3/kg) at p (MPa), h (kJ/kg)."""
    return 1 / PropsSI("D", "P", p * 1e6, "H", h * 1e3, FLUID)


def _h_pT_cw(p, T):
    return _h_pT(p, T)


def _lmtd(a, b):
    return (a - b) / math.log(a / b) if abs(a - b) > 1e-12 else a


def _cone_flow(ref, p_in, h_in, p_out):
    """Flow (kg/s) through a section by Stodola's cone law."""
    return ref["m"] * p_in / ref["p_in"] * (
        ref["p_in"] * ref["v_in"] / (p_in * _v_ph(p_in, h_in))
    ) ** 0.5 * abs((1 - (p_out / p_in) ** 2) / (1 - ref["pr"] ** 2)) ** 0.5


def _cone_inlet_pressure(ref, m, h_in, p_out):
    """Inlet pressure (MPa) that passes the flow m by the cone law."""
    lo = p_out * (1 + 1e-9)
    hi = p_out + 4 * ref["p_in"]
    return brentq(lambda x: _cone_flow(ref, x, h_in, p_out) - m, lo, hi,
                  xtol=1e-14, rtol=1e-14)


def _curve_pressure(params, load):
    points = sorted(
        (pt["load"], pt["p"]) for pt in params["operation"]["pressure_curve"]
    )
    if load <= points[0][0]:
        return points[0][1]
    if load >= points[-1][0]:
        return points[-1][1]
    for (l0, p0), (l1, p1) in zip(points, points[1:]):
        if l0 <= load <= l1:
            return p0 + (p1 - p0) * (load - l0) / (l1 - l0)
    raise AssertionError("unreachable")


def hand_calc_offdesign(
    params: dict, conditions: dict | None = None, tol: float = 1e-10,
    max_iter: int = 500,
) -> dict:
    """Part-load heat balance by the classical Flügel/Stodola iteration.

    Same physical assumptions as the TESPy off-design model (A18-A23 in
    ``design_data.yaml``): cone law per section with design efficiencies,
    constant heater terminal differences, boiler and reheater pressure loss
    ~ m^2 v, Martin's formula for leakages, constant mechanical loss and
    generator efficiency. All reference values come from :func:`hand_calc`,
    not from TESPy.

    ``conditions`` uses the keys of ``plants.sub300.model.solve_offdesign``.
    Returns gross power, heat rate, main steam flow, main steam and turbine
    inlet pressure, section outlet pressures, extraction flows, reheat
    pressure and flow, feedwater temperature, condenser pressure and
    leakage flows.
    """
    p = params
    c = dict(conditions or {})
    design = hand_calc(p)
    d = design["detail"]
    m_d = design["main_steam_flow"]
    eta_mech = p["turbine"]["eta_mech"]
    eta_gen = p["turbine"]["eta_gen"]
    rated = design["gross_power"]
    sections = p["turbine"]["sections"]
    reheat_after = p["turbine"]["reheat_after"]
    heaters = p["heaters"]
    dea = next(k for k, h in enumerate(heaters) if h["type"] == "deaerator")
    dp_hp_heaters = sum(h["dp_water"] for h in heaters[dea + 1:])

    # ---- design reference values --------------------------------------------------
    sec_ref = {}
    for s in d["line"]:
        h_s = _h_ps(s["p_out"], _s_ph(s["p_in"], s["h_in"]))
        sec_ref[s["label"]] = {
            "m": s["flow_in"] * m_d, "p_in": s["p_in"],
            "v_in": _v_ph(s["p_in"], s["h_in"]), "pr": s["p_out"] / s["p_in"],
            "eta_s": (s["h_in"] - s["h_out"]) / (s["h_in"] - h_s),
        }
    rh = d["reheat"]
    rh_ref = {
        "m": d["reheat_fraction"] * m_d, "dp": rh["p_cold"] - rh["p_hot"],
        "v": _v_ph(rh["p_cold"], rh["h_cold"]) + _v_ph(rh["p_hot"], rh["h_hot"]),
    }
    boiler_ref = {
        "m": m_d, "dp": d["p_fw"] - d["p_main"],
        "v": _v_ph(d["p_fw"], d["h_fw"]) + _v_ph(d["p_main"], d["h_main"]),
    }
    leak_ref = {
        lk["label"]: {
            "m": d["beta"][lk["label"]] * m_d,
            "p1": d["leaks"][lk["label"]]["p_source"],
            "v1": _v_ph(d["leaks"][lk["label"]]["p_source"],
                        d["leaks"][lk["label"]]["h"]),
            "p2": d["leaks"][lk["label"]]["p_destination"],
        }
        for lk in p.get("leakages", [])
    }
    mech_loss = (1 - eta_mech) * d["work"] * m_d  # kW
    cond = p["condenser"]
    T_sat_d = _T_sat(cond["p"])
    h_cw_in_d = _h_pT_cw(cond["cw_p"], cond["cw_T_in"])
    h_cw_out_d = _h_pT_cw(cond["cw_p"], T_sat_d - cond["ttd_u"])
    q_cond_d = d["q_condenser"] * m_d
    m_cw = q_cond_d / (h_cw_out_d - h_cw_in_d)
    UA = q_cond_d / _lmtd(T_sat_d - (T_sat_d - cond["ttd_u"]),
                          T_sat_d - cond["cw_T_in"])

    # ---- operating conditions -----------------------------------------------------
    if "gross_power" in c:
        c["load"] = c["gross_power"] / rated
    if "main_steam_flow" not in c:
        c.setdefault("load", 1.0)
        c["gross_power"] = c["load"] * rated
    mode = c.get("pressure", p.get("operation", {}).get("pressure_mode", "sliding"))
    if isinstance(mode, (int, float)):
        p_main_given = float(mode)
    elif mode == "constant":
        p_main_given = p["main_steam"]["p"]
    elif mode == "curve":
        p_main_given = _curve_pressure(p, c["load"])
    else:
        p_main_given = None
    cw_mode = c.get("condenser") is not None and "cw_T_in" in c["condenser"]
    p_cond = (
        cond["p"] if c.get("condenser") is None or cw_mode
        else c["condenser"]["p"]
    )

    # ---- fixed-point iteration ------------------------------------------------------
    q = copy.deepcopy(p)
    for sec in q["turbine"]["sections"]:
        sec.pop("h_out", None)
        sec["eta_s"] = sec_ref[sec["label"]]["eta_s"]
    ratio = c.get("main_steam_flow", m_d) / m_d if "main_steam_flow" in c else c["load"]
    m = m_d * ratio
    p_out = {s["label"]: s["p_out"] * ratio for s in d["line"][:-1]}
    p_hot = rh["p_hot"] * ratio
    p_t = d["line"][0]["p_in"] * ratio
    p_main = p_main_given or p_t
    p_fw = d["p_fw"] * ratio
    leak_m = {k: r["m"] * ratio for k, r in leak_ref.items()}
    mixed = d["mixed_inlet"]
    labels = [s["label"] for s in sections]

    for _ in range(max_iter):
        old = [m, p_t, p_main, p_hot, p_fw, p_cond, *p_out.values(), *leak_m.values()]
        for sec in q["turbine"]["sections"]:
            if sec["label"] in p_out:
                sec["p_out"] = p_out[sec["label"]]
        q["main_steam"]["p"] = p_main
        q["main_steam"]["p_turbine"] = p_t
        q["reheat"]["pr"] = p_hot / p_out[reheat_after]
        q["feed_pump"]["p_out"] = p_fw + dp_hp_heaters
        q["condenser"]["p"] = p_cond
        for lk in q.get("leakages", []):
            lk.pop("fraction", None)
            lk["m"] = leak_m[lk["label"]]
        res = _one_pass(q, m, mixed)
        mixed = res["mixed_inlet"]
        if "main_steam_flow" in c:
            m = c["main_steam_flow"]
        else:
            m = (c["gross_power"] * 1e3 / eta_gen + mech_loss) / res["work"]

        # condenser pressure from the cooling water side
        if cw_mode:
            Q = res["q_condenser"] * m
            T_in = c["condenser"]["cw_T_in"]
            h_in = _h_pT_cw(cond["cw_p"], T_in)
            T_out = PropsSI(
                "T", "P", cond["cw_p"] * 1e6, "H", (h_in + Q / m_cw) * 1e3, FLUID
            ) - 273.15
            T_sat = brentq(
                lambda T: UA * _lmtd(T - T_out, T - T_in) - Q,
                T_out + 1e-6, 120.0, xtol=1e-13,
            )
            p_cond = PropsSI("P", "T", T_sat + 273.15, "Q", 0, FLUID) / 1e6

        # pressures from the cone law, exhaust upstream
        line = {s["label"]: s for s in res["line"]}
        p_next_in = p_cond
        for k in range(len(labels) - 1, -1, -1):
            s = line[labels[k]]
            p_in = _cone_inlet_pressure(
                sec_ref[labels[k]], m * s["flow_in"], s["h_in"], p_next_in
            )
            if k == 0:
                p_t = p_in
                break
            prev = labels[k - 1]
            if prev == reheat_after:
                p_hot = p_in
                rhs = res["reheat"]
                v = _v_ph(p_out[prev], rhs["h_cold"]) + _v_ph(p_hot, rhs["h_hot"])
                dp = rh_ref["dp"] * (res["reheat_fraction"] * m / rh_ref["m"]) ** 2 \
                    * v / rh_ref["v"]
                p_out[prev] = p_hot + dp
            else:
                p_out[prev] = p_in
            p_next_in = p_out[prev]
        p_main = p_t if p_main_given is None else p_main_given
        v = _v_ph(p_fw, res["h_fw"]) + _v_ph(p_main, res["h_main"])
        p_fw = p_main + boiler_ref["dp"] * (m / boiler_ref["m"]) ** 2 * v / boiler_ref["v"]
        for name, r in leak_ref.items():
            ls = res["leaks"][name]
            p1, p2 = ls["p_source"], ls["p_destination"]
            leak_m[name] = r["m"] * p1 / r["p1"] * (
                r["p1"] * r["v1"] / (p1 * _v_ph(p1, ls["h"]))
            ) ** 0.5 * abs((1 - (p2 / p1) ** 2) / (1 - (r["p2"] / r["p1"]) ** 2)) ** 0.5

        new = [m, p_t, p_main, p_hot, p_fw, p_cond, *p_out.values(), *leak_m.values()]
        if max(abs(a / b - 1) for a, b in zip(new, old)) < tol:
            break
    else:
        raise RuntimeError("off-design hand calculation did not converge")

    work = res["work"] * m  # kW
    gross = (work - mech_loss) * eta_gen / 1e3
    return {
        "gross_power": gross,
        "load": gross / rated,
        "heat_rate": res["heat"] * m / (gross * 1e3) * 3600,
        "main_steam_flow": m,
        "p_main": p_main,
        "p_turbine_inlet": p_t,
        "throttle_pr": p_t / p_main,
        "section_p_out": dict(p_out, **{labels[-1]: p_cond}),
        "extraction_flows": {k: a * m for k, a in res["alpha"].items()},
        "bfpt_flow": res["alpha_bfpt"] * m,
        "leakage_flows": dict(leak_m),
        "hot_reheat_p": p_hot,
        "reheat_steam_flow": res["reheat_fraction"] * m,
        "feedwater_T": res["T_fw"],
        "feed_pump_p_out": p_fw + dp_hp_heaters,
        "condenser_p": p_cond,
    }


if __name__ == "__main__":
    from plants.sub300.model import load_params

    res = hand_calc(load_params())
    for key, value in res.items():
        print(f"{key}: {value}")
