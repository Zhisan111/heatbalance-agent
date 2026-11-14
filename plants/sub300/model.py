"""TESPy design-point (THA) model of the sub300 typical reference unit.

sub300 is a typical 300 MW subcritical, single-reheat steam turbine cycle with
eight extractions (3 HP heaters, deaerator, 4 LP heaters) and a turbine-driven
boiler feed pump. All numbers come from ``design_data.yaml``; nothing is hard
coded here except the topology rules described in :func:`build_model`.

Public interface
----------------
- :func:`load_design_data` / :func:`load_params` read the YAML file.
- :func:`build_model` creates the TESPy network from a parameter dict.
- :func:`solve` runs the design calculation and checks convergence.
- :func:`summarize` returns a plain, JSON-serialisable result dictionary.
- :func:`run` chains the three steps.
- :func:`with_fixed_efficiencies` turns the design parameters into a
  predictive parameter set for what-if studies.

Units (network and all dictionaries): pressure MPa, temperature degC,
enthalpy kJ/kg, mass flow kg/s, power and heat MW.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from CoolProp.CoolProp import PropsSI
from tespy.components import (
    Condenser,
    CycleCloser,
    Generator,
    Merge,
    PowerBus,
    PowerSink,
    Pump,
    SimpleHeatExchanger,
    Sink,
    Source,
    Splitter,
    Turbine,
    Valve,
)
from tespy.connections import Connection, PowerConnection
from tespy.networks import Network

DATA_FILE = Path(__file__).with_name("design_data.yaml")

FLUID = "water"

UNITS = {
    "pressure": "MPa",
    "pressure_difference": "MPa",
    "temperature": "degC",
    "temperature_difference": "delta_degC",
    "enthalpy": "kJ/kg",
    "entropy": "kJ/kg/K",
    "mass_flow": "kg/s",
    "power": "MW",
    "heat": "MW",
}


class ModelError(RuntimeError):
    """Raised when the model cannot be built or does not converge."""


# --------------------------------------------------------------------------
# data handling
# --------------------------------------------------------------------------
def load_design_data(path: str | Path | None = None) -> dict:
    """Return the raw design data document (values with unit and ref)."""
    with open(path or DATA_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strip(node):
    """Replace every ``{value, unit, ref}`` leaf by its plain value."""
    if isinstance(node, dict):
        if "value" in node:
            return node["value"]
        return {k: _strip(v) for k, v in node.items() if k != "ref"}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def load_params(path: str | Path | None = None) -> dict:
    """Return the plain model parameters (``parameters`` block, no metadata).

    The result is a fresh nested dict that may be modified freely before it
    is passed to :func:`build_model`.
    """
    return _strip(load_design_data(path)["parameters"])


# --------------------------------------------------------------------------
# model construction
# --------------------------------------------------------------------------
def build_model(params: dict | None = None) -> Network:
    """Build the TESPy network of the unit.

    Topology rules (driven by ``params``):

    - ``turbine.sections`` is the expansion line in flow order. A splitter is
      placed behind every section that feeds a heater or the feed pump
      turbine. The reheater sits behind the section ``turbine.reheat_after``.
    - ``heaters`` is the feedwater train from condenser to boiler. Surface
      heaters are condensers with an integral drain cooling zone; their drains
      cascade to the previous heater in the list (deaerator or condenser at
      the end of a cascade). The deaerator is a mixing heater with saturated
      outlet, followed by the feed pump.
    - ``feed_pump.drive`` is ``turbine`` (the pump is driven by its own
      turbine, BFPT, exhausting to the main condenser) or ``motor`` (pump
      power is an external input and is not deducted from gross output).
    - ``rating`` holds either ``gross_power`` (main steam flow is a result)
      or ``main_steam_flow`` (gross power is a result).

    Parameters
    ----------
    params : dict, optional
        Plain parameter dict as returned by :func:`load_params`. Defaults to
        the design data shipped with the package.

    Returns
    -------
    tespy.networks.Network
        Unsolved network. Topology metadata needed by :func:`summarize` is
        attached as ``network.plant``.
    """
    p = copy.deepcopy(params) if params is not None else load_params()
    _validate(p)

    nw = Network(iterinfo=False)
    nw.units.set_defaults(**UNITS)

    sections = p["turbine"]["sections"]
    heaters = p["heaters"]
    fp = p["feed_pump"]
    p_cond = p["condenser"]["p"]
    conns: list = []

    def connect(src, out, dst, inl, label):
        c = Connection(src, out, dst, inl, label=label)
        conns.append(c)
        return c

    # ---- consumers of every extraction point ------------------------------
    consumers = {s["label"]: [] for s in sections}
    for h in heaters:
        consumers[h["extraction"]].append(("heater", h["label"]))
    turbine_drive = fp.get("drive", "turbine") == "turbine"
    if turbine_drive:
        consumers[fp["bfpt"]["extraction"]].append(("bfpt", "BFPT"))

    # ---- steam generator and turbine train --------------------------------
    boiler = SimpleHeatExchanger("boiler")
    reheater = SimpleHeatExchanger("reheater")
    closer = CycleCloser("cycle closer")
    turbines = {s["label"]: Turbine(s["label"]) for s in sections}

    # steam nodes that collect flows returning to the condenser
    cond_inflows: list = []  # (component, outlet, label)
    pending_exhaust_spec = None
    steam_supply: dict = {}  # consumer label -> (splitter, outlet)

    c = connect(closer, "out1", turbines[sections[0]["label"]], "in1", "main_steam")
    c.set_attr(fluid={FLUID: 1}, p=p["main_steam"]["p"], T=p["main_steam"]["T"])
    if "main_steam_flow" in p["rating"]:
        c.set_attr(m=p["rating"]["main_steam_flow"])

    for i, sec in enumerate(sections):
        label = sec["label"]
        turb = turbines[label]
        users = consumers[label]
        is_last = i == len(sections) - 1
        p_out = p_cond if sec["p_out"] == "condenser" else sec["p_out"]

        if users:
            split = Splitter(f"extraction {label}", num_out=1 + len(users))
            c = connect(turb, "out1", split, "in1", f"{label}_out")
            for k, (_, user) in enumerate(users):
                steam_supply[user] = (split, f"out{k + 2}")
            src, out = split, "out1"
        else:
            c = None
            src, out = turb, "out1"

        if is_last:
            cond_inflows.append((src, out, "lp_exhaust"))
        elif label == p["turbine"]["reheat_after"]:
            cold = connect(src, out, reheater, "in1", "cold_reheat")
            hot = connect(
                reheater, "out1", turbines[sections[i + 1]["label"]], "in1",
                "hot_reheat"
            )
            hot.set_attr(T=p["reheat"]["T"])
            reheater.set_attr(pr=p["reheat"]["pr"])
            c = c or cold
        else:
            nxt = connect(
                src, out, turbines[sections[i + 1]["label"]], "in1",
                f"{label}_to_{sections[i + 1]['label']}"
            )
            c = c or nxt

        # state at section outlet: pressure plus enthalpy or efficiency
        outlet_spec = {"p": p_out}
        if "h_out" in sec:
            outlet_spec["h"] = sec["h_out"]
        else:
            turb.set_attr(eta_s=sec["eta_s"])
        if c is None:
            # last section without extraction: its outlet connection (to the
            # condenser inlet) is created further down
            pending_exhaust_spec = outlet_spec
        else:
            c.set_attr(**outlet_spec)

    # ---- feed pump turbine --------------------------------------------------
    if turbine_drive:
        bfpt = Turbine("BFPT")
        bfpt_valve = Valve("BFPT supply line")
        split, out = steam_supply["BFPT"]
        connect(split, out, bfpt_valve, "in1", "ext_BFPT")
        connect(bfpt_valve, "out1", bfpt, "in1", "steam_BFPT")
        bfpt_valve.set_attr(pr=fp["bfpt"]["line_pr"])
        bfpt.set_attr(eta_s=fp["bfpt"]["eta_s"])
        cond_inflows.append((bfpt, "out1", "bfpt_exhaust"))

    # ---- feedwater train ------------------------------------------------------
    condenser = Condenser("condenser")
    cond_pump = Pump("condensate pump")
    feed_pump = Pump("feed pump")
    cw_in = Source("cooling water inlet")
    cw_out = Sink("cooling water outlet")

    heater_comps: dict = {}
    drain_targets: dict = {}  # heater label -> (component, inlet) for drains
    shell_inlets: dict = {}  # heater label -> (component, inlet) for steam
    for k, h in enumerate(heaters):
        label = h["label"]
        gets_drain = (
            k + 1 < len(heaters) and heaters[k + 1]["type"] == "surface"
        )
        if h["type"] == "deaerator":
            comp = Merge(label, num_in=3 if gets_drain else 2)
            shell_inlets[label] = (comp, "in2")
            if gets_drain:
                drain_targets[label] = (comp, "in3")
        else:
            comp = Condenser(label)
            comp.set_attr(
                subcooling=True, pr1=1, dp2=h["dp_water"],
                ttd_u=h["ttd_u"], ttd_l=h["ttd_l"]
            )
            # TESPy's Condenser is a lumped model whose upper terminal
            # difference refers to the saturation temperature and is bounded
            # at 0 K. A heater with a desuperheating zone may heat the
            # feedwater above saturation temperature (negative TTD), so the
            # bound is lifted. The energy balance is not affected.
            comp.ttd_u.min_val = -float("inf")
            comp.ttd_min.min_val = -float("inf")
            if gets_drain:
                mix = Merge(f"{label} shell inlet", num_in=2)
                connect(mix, "out1", comp, "in1", f"shell_{label}")
                shell_inlets[label] = (mix, "in1")
                drain_targets[label] = (mix, "in2")
            else:
                shell_inlets[label] = (comp, "in1")
        heater_comps[label] = comp

    # water side, condenser -> boiler
    c = connect(condenser, "out1", cond_pump, "in1", "hotwell")
    cond_pump.set_attr(eta_s=p["condensate_pump"]["eta_s"])
    src, out, label = cond_pump, "out1", "cond_pump_out"
    next_spec = {"p": p["condensate_pump"]["p_out"]}
    for h in heaters:
        comp = heater_comps[h["label"]]
        if h["type"] == "deaerator":
            lcv = Valve("deaerator level control valve")
            c = connect(src, out, lcv, "in1", label)
            c.set_attr(**next_spec)
            connect(lcv, "out1", comp, "in1", f"water_into_{h['label']}")
            c = connect(comp, "out1", feed_pump, "in1", f"water_out_{h['label']}")
            c.set_attr(x=0)
            feed_pump.set_attr(eta_s=fp["eta_s"])
            src, out, label = feed_pump, "out1", "feed_pump_out"
            next_spec = {"p": fp["p_out"]}
        else:
            c = connect(src, out, comp, "in2", label)
            c.set_attr(**next_spec)
            src, out, label = comp, "out2", f"water_out_{h['label']}"
            next_spec = {}
    c = connect(src, out, boiler, "in1", label)
    c.set_attr(**next_spec)
    feedwater_label = label
    connect(boiler, "out1", closer, "in1", "boiler_out")

    # steam side and drains
    for k, h in enumerate(heaters):
        label = h["label"]
        comp = heater_comps[label]
        line = Valve(f"{label} extraction line")
        line.set_attr(pr=h["line_pr"])
        split, out = steam_supply[label]
        connect(split, out, line, "in1", f"ext_{label}")
        dst, inl = shell_inlets[label]
        connect(line, "out1", dst, inl, f"steam_{label}")
        if h["type"] != "surface":
            continue
        drain_valve = Valve(f"{label} drain valve")
        connect(comp, "out1", drain_valve, "in1", f"drain_{label}")
        if k > 0:
            dst, inl = drain_targets[heaters[k - 1]["label"]]
            connect(drain_valve, "out1", dst, inl, f"drain_{label}_throttled")
        else:
            cond_inflows.append(
                (drain_valve, "out1", f"drain_{label}_throttled")
            )

    # condenser
    cond_mix = Merge("condenser inlet", num_in=len(cond_inflows))
    for k, (src, out, label) in enumerate(cond_inflows):
        c = connect(src, out, cond_mix, f"in{k + 1}", label)
        if label == "lp_exhaust" and pending_exhaust_spec is not None:
            c.set_attr(**pending_exhaust_spec)
    connect(cond_mix, "out1", condenser, "in1", "condenser_in")
    c = connect(cw_in, "out1", condenser, "in2", "cw_in")
    c.set_attr(
        fluid={FLUID: 1}, T=p["condenser"]["cw_T_in"], p=p["condenser"]["cw_p"]
    )
    connect(condenser, "out2", cw_out, "in1", "cw_out")
    condenser.set_attr(pr1=1, pr2=1, ttd_u=p["condenser"]["ttd_u"])

    nw.add_conns(*conns)

    # ---- power side -----------------------------------------------------------
    shaft = PowerBus("shaft", num_in=len(sections), num_out=1)
    generator = Generator("generator")
    grid = PowerSink("grid")
    pconns = [
        PowerConnection(
            turbines[s["label"]], "power", shaft, f"power_in{k + 1}",
            label=f"P_{s['label']}"
        )
        for k, s in enumerate(sections)
    ]
    pconns.append(
        PowerConnection(shaft, "power_out1", generator, "power_in", label="P_shaft")
    )
    gross = PowerConnection(generator, "power_out", grid, "power", label="P_gross")
    if "gross_power" in p["rating"]:
        gross.set_attr(E=p["rating"]["gross_power"])
    generator.set_attr(eta=p["turbine"]["eta_mech"] * p["turbine"]["eta_gen"])
    pconns.append(gross)
    if turbine_drive:
        pconns.append(
            PowerConnection(bfpt, "power", feed_pump, "power", label="P_BFPT")
        )
    nw.add_conns(*pconns)

    nw.plant = {
        "params": p,
        "sections": [s["label"] for s in sections],
        "cylinders": {s["label"]: s["cylinder"] for s in sections},
        "heaters": [
            {"label": h["label"], "type": h["type"], "extraction": h["extraction"]}
            for h in heaters
        ],
        "feedwater": feedwater_label,
        "exhaust": "lp_exhaust",
        "feed_pump_drive": "turbine" if turbine_drive else "motor",
    }
    return nw


def _validate(p: dict) -> None:
    sections = [s["label"] for s in p["turbine"]["sections"]]
    if len(set(sections)) != len(sections):
        raise ModelError("turbine section labels must be unique")
    if p["turbine"]["reheat_after"] not in sections[:-1]:
        raise ModelError("turbine.reheat_after must name a non-final section")
    for s in p["turbine"]["sections"]:
        if ("h_out" in s) == ("eta_s" in s):
            raise ModelError(
                f"section {s['label']}: give exactly one of h_out and eta_s"
            )
    if p["turbine"]["sections"][-1]["p_out"] != "condenser":
        raise ModelError("the last turbine section must exhaust to 'condenser'")
    kinds = [h["type"] for h in p["heaters"]]
    if kinds.count("deaerator") != 1:
        raise ModelError("exactly one deaerator is required")
    for h in p["heaters"]:
        if h["type"] not in ("surface", "deaerator"):
            raise ModelError(f"heater {h['label']}: unknown type {h['type']}")
        if h["extraction"] not in sections:
            raise ModelError(
                f"heater {h['label']}: unknown extraction {h['extraction']}"
            )
    drive = p["feed_pump"].get("drive", "turbine")
    if drive not in ("turbine", "motor"):
        raise ModelError("feed_pump.drive must be 'turbine' or 'motor'")
    if drive == "turbine" and (
        p["feed_pump"]["bfpt"]["extraction"] not in sections
    ):
        raise ModelError("feed_pump.bfpt.extraction: unknown section")
    if ("gross_power" in p["rating"]) == ("main_steam_flow" in p["rating"]):
        raise ModelError(
            "rating: give exactly one of gross_power and main_steam_flow"
        )


# --------------------------------------------------------------------------
# solving
# --------------------------------------------------------------------------
def solve(network: Network) -> Network:
    """Solve the design case; raise :class:`ModelError` if not converged."""
    network.solve("design")
    try:
        network.assert_convergence()
    except Exception as exc:  # TESPy raises its own assertion type
        raise ModelError(f"design calculation did not converge: {exc}") from exc
    return network


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
def _quality(p: float, h: float) -> float | None:
    """Vapour mass fraction, or None outside the two-phase region."""
    if p * 1e6 >= PropsSI("Pcrit", FLUID):
        return None
    h_l = PropsSI("H", "P", p * 1e6, "Q", 0, FLUID) / 1e3
    h_v = PropsSI("H", "P", p * 1e6, "Q", 1, FLUID) / 1e3
    x = (h - h_l) / (h_v - h_l)
    tol = 1e-9
    if x < -tol or x > 1 + tol:
        return None
    return min(max(x, 0.0), 1.0)


def _state(c) -> dict:
    """Plain state dict of a connection in network units.

    ``x`` is the vapour mass fraction for saturated and two-phase states and
    None for subcooled liquid and superheated steam.
    """
    return {
        "m": float(c.m.val),
        "p": float(c.p.val),
        "T": float(c.T.val),
        "h": float(c.h.val),
        "s": float(c.s.val),
        "x": _quality(float(c.p.val), float(c.h.val)),
    }


def _h_isentropic(p_out: float, p_in: float, h_in: float) -> float:
    """Enthalpy (kJ/kg) after isentropic expansion from (p_in, h_in) to p_out."""
    s = PropsSI("S", "P", p_in * 1e6, "H", h_in * 1e3, FLUID)
    return PropsSI("H", "P", p_out * 1e6, "S", s, FLUID) / 1e3


def _t_sat(p: float) -> float:
    return PropsSI("T", "P", p * 1e6, "Q", 0, FLUID) - 273.15


def summarize(network: Network) -> dict:
    """Collect the key results of a solved network.

    Returns
    -------
    dict
        ``performance`` (power, heat rate, flows), ``turbine`` (section and
        cylinder data), ``extractions``, ``heaters``, ``condenser``,
        ``pumps`` and ``states`` (every connection by label).
    """
    nw = network
    plant = nw.plant
    conn = nw.get_conn
    comp = nw.get_comp

    states = {
        label: _state(c)
        for label, c in zip(nw.conns["object"].index, nw.conns["object"])
        if isinstance(c, Connection) and not isinstance(c, PowerConnection)
    }
    power = {
        label: float(c.E.val)
        for label, c in zip(nw.conns["object"].index, nw.conns["object"])
        if isinstance(c, PowerConnection)
    }

    main = states["main_steam"]
    fw = states[plant["feedwater"]]
    exhaust = states[plant["exhaust"]]
    q_boiler = float(comp("boiler").Q.val)
    q_reheat = float(comp("reheater").Q.val)
    q_in = q_boiler + q_reheat
    p_gross = power["P_gross"]
    p_shaft = power["P_shaft"]

    # ---- turbine sections and cylinders ---------------------------------------
    sections = []
    cyl_states: dict = {}
    for label in plant["sections"]:
        t = comp(label)
        s_in, s_out = _state(t.inl[0]), _state(t.outl[0])
        sections.append({
            "label": label,
            "cylinder": plant["cylinders"][label],
            "p_in": s_in["p"], "T_in": s_in["T"], "h_in": s_in["h"],
            "p_out": s_out["p"], "T_out": s_out["T"], "h_out": s_out["h"],
            "x_out": s_out["x"],
            "m": s_in["m"],
            "eta_s": float(t.eta_s.val),
            "power": -float(t.P.val),
        })
        cyl = cyl_states.setdefault(
            plant["cylinders"][label], {"in": s_in, "power": 0.0}
        )
        cyl["out"] = s_out
        cyl["power"] += -float(t.P.val)
    cylinders = {}
    for name, c in cyl_states.items():
        h_s = _h_isentropic(c["out"]["p"], c["in"]["p"], c["in"]["h"])
        cylinders[name] = {
            "p_in": c["in"]["p"], "h_in": c["in"]["h"],
            "p_out": c["out"]["p"], "h_out": c["out"]["h"],
            "eta_s": (c["in"]["h"] - c["out"]["h"]) / (c["in"]["h"] - h_s),
            "power": c["power"],
        }

    # ---- heaters and extractions ------------------------------------------------
    heaters = []
    extractions = []
    for k, h in enumerate(plant["heaters"]):
        label = h["label"]
        ext = states[f"ext_{label}"]
        steam = states[f"steam_{label}"]
        c = comp(label)
        extractions.append({
            "no": len(plant["heaters"]) - k,
            "heater": label,
            "section": h["extraction"],
            "m": ext["m"], "fraction": ext["m"] / main["m"],
            "p": ext["p"], "T": ext["T"], "h": ext["h"], "x": ext["x"],
        })
        entry = {
            "label": label,
            "type": h["type"],
            "p_extraction": ext["p"],
            "p_shell": steam["p"],
            "T_sat": _t_sat(steam["p"]),
            "m_steam": ext["m"],
        }
        if h["type"] == "surface":
            w_in, w_out = _state(c.inl[1]), _state(c.outl[1])
            drain = _state(c.outl[0])
            entry.update({
                "T_water_in": w_in["T"], "T_water_out": w_out["T"],
                "h_water_in": w_in["h"], "h_water_out": w_out["h"],
                "m_water": w_in["m"],
                "T_drain": drain["T"], "h_drain": drain["h"],
                "m_drain": drain["m"],
                "ttd_u": entry["T_sat"] - w_out["T"],
                "ttd_l": drain["T"] - w_in["T"],
                "duty": -float(c.Q.val),
            })
        else:
            w_in, w_out = _state(c.inl[0]), _state(c.outl[0])
            entry.update({
                "T_water_in": w_in["T"], "T_water_out": w_out["T"],
                "h_water_in": w_in["h"], "h_water_out": w_out["h"],
                "m_water": w_out["m"],
                "x_out": w_out["x"],
            })
        heaters.append(entry)
    extractions.sort(key=lambda e: e["no"])

    drive = plant["feed_pump_drive"]
    bfpt_flow = states["steam_BFPT"]["m"] if drive == "turbine" else 0.0
    cw_in, cw_out = states["cw_in"], states["cw_out"]
    cond = comp("condenser")

    performance = {
        "gross_power": p_gross,
        "shaft_power": p_shaft,
        "heat_input": q_in,
        "heat_input_boiler": q_boiler,
        "heat_input_reheater": q_reheat,
        "heat_rate": q_in / p_gross * 3600,  # kJ/kWh
        "efficiency": p_gross / q_in,
        "main_steam_flow": main["m"],
        "main_steam_flow_tph": main["m"] * 3.6,
        "steam_rate": main["m"] * 3.6 / p_gross,  # kg/kWh
        "reheat_steam_flow": states["hot_reheat"]["m"],
        "feedwater_T": fw["T"],
        "feedwater_p": fw["p"],
        "exhaust_flow": exhaust["m"],
        "exhaust_h": exhaust["h"],
        "exhaust_x": exhaust["x"],
    }

    return {
        "performance": performance,
        "turbine": {
            "sections": sections,
            "cylinders": cylinders,
            "main_steam": main,
            "cold_reheat": states["cold_reheat"],
            "hot_reheat": states["hot_reheat"],
        },
        "extractions": extractions,
        "heaters": heaters,
        "condenser": {
            "p": states["condenser_in"]["p"],
            "T_sat": _t_sat(states["condenser_in"]["p"]),
            "duty": -float(cond.Q.val),
            "cw_flow": cw_in["m"],
            "cw_T_in": cw_in["T"],
            "cw_T_out": cw_out["T"],
            "ttd_u": float(cond.ttd_u.val),
        },
        "pumps": {
            "feed_pump_power": float(comp("feed pump").P.val),
            "feed_pump_drive": drive,
            "bfpt_steam_flow": bfpt_flow,
            "bfpt_steam_fraction": bfpt_flow / main["m"],
            "bfpt_power": power.get("P_BFPT", 0.0),
            "condensate_pump_power": float(comp("condensate pump").P.val),
        },
        "states": states,
        "power": power,
    }


def with_fixed_efficiencies(params: dict, summary: dict) -> dict:
    """Return a copy of ``params`` with section efficiencies instead of
    outlet enthalpies.

    The design data define the expansion line by extraction enthalpies, so
    the section efficiencies are results. For what-if studies derived from
    the design point (other pressures, temperatures or flows) the expansion
    line must be allowed to move: this helper freezes the efficiencies found
    in ``summary`` and drops the fixed outlet enthalpies.
    """
    p = copy.deepcopy(params)
    eta = {s["label"]: s["eta_s"] for s in summary["turbine"]["sections"]}
    for sec in p["turbine"]["sections"]:
        sec.pop("h_out", None)
        sec["eta_s"] = eta[sec["label"]]
    return p


def run(params: dict | None = None) -> tuple[Network, dict]:
    """Build, solve and summarise in one call."""
    nw = solve(build_model(params))
    return nw, summarize(nw)


def format_summary(summary: dict) -> str:
    """Human-readable short report of a summary dict."""
    perf = summary["performance"]
    lines = [
        f"Gross power        {perf['gross_power']:10.2f} MW",
        f"Heat rate          {perf['heat_rate']:10.1f} kJ/kWh",
        f"Cycle efficiency   {perf['efficiency'] * 100:10.2f} %",
        f"Main steam flow    {perf['main_steam_flow_tph']:10.1f} t/h",
        f"Reheat steam flow  {perf['reheat_steam_flow'] * 3.6:10.1f} t/h",
        f"Final feedwater T  {perf['feedwater_T']:10.1f} degC",
        f"Exhaust            {perf['exhaust_flow'] * 3.6:10.1f} t/h, "
        f"h = {perf['exhaust_h']:.1f} kJ/kg, x = {perf['exhaust_x']:.4f}",
        "",
        "Cylinder efficiencies: " + ", ".join(
            f"{k} {v['eta_s'] * 100:.2f} %"
            for k, v in summary["turbine"]["cylinders"].items()
        ),
        "",
        " No heater  p_ext/MPa   T/degC  h/kJ/kg   m/(t/h)  T_w,out/degC",
    ]
    heaters = {h["label"]: h for h in summary["heaters"]}
    for e in summary["extractions"]:
        h = heaters[e["heater"]]
        lines.append(
            f"{e['no']:3d} {e['heater']:<6} {e['p']:10.4f} {e['T']:8.1f} "
            f"{e['h']:8.1f} {e['m'] * 3.6:9.2f} {h['T_water_out']:10.1f}"
        )
    pumps = summary["pumps"]
    lines += [
        "",
        f"Feed pump ({pumps['feed_pump_drive']} driven) "
        f"{pumps['feed_pump_power']:.2f} MW, "
        f"BFPT steam {pumps['bfpt_steam_flow'] * 3.6:.2f} t/h",
        f"Condenser duty {summary['condenser']['duty']:.1f} MW, "
        f"cooling water {summary['condenser']['cw_flow'] * 3.6:.0f} t/h",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_summary(run()[1]))
