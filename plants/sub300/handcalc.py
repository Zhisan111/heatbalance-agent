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

from CoolProp.CoolProp import PropsSI

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
    p_in = p["main_steam"]["p"]
    h_in = h_main = _h_pT(p_in, p["main_steam"]["T"])
    point_h = {"main_steam": h_main}  # enthalpy at every tap point
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
            "p_out": p_out, "h_out": h_out,
        })
        point_h[sec["label"]] = h_out
        if sec["label"] == reheat_after:
            p_hot = p_out * p["reheat"]["pr"]
            h_hot = _h_pT(p_hot, p["reheat"]["T"])
            reheat = {"h_cold": h_out, "h_hot": h_hot}
            point_h["hot_reheat"] = h_hot
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
    for lk in leaks:
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
    h_fw, T_fw = h_w, T_w

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

    # ---- feed pump turbine --------------------------------------------------------
    alpha_bfpt = 0.0
    if fp.get("drive", "turbine") == "turbine":
        e = ext[fp["bfpt"]["extraction"]]
        h_bfpt_out = _expand(
            e["p_out"] * fp["bfpt"]["line_pr"], e["h_out"], p_cond,
            fp["bfpt"]["eta_s"]
        )
        alpha_bfpt = pump_work / (e["h_out"] - h_bfpt_out)

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
        work += flow * (s["h_in"] - s["h_out"])
        flow -= leaving[s["label"]]
        if s["label"] == reheat_after:
            reheat_fraction = flow
            flow -= out_of.get("hot_reheat", 0.0)

    heat = (h_main - h_fw) + reheat_fraction * (
        reheat["h_hot"] - reheat["h_cold"]
    )
    return {
        "work": work, "heat": heat, "alpha": alpha, "beta": beta,
        "alpha_bfpt": alpha_bfpt, "reheat_fraction": reheat_fraction,
        "exhaust_fraction": flow, "T_fw": T_fw, "h_fw": h_fw,
        "mixed_inlet": mixed,
    }


if __name__ == "__main__":
    from plants.sub300.model import load_params

    res = hand_calc(load_params())
    for key, value in res.items():
        print(f"{key}: {value}")
