"""Independent classical heat balance of the sub300 unit (no TESPy).

This is the textbook "series" calculation of a regenerative reheat cycle:
all steam and water states follow directly from the design data, the
extraction fractions follow from one energy balance per heater solved from
the top heater downwards, and power and heat rate follow from the specific
work of the turbine sections.

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


def hand_calc(params: dict) -> dict:
    """Solve the design heat balance by sequential heater balances.

    Parameters
    ----------
    params : dict
        Plain parameters as returned by ``plants.sub300.model.load_params``.

    Returns
    -------
    dict
        ``heat_rate`` (kJ/kWh), ``main_steam_flow`` (kg/s) at rated gross
        power, ``specific_power`` (MW per kg/s main steam at generator
        terminals), ``extraction_fractions`` by heater label,
        ``bfpt_fraction``, ``reheat_fraction``, ``feedwater_T`` (degC) and
        ``feedwater_h`` (kJ/kg).
    """
    p = params
    sections = p["turbine"]["sections"]
    heaters = p["heaters"]
    p_cond = p["condenser"]["p"]
    fp = p["feed_pump"]

    # ---- expansion line ---------------------------------------------------------
    p_in = p["main_steam"]["p"]
    h_in = _h_pT(p_in, p["main_steam"]["T"])
    h_main = h_in
    line = []  # per section: inlet and outlet state
    reheat = None
    for sec in sections:
        p_out = p_cond if sec["p_out"] == "condenser" else sec["p_out"]
        if "h_out" in sec:
            h_out = sec["h_out"]
        else:
            h_out = _expand(p_in, h_in, p_out, sec["eta_s"])
        line.append({
            "label": sec["label"], "h_in": h_in, "p_out": p_out, "h_out": h_out
        })
        if sec["label"] == p["turbine"]["reheat_after"]:
            p_hot = p_out * p["reheat"]["pr"]
            h_hot = _h_pT(p_hot, p["reheat"]["T"])
            reheat = {"h_cold": h_out, "h_hot": h_hot}
            p_in, h_in = p_hot, h_hot
        else:
            p_in, h_in = p_out, h_out
    ext = {s["label"]: s for s in line}

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
        if rec["type"] == "deaerator":
            # mixing heater: water = steam + drains + condensate
            a = (
                water * rec["h_w_out"] - drain_m * drain_h
                - (water - drain_m) * rec["h_w_in"]
            ) / (rec["h_steam"] - rec["h_w_in"])
            water = water - a - drain_m
            drain_m, drain_h = 0.0, 0.0
        else:
            a = (
                water * (rec["h_w_out"] - rec["h_w_in"])
                - drain_m * (drain_h - rec["h_drain"])
            ) / (rec["h_steam"] - rec["h_drain"])
            drain_m, drain_h = drain_m + a, rec["h_drain"]
        alpha[rec["label"]] = a

    # ---- feed pump turbine --------------------------------------------------------
    e = ext[fp["bfpt"]["extraction"]]
    h_bfpt_out = _expand(
        e["p_out"] * fp["bfpt"]["line_pr"], e["h_out"], p_cond,
        fp["bfpt"]["eta_s"]
    )
    alpha_bfpt = pump_work / (e["h_out"] - h_bfpt_out)

    # ---- turbine work -------------------------------------------------------------
    leaving = {s["label"]: 0.0 for s in sections}
    for h in heaters:
        leaving[h["extraction"]] += alpha[h["label"]]
    leaving[fp["bfpt"]["extraction"]] += alpha_bfpt
    flow = 1.0
    work = 0.0
    reheat_fraction = None
    for s in line:
        work += flow * (s["h_in"] - s["h_out"])
        flow -= leaving[s["label"]]
        if s["label"] == p["turbine"]["reheat_after"]:
            reheat_fraction = flow

    eta = p["turbine"]["eta_mech"] * p["turbine"]["eta_gen"]
    specific_power = work * eta / 1e3  # MW per kg/s
    heat = (h_main - h_fw) + reheat_fraction * (
        reheat["h_hot"] - reheat["h_cold"]
    )
    return {
        "heat_rate": heat / (work * eta) * 3600,
        "main_steam_flow": p["rating"]["gross_power"] / specific_power,
        "specific_power": specific_power,
        "extraction_fractions": alpha,
        "bfpt_fraction": alpha_bfpt,
        "reheat_fraction": reheat_fraction,
        "exhaust_fraction": flow,
        "feedwater_T": T_fw,
        "feedwater_h": h_fw,
    }


if __name__ == "__main__":
    from plants.sub300.model import load_params

    res = hand_calc(load_params())
    for key, value in res.items():
        print(f"{key}: {value}")
