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
from tespy.connections import Connection, PowerConnection, Ref
from tespy.networks import Network
from tespy.tools import UserDefinedEquation
from tespy.tools.characteristics import CharLine

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


# Efficiency characteristic used off design: the section efficiency stays at
# its design value whatever the flow (A18).
_FLAT = CharLine(x=[0.0, 10.0], y=[1.0, 1.0])


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
    - ``leakages`` (optional) are steam flows leaving the main path other
      than through extractions, e.g. valve stem and gland leakage. Source:
      ``main_steam``, ``hot_reheat`` or a section label (state at its
      outlet). Destination: a heater label (its shell), ``condenser`` or a
      downstream section label (its inlet). Flow: ``fraction`` of the main
      steam flow or ``m`` in kg/s.
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

    leaks = p.get("leakages", [])
    turbine_drive = fp.get("drive", "turbine") == "turbine"

    # ---- streams leaving and entering the main steam path -----------------
    # tap point -> labels of the streams taken there
    taps = {"main_steam": [], "hot_reheat": []}
    taps.update({s["label"]: [] for s in sections})
    for h in heaters:
        taps[h["extraction"]].append(f"ext_{h['label']}")
    if turbine_drive:
        taps[fp["bfpt"]["extraction"]].append("ext_BFPT")
    for lk in leaks:
        taps[lk["source"]].append(f"leak_{lk['label']}")
    # destination -> number of leakage streams entering there
    leaks_into: dict = {}
    for lk in leaks:
        dest = lk["destination"]
        leaks_into[dest] = leaks_into.get(dest, 0) + 1

    stream_outlets: dict = {}  # stream label -> (component, outlet)
    free_inlets: dict = {}  # leakage destination -> [(component, inlet)]
    cond_inflows: list = []  # open ends returning to the condenser

    def end(src, out, label, **spec):
        """Open end of the main steam path: the next connection to create."""
        return {"src": src, "out": out, "label": label, "spec": spec}

    def close(head, dst, inl):
        c = connect(head["src"], head["out"], dst, inl, head["label"])
        if head["spec"]:
            c.set_attr(**head["spec"])
        return c

    def tap(point, name, head, label_after):
        """Insert a splitter if any stream is taken at this point."""
        users = taps[point]
        if not users:
            return head
        split = Splitter(name, num_out=1 + len(users))
        close(head, split, "in1")
        for k, user in enumerate(users):
            stream_outlets[user] = (split, f"out{k + 2}")
        return end(split, "out1", label_after)

    def join(label, head):
        """Insert a mixer in front of a section that receives leakage."""
        n = leaks_into.get(label, 0)
        if not n:
            return head
        mix = Merge(f"{label} inlet", num_in=1 + n)
        close(head, mix, "in1")
        free_inlets[label] = [(mix, f"in{k + 2}") for k in range(n)]
        return end(mix, "out1", f"{label}_in")

    # ---- steam generator and turbine train --------------------------------
    boiler = SimpleHeatExchanger("boiler")
    reheater = SimpleHeatExchanger("reheater")
    closer = CycleCloser("cycle closer")
    turbines = {s["label"]: Turbine(s["label"]) for s in sections}

    main_spec = {
        "fluid": {FLUID: 1}, "p": p["main_steam"]["p"], "T": p["main_steam"]["T"]
    }
    if "main_steam_flow" in p["rating"]:
        main_spec["m"] = p["rating"]["main_steam_flow"]
    head = end(closer, "out1", "main_steam", **main_spec)
    head = tap("main_steam", "main steam tap", head, "main_steam_to_valve")
    # Stop and control valves. Fully open at the design point (their loss is
    # contained in the HP1 efficiency, A12); in off-design operation they
    # throttle the steam to the pressure demanded by the operating mode.
    valve = Valve("main steam valve")
    valve.set_attr(pr=1)
    close(head, valve, "in1")
    head = end(valve, "out1", "turbine_inlet")

    for i, sec in enumerate(sections):
        label = sec["label"]
        turb = turbines[label]
        is_last = i == len(sections) - 1
        close(join(label, head), turb, "in1")

        # State at section outlet: pressure plus enthalpy or efficiency. In
        # off-design operation the pressures follow from Stodola's cone law
        # and the efficiency stays at its design value (A18).
        outlet_spec = {"p": p_cond if sec["p_out"] == "condenser" else sec["p_out"]}
        if "h_out" in sec:
            outlet_spec["h"] = sec["h_out"]
        else:
            turb.set_attr(eta_s=sec["eta_s"])
        outlet_spec["design"] = [k for k in ("p", "h") if k in outlet_spec]
        if is_last:
            # condenser pressure is an operating condition, see apply
            outlet_spec["design"].remove("p")
        turb.set_attr(
            design=["eta_s"], offdesign=["eta_s_char", "cone"],
            eta_s_char={"char_func": _FLAT, "param": "m"},
        )

        if is_last:
            role = "lp_exhaust"
        elif label == p["turbine"]["reheat_after"]:
            role = "cold_reheat"
        else:
            role = f"{label}_to_{sections[i + 1]['label']}"
        head = end(
            turb, "out1", f"{label}_out" if taps[label] else role, **outlet_spec
        )
        head = tap(label, f"extraction {label}", head, role)

        if is_last:
            cond_inflows.append(head)
        elif label == p["turbine"]["reheat_after"]:
            close(head, reheater, "in1")
            reheater.set_attr(
                pr=p["reheat"]["pr"], design=["pr"], offdesign=["zeta_d4"]
            )
            head = end(reheater, "out1", "hot_reheat", T=p["reheat"]["T"])
            head = tap(
                "hot_reheat", "hot reheat tap", head,
                f"hot_reheat_to_{sections[i + 1]['label']}"
            )

    # ---- feed pump turbine --------------------------------------------------
    if turbine_drive:
        bfpt = Turbine("BFPT")
        bfpt_valve = Valve("BFPT supply line")
        split, out = stream_outlets["ext_BFPT"]
        connect(split, out, bfpt_valve, "in1", "ext_BFPT")
        connect(bfpt_valve, "out1", bfpt, "in1", "steam_BFPT")
        bfpt_valve.set_attr(pr=fp["bfpt"]["line_pr"])
        bfpt.set_attr(eta_s=fp["bfpt"]["eta_s"])
        cond_inflows.append(end(bfpt, "out1", "bfpt_exhaust"))

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
        n_leaks = leaks_into.get(label, 0)
        if h["type"] == "deaerator":
            # inlets: water, extraction steam, [drain], [leakages]
            comp = Merge(label, num_in=2 + gets_drain + n_leaks)
            shell_inlets[label] = (comp, "in2")
            if gets_drain:
                drain_targets[label] = (comp, "in3")
            free_inlets[label] = [
                (comp, f"in{3 + gets_drain + j}") for j in range(n_leaks)
            ]
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
            if gets_drain or n_leaks:
                # inlets: extraction steam, [drain], [leakages]
                mix = Merge(
                    f"{label} shell inlet", num_in=1 + gets_drain + n_leaks
                )
                connect(mix, "out1", comp, "in1", f"shell_{label}")
                shell_inlets[label] = (mix, "in1")
                if gets_drain:
                    drain_targets[label] = (mix, "in2")
                free_inlets[label] = [
                    (mix, f"in{2 + gets_drain + j}") for j in range(n_leaks)
                ]
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
            # off design: discharge pressure follows from the boiler loss
            next_spec = {"p": fp["p_out"], "design": ["p"]}
        else:
            c = connect(src, out, comp, "in2", label)
            c.set_attr(**next_spec)
            src, out, label = comp, "out2", f"water_out_{h['label']}"
            next_spec = {}
    c = connect(src, out, boiler, "in1", label)
    c.set_attr(**next_spec)
    feedwater_label = label
    connect(boiler, "out1", closer, "in1", "boiler_out")
    boiler.set_attr(offdesign=["zeta_d4"])

    # steam side and drains
    for k, h in enumerate(heaters):
        label = h["label"]
        comp = heater_comps[label]
        line = Valve(f"{label} extraction line")
        line.set_attr(pr=h["line_pr"])
        split, out = stream_outlets[f"ext_{label}"]
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
                end(drain_valve, "out1", f"drain_{label}_throttled")
            )

    # leakages: throttled from their source to the destination pressure
    main_steam = next(c for c in conns if c.label == "main_steam")
    for lk in leaks:
        name = lk["label"]
        valve = Valve(f"leak {name}")
        split, out = stream_outlets[f"leak_{name}"]
        c = connect(split, out, valve, "in1", f"leak_{name}")
        if "fraction" in lk:
            c.set_attr(m=Ref(main_steam, lk["fraction"], 0), design=["m"])
        else:
            c.set_attr(m=lk["m"], design=["m"])
        if lk["destination"] == "condenser":
            cond_inflows.append(end(valve, "out1", f"leak_{name}_throttled"))
        else:
            dst, inl = free_inlets[lk["destination"]].pop(0)
            connect(valve, "out1", dst, inl, f"leak_{name}_throttled")

    # condenser
    cond_mix = Merge("condenser inlet", num_in=len(cond_inflows))
    for k, head in enumerate(cond_inflows):
        close(head, cond_mix, f"in{k + 1}")
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
    # Mechanical (bearing) losses: constant absolute value off design (A19).
    bearings = Generator("mechanical losses")
    bearings.set_attr(
        eta=p["turbine"]["eta_mech"], design=["eta"], offdesign=["delta_power"]
    )
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
        PowerConnection(
            shaft, "power_out1", bearings, "power_in", label="P_shaft"
        )
    )
    pconns.append(
        PowerConnection(
            bearings, "power_out", generator, "power_in", label="P_coupling"
        )
    )
    gross = PowerConnection(generator, "power_out", grid, "power", label="P_gross")
    if "gross_power" in p["rating"]:
        gross.set_attr(E=p["rating"]["gross_power"])
    generator.set_attr(eta=p["turbine"]["eta_gen"])
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
        "operation": {"mode": "design"},
        "leakages": [
            {k: lk[k] for k in ("label", "source", "destination")}
            for lk in leaks
        ],
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
    heater_labels = [h["label"] for h in p["heaters"]]
    leak_labels = [lk["label"] for lk in p.get("leakages", [])]
    if len(set(leak_labels)) != len(leak_labels):
        raise ModelError("leakage labels must be unique")
    path = ["main_steam"]
    for label in sections:
        path.append(label)
        if label == p["turbine"]["reheat_after"]:
            path.append("hot_reheat")
    for lk in p.get("leakages", []):
        name = lk["label"]
        if ("fraction" in lk) == ("m" in lk):
            raise ModelError(f"leakage {name}: give exactly one of fraction and m")
        if lk["source"] not in path:
            raise ModelError(f"leakage {name}: unknown source {lk['source']}")
        dest = lk["destination"]
        if dest in sections:
            # steam can only re-enter the turbine downstream of its source
            if path.index(dest) <= path.index(lk["source"]):
                raise ModelError(
                    f"leakage {name}: destination section {dest} is not "
                    f"downstream of {lk['source']}"
                )
        elif dest != "condenser" and dest not in heater_labels:
            raise ModelError(f"leakage {name}: unknown destination {dest}")
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
        cylinder data), ``extractions``, ``leakages``, ``heaters``,
        ``condenser``,
        ``pumps``, ``states`` (every connection by label) and ``power``.
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
    operation = dict(plant["operation"])
    rated = operation.get("rated_power", p_gross)
    operation.update({
        "load": p_gross / rated,
        "p_main": states["main_steam"]["p"],
        "p_turbine_inlet": states["turbine_inlet"]["p"],
        "throttle_pr": states["turbine_inlet"]["p"] / states["main_steam"]["p"],
    })
    if operation["mode"] == "design":
        operation["pressure_mode"] = "design"

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
            entry["drain_to"] = (
                plant["heaters"][k - 1]["label"] if k > 0 else "condenser"
            )
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

    leakages = []
    for lk in plant["leakages"]:
        src = states[f"leak_{lk['label']}"]
        dst = states[f"leak_{lk['label']}_throttled"]
        leakages.append({
            **lk,
            "m": src["m"], "fraction": src["m"] / main["m"], "h": src["h"],
            "p_source": src["p"], "p_destination": dst["p"],
        })

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
        "exhaust_volume_flow": exhaust["m"] * _specific_volume(
            exhaust["p"], exhaust["h"]
        ),
        "load": operation["load"],
        "mechanical_loss": p_shaft - power["P_coupling"],
        "generator_loss": power["P_coupling"] - p_gross,
    }

    return {
        "operation": operation,
        "performance": performance,
        "turbine": {
            "sections": sections,
            "cylinders": cylinders,
            "main_steam": main,
            "cold_reheat": states["cold_reheat"],
            "hot_reheat": states["hot_reheat"],
        },
        "extractions": extractions,
        "leakages": leakages,
        "heaters": heaters,
        "condenser": {
            "p": states["condenser_in"]["p"],
            "T_sat": _t_sat(states["condenser_in"]["p"]),
            "duty": -float(cond.Q.val),
            "cw_flow": cw_in["m"],
            "cw_T_in": cw_in["T"],
            "cw_T_out": cw_out["T"],
            "ttd_u": float(cond.ttd_u.val),
            "UA": float(cond.UA.val),  # W/K
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


# --------------------------------------------------------------------------
# off-design operation
# --------------------------------------------------------------------------
PRESSURE_MODES = ("sliding", "curve", "constant")


def design_state(network: Network) -> dict:
    """Return the design state needed for off-design calculations.

    The result is JSON-serialisable: ``tespy`` holds TESPy's saved network
    state (reference values for cone law, pressure loss coefficients and
    so on), ``summary`` the design summary.
    """
    if network.plant["operation"]["mode"] != "design":
        raise ModelError("design_state needs a network solved in design mode")
    return {"tespy": network.save(), "summary": summarize(network)}


def pressure_curve(params: dict, load: float) -> float:
    """Main steam pressure (MPa) of the operating curve at a load fraction.

    Piecewise linear in the points ``{load, p}`` of
    ``operation.pressure_curve``, constant outside them.
    """
    points = sorted(
        (pt["load"], pt["p"]) for pt in params["operation"]["pressure_curve"]
    )
    loads = [pt[0] for pt in points]
    if load <= loads[0]:
        return points[0][1]
    if load >= loads[-1]:
        return points[-1][1]
    for (l0, p0), (l1, p1) in zip(points, points[1:]):
        if l0 <= load <= l1:
            return p0 + (p1 - p0) * (load - l0) / (l1 - l0)
    raise AssertionError("unreachable")


def _normalise_conditions(params: dict, state: dict, conditions: dict | None) -> dict:
    """Check the operating conditions and fill in defaults.

    Keys
    ----
    load / gross_power / main_steam_flow
        Exactly one (default ``load = 1``). ``load`` is the gross power as a
        fraction of the design gross power.
    pressure
        ``"sliding"`` (valves wide open), ``"curve"`` (``operation.
        pressure_curve``, needs load or gross power), ``"constant"``
        (design main steam pressure) or a number in MPa. Default:
        ``operation.pressure_mode`` or ``"sliding"``.
    condenser
        None (design back pressure), ``{"p": MPa}`` or ``{"cw_T_in": degC}``
        (cooling water flow and condenser UA at design values).
    """
    c = dict(conditions or {})
    known = {"load", "gross_power", "main_steam_flow", "pressure", "condenser"}
    unknown = set(c) - known
    if unknown:
        raise ModelError(f"unknown operating conditions: {sorted(unknown)}")
    rated = state["summary"]["performance"]["gross_power"]
    given = [k for k in ("load", "gross_power", "main_steam_flow") if k in c]
    if len(given) > 1:
        raise ModelError("give only one of load, gross_power, main_steam_flow")
    if not given:
        c["load"] = 1.0
    if "gross_power" in c:
        c["load"] = c["gross_power"] / rated
    if "load" in c:
        if not 0.2 <= c["load"] <= 1.1:
            raise ModelError("load must be between 0.2 and 1.1")
        c["gross_power"] = c["load"] * rated

    mode = c.get("pressure", params.get("operation", {}).get("pressure_mode", "sliding"))
    if isinstance(mode, (int, float)) and not isinstance(mode, bool):
        c["pressure_mode"], c["p_main"] = "given", float(mode)
    elif mode == "sliding":
        c["pressure_mode"], c["p_main"] = "sliding", None
    elif mode == "constant":
        c["pressure_mode"] = "constant"
        c["p_main"] = state["summary"]["turbine"]["main_steam"]["p"]
    elif mode == "curve":
        if "load" not in c:
            raise ModelError("pressure 'curve' needs load or gross_power")
        if "pressure_curve" not in params.get("operation", {}):
            raise ModelError("operation.pressure_curve is not defined")
        c["pressure_mode"] = "curve"
        c["p_main"] = pressure_curve(params, c["load"])
    else:
        raise ModelError(
            f"pressure must be one of {PRESSURE_MODES} or a number in MPa"
        )
    c.pop("pressure", None)

    cond = c.get("condenser")
    if cond is not None and set(cond) not in ({"p"}, {"cw_T_in"}):
        raise ModelError("condenser must be {'p': MPa} or {'cw_T_in': degC}")
    return c


def _martin_leakage(ude):
    """Leakage flow through a labyrinth seal (Martin's formula).

    Written relative to the design point it has the same form as Stodola's
    cone law: m ~ p1 / sqrt(p1 v1) * sqrt(1 - (p2 / p1)^2).
    """
    leak, throttled = ude.conns
    r = ude.params
    p1, p2 = leak.p.val_SI, throttled.p.val_SI
    v1 = leak.calc_vol(T0=leak.T.val_SI)
    return leak.m.val_SI - r["m"] * p1 / r["p1"] * (
        r["p1"] * r["v1"] / (p1 * v1)
    ) ** 0.5 * abs(
        (1 - (p2 / p1) ** 2) / (1 - (r["p2"] / r["p1"]) ** 2)
    ) ** 0.5


def _martin_dependents(ude):
    leak, throttled = ude.conns
    return [leak.m, leak.p, leak.h, throttled.p]


def _specific_volume(p: float, h: float) -> float:
    """Specific volume (m3/kg) at p (MPa) and h (kJ/kg)."""
    return 1 / PropsSI("D", "P", p * 1e6, "H", h * 1e3, FLUID)


def _apply_conditions(network: Network, state: dict, c: dict) -> None:
    """Set the operating conditions on a network built by build_model."""
    nw = network
    design = state["summary"]
    main = nw.get_conn("main_steam")
    gross = nw.get_conn("P_gross")
    valve = nw.get_comp("main steam valve")

    if "main_steam_flow" in c:
        main.set_attr(m=c["main_steam_flow"])
        gross.set_attr(E=None)
    else:
        main.set_attr(m=None)
        gross.set_attr(E=c["gross_power"])

    if c["pressure_mode"] == "sliding":
        main.set_attr(p=None)
        valve.set_attr(pr=1)
    else:
        main.set_attr(p=c["p_main"])
        valve.set_attr(pr=None)

    exhaust = nw.get_conn("lp_exhaust")
    condenser = nw.get_comp("condenser")
    cw_in = nw.get_conn("cw_in")
    cond = c.get("condenser")
    if cond is not None and "cw_T_in" in cond:
        exhaust.set_attr(p=None)
        condenser.set_attr(ttd_u=None, UA=design["condenser"]["UA"])
        cw_in.set_attr(m=design["condenser"]["cw_flow"], T=cond["cw_T_in"])
    else:
        p_cond = design["condenser"]["p"] if cond is None else cond["p"]
        exhaust.set_attr(p=p_cond)
        condenser.set_attr(ttd_u=design["condenser"]["ttd_u"], UA=None)
        cw_in.set_attr(m=None, T=design["condenser"]["cw_T_in"])

    # leakages follow Martin's labyrinth formula (A20)
    if not getattr(nw, "_leakage_udes", None):
        udes = []
        for lk in design["leakages"]:
            name = lk["label"]
            ref = {
                "m": lk["m"], "p1": lk["p_source"] * 1e6,
                "p2": lk["p_destination"] * 1e6,
                "v1": _specific_volume(lk["p_source"], lk["h"]),
            }
            ude = UserDefinedEquation(
                f"leakage {name}", _martin_leakage, _martin_dependents,
                conns=[nw.get_conn(f"leak_{name}"),
                       nw.get_conn(f"leak_{name}_throttled")],
                params=ref,
            )
            udes.append(ude)
        if udes:
            nw.add_ude(*udes)
        nw._leakage_udes = udes


def solve_offdesign(
    params: dict, state: dict, conditions: dict | None = None,
    network: Network | None = None,
) -> Network:
    """Solve an off-design (part-load) operating point.

    Parameters
    ----------
    params : dict
        The same parameters the design state was computed with.
    state : dict
        Result of :func:`design_state`.
    conditions : dict, optional
        Operating conditions, see :func:`_normalise_conditions`. Default:
        design load with the default pressure mode.
    network : Network, optional
        A network returned by an earlier call with the same ``params`` and
        ``state``; it is re-solved starting from its current solution, which
        helps convergence when stepping through loads.

    Returns
    -------
    Network
        Solved network; ``summarize`` works as for the design case.
    """
    c = _normalise_conditions(params, state, conditions)
    fresh = network is None
    nw = build_model(params) if fresh else network
    _apply_conditions(nw, state, c)
    try:
        nw.solve(
            "offdesign", design_path=state["tespy"],
            init_path=state["tespy"] if fresh else None,
        )
        nw.assert_convergence()
    except Exception as exc:
        raise ModelError(f"off-design calculation did not converge: {exc}") from exc
    nw.plant["operation"] = {
        "mode": "offdesign",
        "pressure_mode": c["pressure_mode"],
        "conditions": {
            k: v for k, v in c.items() if k not in ("pressure_mode", "p_main")
        },
        "rated_power": state["summary"]["performance"]["gross_power"],
        "design_exhaust_volume_flow":
            state["summary"]["performance"]["exhaust_volume_flow"],
    }
    return nw


def load_sweep(
    params: dict | None = None, loads=(1.0, 0.9, 0.75, 0.5, 0.4),
    conditions: dict | None = None, step: float = 0.1,
) -> list[dict]:
    """Summaries of a series of loads, solved from high to low load.

    Intermediate loads at most ``step`` apart are solved as stepping stones
    but not returned. ``conditions`` apply to every load (no load key).
    """
    p = load_params() if params is None else params
    design = solve(build_model(p))
    state = design_state(design)
    targets = sorted(set(loads), reverse=True)
    path = [targets[0]]
    for target in targets[1:]:
        while path[-1] - target > step + 1e-9:
            path.append(path[-1] - step)
        path.append(target)
    results = {}
    nw = None
    for load in path:
        nw = solve_offdesign(
            p, state, dict(conditions or {}, load=load), network=nw
        )
        if load in targets:
            results[load] = summarize(nw)
    return [results[load] for load in sorted(loads, reverse=True)]


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
