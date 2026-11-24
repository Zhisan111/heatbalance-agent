"""Workspace of reference plants and derived models (no MCP dependency).

The MCP server in ``server.py`` is a thin wrapper around :class:`Workspace`.
Every method returns plain, JSON-serialisable data. Physical problems are
reported as structured issues inside the result; :class:`ServiceError` is
raised only for invalid requests (unknown model, malformed patch).
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import yaml

from checks.balance import check_balance
from checks.inputs import check_inputs
from checks.issues import ERROR, Issue
from checks.ranges import check_ranges, check_sweep
from heatbalance_mcp.patches import PatchError, apply_patches
from plants.sub300 import model as sub300

_PLANT_DIR = Path(sub300.__file__).parent

REFERENCE_PLANTS = {
    "sub300": {
        "module": sub300,
        "file": _PLANT_DIR / "design_data.yaml",
        "description": (
            "Typical 300 MW subcritical single-reheat unit, 16.7 MPa / 538 / "
            "538 degC, 8 extractions (3 HP heaters, deaerator, 4 LP heaters), "
            "turbine-driven feed pump, wet cooling 4.9 kPa. Typical values, "
            "not a real plant; design point calibrated against an "
            "independent hand calculation."
        ),
    },
    "en16093727": {
        "module": sub300,
        "file": _PLANT_DIR / "validation_en16093727.yaml",
        "description": (
            "Published 300 MW subcritical unit with 7 heaters (Energies 2023, "
            "16(9), 3727), motor-driven feed pump, back pressure 14.55 kPa "
            "(inferred). Validated against published design data at 100 % "
            "and 75 % THA."
        ),
    },
}

# results returned by default (more via get_results)
PERFORMANCE_KEYS = (
    "gross_power", "heat_rate", "efficiency", "main_steam_flow",
    "reheat_steam_flow", "feedwater_T", "exhaust_x", "load",
)
SUMMARY_FIELDS = (
    "operation", "performance", "turbine", "extractions", "leakages",
    "heaters", "condenser", "pumps", "states", "power",
)


class ServiceError(ValueError):
    """Invalid request: unknown model or case, malformed patch."""


def _round(obj, digits: int = 6):
    """Round floats to significant digits to keep responses short."""
    if isinstance(obj, float):
        if obj == 0 or not math.isfinite(obj):
            return obj
        return round(obj, max(0, digits - 1 - int(math.floor(math.log10(abs(obj))))))
    if isinstance(obj, dict):
        return {k: _round(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, digits) for v in obj]
    return obj


def _issues(issues: list[Issue]) -> dict:
    return {
        "errors": [i.to_dict() for i in issues if i.severity == ERROR],
        "warnings": [i.to_dict() for i in issues if i.severity != ERROR],
    }


class Workspace:
    """In-memory store of reference plants, derived models and their cases."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        for pid, ref in REFERENCE_PLANTS.items():
            with open(ref["file"], encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            self.models[pid] = {
                "id": pid, "base": None, "reference": pid, "document": doc,
                "changes": [], "module": ref["module"], "cases": {},
                "design": None,
            }

    # ---- lookup -------------------------------------------------------------------------
    def _model(self, model_id: str) -> dict:
        try:
            return self.models[model_id]
        except KeyError:
            raise ServiceError(
                f"unknown model '{model_id}'; known: {sorted(self.models)}"
            ) from None

    def _params(self, m: dict) -> dict:
        return m["module"]._strip(m["document"]["parameters"])

    # ---- reference data -------------------------------------------------------------
    def list_reference_plants(self) -> list[dict]:
        out = []
        for pid, ref in REFERENCE_PLANTS.items():
            doc = self.models[pid]["document"]
            params = self._params(self.models[pid])
            out.append({
                "id": pid,
                "title": doc["meta"]["title"],
                "description": ref["description"],
                "heaters": [h["label"] for h in params["heaters"]],
                "turbine_sections": [s["label"] for s in params["turbine"]["sections"]],
                "feed_pump_drive": params["feed_pump"].get("drive", "turbine"),
                "has_pressure_curve": "pressure_curve" in params.get("operation", {}),
            })
        return out

    def list_models(self) -> list[dict]:
        return [
            {"id": m["id"], "base": m["base"], "reference": m["reference"],
             "changes": len(m["changes"]), "solved_design": m["design"] is not None,
             "cases": sorted(m["cases"])}
            for m in self.models.values()
        ]

    def get_plant_data(self, model_id: str, part: str = "parameters") -> dict:
        """Parameters (with unit and ref), references or assumptions."""
        m = self._model(model_id)
        doc = m["document"]
        parts = ("parameters", "references", "assumptions", "changes", "all")
        if part not in parts:
            raise ServiceError(f"part must be one of {parts}")
        if part == "changes":
            return {"model": model_id, "base": m["base"], "changes": m["changes"]}
        if part == "all":
            return {"model": model_id, **{
                k: doc.get(k) for k in ("meta", "parameters", "references", "assumptions")
            }, "changes": m["changes"]}
        return {"model": model_id, part: doc.get(part)}

    # ---- deriving -----------------------------------------------------------------------
    def derive_model(self, base: str, patches: list[dict], name: str | None = None) -> dict:
        """Create a new model from ``base`` with patches; see patches.py.

        The model is created only if the patched parameters have no input
        errors; otherwise the errors are returned and nothing is stored.
        """
        b = self._model(base)
        if not patches:
            raise ServiceError("give at least one patch")
        try:
            doc, log = apply_patches(
                b["document"], patches, first_id=len(b["changes"]) + 1
            )
        except PatchError as exc:
            raise ServiceError(str(exc)) from exc
        params = b["module"]._strip(doc["parameters"])
        issues = list(check_inputs(params))
        try:
            b["module"]._validate(params)
        except b["module"].ModelError as exc:
            issues.append(Issue(
                check="input_topology", problem=str(exc), location="parameters",
                value=None, expected=(None, None), unit="-", severity=ERROR,
                hint="Check labels and references between turbine sections, "
                "heaters and leakages.",
            ))
        result = {"changes": log, "input_issues": _issues(issues)}
        if any(i.severity == ERROR for i in issues):
            return _round({"status": "rejected", "model": None, **result})

        model_id = name or f"{b['reference']}-d{sum(1 for x in self.models.values() if x['reference'] == b['reference'])}"
        if model_id in self.models:
            raise ServiceError(f"model '{model_id}' already exists")
        doc["meta"] = dict(doc["meta"], name=model_id, derived_from=base)
        self.models[model_id] = {
            "id": model_id, "base": base, "reference": b["reference"],
            "document": doc, "changes": b["changes"] + log,
            "module": b["module"], "cases": {}, "design": None,
        }
        return _round({"status": "created", "model": model_id, "base": base, **result})

    def export_model(self, model_id: str) -> dict:
        """YAML text of the model's design data, patches as assumptions."""
        m = self._model(model_id)
        text = yaml.safe_dump(m["document"], sort_keys=False, allow_unicode=True)
        return {"model": model_id, "changes": len(m["changes"]), "yaml": text}

    # ---- solving ------------------------------------------------------------------------
    def _compact(self, summary: dict) -> dict:
        perf = {k: summary["performance"][k] for k in PERFORMANCE_KEYS}
        op = {k: v for k, v in summary["operation"].items() if k != "conditions"}
        return {
            "performance": perf,
            "operation": op,
            "extractions": [
                {k: e[k] for k in ("no", "heater", "p", "T", "m")}
                for e in summary["extractions"]
            ],
        }

    def _failed(self, model_id: str, case: str, exc: Exception, hints: list[str]) -> dict:
        issue = Issue(
            check="convergence", problem=str(exc).splitlines()[0][:400],
            location=case, value=None, expected=(None, None), unit="-",
            severity=ERROR, hint=" ".join(hints),
        )
        return {"status": "failed", "model": model_id, "case": case,
                "issues": _issues([issue])}

    def solve_design(self, model_id: str) -> dict:
        m = self._model(model_id)
        mod = m["module"]
        params = self._params(m)
        pre = check_inputs(params)
        if any(i.severity == ERROR for i in pre):
            return _round({"status": "rejected", "model": model_id,
                           "case": "design", "issues": _issues(pre)})
        try:
            nw = mod.solve(mod.build_model(params))
        except Exception as exc:  # solver errors of any kind
            return self._failed(model_id, "design", exc, [
                "Undo the most recent patches one by one to find the one "
                "that breaks the solution; check extraction enthalpies "
                "against pressures.",
            ])
        summary = mod.summarize(nw)
        balance = check_balance(nw)
        issues = pre + balance["issues"] + check_ranges(summary)
        m["design"] = {"state": mod.design_state(nw), "summary": summary}
        m["cases"] = {"design": summary}
        return _round({
            "status": "solved", "model": model_id, "case": "design",
            **self._compact(summary),
            "balance": {
                "max_energy_residual_rel": balance["max_energy_residual_rel"],
                "system_energy_residual_rel": balance["system"]["energy_residual_rel"],
            },
            "issues": _issues(issues),
        })

    def _ensure_design(self, model_id: str) -> dict | None:
        m = self._model(model_id)
        if m["design"] is None:
            res = self.solve_design(model_id)
            if res["status"] != "solved":
                return res
        return None

    def solve_offdesign(self, model_id: str, conditions: dict | None = None) -> dict:
        failed = self._ensure_design(model_id)
        if failed:
            return failed
        m = self._model(model_id)
        mod = m["module"]
        params = self._params(m)
        case = f"od{sum(1 for c in m['cases'] if c.startswith('od')) + 1}"
        try:
            nw = mod.solve_offdesign(params, m["design"]["state"], conditions)
        except mod.ModelError as exc:
            if "did not converge" not in str(exc):
                raise ServiceError(str(exc)) from exc
            return self._failed(model_id, case, exc, [
                "Try pressure 'sliding', a load closer to 1, or use "
                "load_sweep, which approaches low loads in steps.",
            ])
        summary = mod.summarize(nw)
        issues = check_balance(nw)["issues"] + check_ranges(summary)
        m["cases"][case] = summary
        return _round({
            "status": "solved", "model": model_id, "case": case,
            "conditions": summary["operation"]["conditions"],
            **self._compact(summary), "issues": _issues(issues),
        })

    def load_sweep(self, model_id: str, loads: list[float] | None = None,
                   conditions: dict | None = None) -> dict:
        failed = self._ensure_design(model_id)
        if failed:
            return failed
        m = self._model(model_id)
        mod = m["module"]
        loads = list(loads or (1.0, 0.9, 0.75, 0.5, 0.4))
        if conditions and set(conditions) & {"load", "gross_power", "main_steam_flow"}:
            raise ServiceError("conditions of a sweep must not contain the load")
        if not all(isinstance(x, (int, float)) and 0.2 <= x <= 1.1 for x in loads):
            raise ServiceError("loads must be numbers between 0.2 and 1.1")
        try:
            summaries = mod.load_sweep(self._params(m), loads, conditions)
        except mod.ModelError as exc:
            if "did not converge" not in str(exc):
                raise ServiceError(str(exc)) from exc
            return self._failed(model_id, "sweep", exc, [
                "Raise the lowest load or use pressure 'sliding'.",
            ])
        n = sum(1 for c in m["cases"] if c.startswith("sweep")) + 1
        points = []
        for k, s in enumerate(summaries):
            case = f"sweep{n}.{k + 1}"
            m["cases"][case] = s
            points.append({
                "case": case, **{key: s["performance"][key] for key in (
                    "load", "gross_power", "heat_rate", "main_steam_flow",
                    "feedwater_T")},
                "p_main": s["operation"]["p_main"],
                "throttle_pr": s["operation"]["throttle_pr"],
                "issues": _issues(check_ranges(s)),
            })
        return _round({
            "status": "solved", "model": model_id, "points": points,
            "sweep_issues": _issues(check_sweep(summaries)),
        })

    # ---- results ------------------------------------------------------------------------
    def get_results(self, model_id: str, case: str = "design",
                    fields: list[str] | None = None) -> dict:
        m = self._model(model_id)
        if case not in m["cases"]:
            raise ServiceError(
                f"unknown case '{case}' of model '{model_id}'; known: "
                f"{sorted(m['cases'])} (solve first)"
            )
        fields = list(fields or ("performance", "operation"))
        bad = set(fields) - set(SUMMARY_FIELDS)
        if bad:
            raise ServiceError(f"unknown fields {sorted(bad)}; use {SUMMARY_FIELDS}")
        summary = m["cases"][case]
        return _round({"model": model_id, "case": case,
                       **{f: copy.deepcopy(summary[f]) for f in fields}})

    def compare(self, model_a: str, model_b: str, case_a: str = "design",
                case_b: str = "design") -> dict:
        """Key results of two cases side by side (b - a)."""
        sa = self.get_results(model_a, case_a, ["performance", "extractions"])
        sb = self.get_results(model_b, case_b, ["performance", "extractions"])
        rows = []
        for key in PERFORMANCE_KEYS:
            a, b = sa["performance"][key], sb["performance"][key]
            row = {"quantity": key, "a": a, "b": b}
            if isinstance(a, float) and isinstance(b, float):
                row["delta"] = b - a
                row["delta_rel"] = (b - a) / a if a else None
            rows.append(row)
        ext_a = {e["heater"]: e for e in sa["extractions"]}
        for e in sb["extractions"]:
            if e["heater"] in ext_a:
                a, b = ext_a[e["heater"]]["m"], e["m"]
                rows.append({"quantity": f"extraction {e['heater']} m", "a": a,
                             "b": b, "delta": b - a,
                             "delta_rel": (b - a) / a if a else None})
        return _round({"a": f"{model_a}/{case_a}", "b": f"{model_b}/{case_b}",
                       "rows": rows})
