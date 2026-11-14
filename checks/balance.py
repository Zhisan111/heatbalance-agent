"""Mass and energy conservation residuals of a solved TESPy network.

The functions work on any TESPy network (they do not know the plant). All
values are evaluated in SI units from the connection results, independently
of the equations TESPy solved:

- per component: ``sum(m_in) - sum(m_out)`` and
  ``sum(m*h)_in - sum(m*h)_out + power_in - power_out + Q_boundary``
- whole system: everything crossing the system boundary (heat added or
  removed in simple heat exchangers, power sinks and sources, externally
  driven machines, converter losses, material sources and sinks).

Usage::

    report = check_balance(network)
    report["system"]      # system residuals
    report["components"]  # one record per component
    report["issues"]      # list[Issue], empty when everything is conserved
"""

from __future__ import annotations

from checks.issues import ERROR, Issue

# Residuals are compared to the scale of the flows involved.
DEFAULT_MASS_RTOL = 1e-6
DEFAULT_ENERGY_RTOL = 1e-6

_BOUNDARY_TYPES = {"Source", "Sink", "PowerSource", "PowerSink"}
_CONVERTER_TYPES = {"Generator", "Motor"}


def _kind(comp) -> str:
    return type(comp).__name__


def _boundary_heat(comp) -> float:
    """Heat (W) entering a component from outside the network."""
    if comp.heat_inl or comp.heat_outl:
        return 0.0
    if _kind(comp) in ("SimpleHeatExchanger", "Pipe") or hasattr(comp, "Tamb"):
        return float(comp.Q.val_SI)
    return 0.0


def _boundary_power(comp) -> float:
    """Shaft power (W) entering a fluid machine that has no power port in use.

    TESPy's sign convention applies: positive for consumers (pumps,
    compressors), negative for turbines.
    """
    if comp.power_inl or comp.power_outl:
        return 0.0
    if comp.inl and comp.outl and hasattr(comp, "P") and hasattr(comp, "eta_s"):
        return float(comp.P.val_SI)
    return 0.0


def component_balances(network) -> list[dict]:
    """Mass and energy residual of every component.

    Returns
    -------
    list of dict
        Keys: ``component``, ``type``, ``mass_in``, ``mass_residual`` (kg/s),
        ``energy_residual`` (W), ``energy_scale`` (W), ``boundary_heat`` (W),
        ``boundary_power`` (W) and ``loss`` (W, converters only).
    """
    records = []
    for comp in network.comps["object"]:
        kind = _kind(comp)
        if kind in _BOUNDARY_TYPES:
            continue
        m_in = sum(c.m.val_SI for c in comp.inl)
        m_out = sum(c.m.val_SI for c in comp.outl)
        e_in = sum(c.m.val_SI * c.h.val_SI for c in comp.inl)
        e_out = sum(c.m.val_SI * c.h.val_SI for c in comp.outl)
        p_in = sum(c.E.val_SI for c in comp.power_inl)
        p_out = sum(c.E.val_SI for c in comp.power_outl)
        heat_in = sum(c.E.val_SI for c in comp.heat_inl)
        heat_out = sum(c.E.val_SI for c in comp.heat_outl)
        q_b = _boundary_heat(comp)
        p_b = _boundary_power(comp)

        loss = 0.0
        if kind in _CONVERTER_TYPES:
            # converters dissipate (1 - eta) of their input by definition
            loss = p_in - p_out
            residual = p_out - float(comp.eta.val_SI) * p_in
        else:
            residual = (
                e_in - e_out + p_in - p_out + heat_in - heat_out + q_b + p_b
            )
        scale = max(
            abs(m_in) * _enthalpy_span(comp), abs(p_in), abs(p_out),
            abs(q_b), abs(p_b), abs(heat_in), abs(heat_out), 1.0
        )
        records.append({
            "component": comp.label,
            "type": kind,
            "mass_in": m_in,
            "mass_residual": m_in - m_out,
            "energy_residual": residual,
            "energy_scale": scale,
            "boundary_heat": q_b,
            "boundary_power": p_b,
            "loss": loss,
        })
    return records


def _enthalpy_span(comp) -> float:
    """Largest specific enthalpy difference between the ports (J/kg)."""
    hs = [c.h.val_SI for c in list(comp.inl) + list(comp.outl)]
    if len(hs) < 2:
        return 0.0
    return max(max(hs) - min(hs), 1e3)


def system_balance(network) -> dict:
    """First-law and mass balance over the system boundary.

    Returns
    -------
    dict
        ``energy_in``, ``energy_out`` (W) with their itemised contributions,
        ``energy_residual`` (W), ``energy_residual_rel`` (relative to the
        heat and power input), ``mass_in``, ``mass_out`` and
        ``mass_residual`` (kg/s, material sources against sinks).
    """
    items_in: dict = {}
    items_out: dict = {}
    m_src = m_snk = 0.0
    e_src = e_snk = 0.0
    for comp in network.comps["object"]:
        kind = _kind(comp)
        label = comp.label
        if kind == "Source":
            m_src += sum(c.m.val_SI for c in comp.outl)
            e_src += sum(c.m.val_SI * c.h.val_SI for c in comp.outl)
        elif kind == "Sink":
            m_snk += sum(c.m.val_SI for c in comp.inl)
            e_snk += sum(c.m.val_SI * c.h.val_SI for c in comp.inl)
        elif kind == "PowerSource":
            items_in[label] = sum(c.E.val_SI for c in comp.power_outl)
        elif kind == "PowerSink":
            items_out[label] = sum(c.E.val_SI for c in comp.power_inl)
        elif kind in _CONVERTER_TYPES:
            items_out[f"{label} (loss)"] = (
                sum(c.E.val_SI for c in comp.power_inl)
                - sum(c.E.val_SI for c in comp.power_outl)
            )
        else:
            for value, what in (
                (_boundary_heat(comp), "heat"),
                (_boundary_power(comp), "power"),
            ):
                if value > 0:
                    items_in[f"{label} ({what})"] = value
                elif value < 0:
                    items_out[f"{label} ({what})"] = -value

    # material streams: only their net enthalpy flow is meaningful
    net_stream = e_snk - e_src
    if net_stream >= 0:
        items_out["material streams (net)"] = net_stream
    else:
        items_in["material streams (net)"] = -net_stream

    e_in = sum(items_in.values())
    e_out = sum(items_out.values())
    return {
        "energy_in": e_in,
        "energy_out": e_out,
        "energy_in_items": items_in,
        "energy_out_items": items_out,
        "energy_residual": e_in - e_out,
        "energy_residual_rel": (e_in - e_out) / max(e_in, 1.0),
        "mass_in": m_src,
        "mass_out": m_snk,
        "mass_residual": m_src - m_snk,
    }


def check_balance(
    network,
    mass_rtol: float = DEFAULT_MASS_RTOL,
    energy_rtol: float = DEFAULT_ENERGY_RTOL,
) -> dict:
    """Evaluate all residuals and report violations as issues.

    Parameters
    ----------
    network : tespy.networks.Network
        Solved network.
    mass_rtol, energy_rtol : float
        Allowed residual relative to the flow through the component (or
        through the system boundary).

    Returns
    -------
    dict
        ``system`` (see :func:`system_balance`), ``components`` (see
        :func:`component_balances`), ``max_mass_residual``,
        ``max_energy_residual_rel`` and ``issues`` (list of
        :class:`checks.issues.Issue`).
    """
    system = system_balance(network)
    components = component_balances(network)
    issues = []

    for rec in components:
        m_scale = max(abs(rec["mass_in"]), 1e-3)
        rec["mass_residual_rel"] = rec["mass_residual"] / m_scale
        rec["energy_residual_rel"] = rec["energy_residual"] / rec["energy_scale"]
        if abs(rec["mass_residual_rel"]) > mass_rtol:
            issues.append(Issue(
                check="component_mass_balance",
                problem="mass is not conserved across the component",
                location=rec["component"],
                value=rec["mass_residual"],
                expected=(-mass_rtol * m_scale, mass_rtol * m_scale),
                unit="kg/s",
                severity=ERROR,
                hint=(
                    "Check that the calculation converged and that all "
                    "inlets and outlets of this component are connected; "
                    "for a cycle closer, a residual means the loop is not "
                    "closed (missing or extra mass flow specification)."
                ),
            ))
        if abs(rec["energy_residual_rel"]) > energy_rtol:
            issues.append(Issue(
                check="component_energy_balance",
                problem="energy is not conserved across the component",
                location=rec["component"],
                value=rec["energy_residual"],
                expected=(
                    -energy_rtol * rec["energy_scale"],
                    energy_rtol * rec["energy_scale"],
                ),
                unit="W",
                severity=ERROR,
                hint=(
                    "Check convergence, then the power or heat connections "
                    "of this component (missing shaft link, wrong sign or "
                    "an unintended heat loss)."
                ),
            ))

    if abs(system["energy_residual_rel"]) > energy_rtol:
        issues.append(Issue(
            check="system_energy_balance",
            problem="energy crossing the system boundary does not balance",
            location="system",
            value=system["energy_residual"],
            expected=(
                -energy_rtol * system["energy_in"],
                energy_rtol * system["energy_in"],
            ),
            unit="W",
            severity=ERROR,
            hint=(
                "Look at the component with the largest energy residual "
                "first; then check that every heat and power stream "
                "leaving the cycle is accounted for."
            ),
        ))
    m_scale = max(system["mass_in"], 1e-3)
    if abs(system["mass_residual"]) / m_scale > mass_rtol:
        issues.append(Issue(
            check="system_mass_balance",
            problem="mass entering through sources does not leave through sinks",
            location="system",
            value=system["mass_residual"],
            expected=(-mass_rtol * m_scale, mass_rtol * m_scale),
            unit="kg/s",
            severity=ERROR,
            hint=(
                "A closed cycle must not gain or lose working fluid: check "
                "for an open end, a make-up or a blowdown stream."
            ),
        ))

    return {
        "system": system,
        "components": components,
        "max_mass_residual": max(
            (abs(r["mass_residual"]) for r in components), default=0.0
        ),
        "max_energy_residual_rel": max(
            (abs(r["energy_residual_rel"]) for r in components), default=0.0
        ),
        "issues": issues,
    }
