"""Patch language for deriving models from a reference plant.

A patch changes one value in the ``parameters`` block of a design data
document (the YAML structure with ``{value, unit, ref}`` leaves)::

    {"path": "condenser.p", "value": 0.0065, "reason": "..."}
    {"path": "heaters[H1].ttd_u", "value": -1.0, "reason": "..."}
    {"path": "turbine.sections[LP4].h_out", "op": "delete", "reason": "..."}
    {"path": "turbine.sections[LP4].eta_s", "value": 0.88, "reason": "..."}
    {"path": "operation.pressure_curve[0].p", "value": 9.0, "reason": "..."}

Path syntax: dot-separated keys; ``[X]`` selects a list item by its
``label`` (or by index for unlabelled lists). ``op`` is ``set`` (default)
or ``delete``. Every patch needs a ``reason``; it is recorded as an
assumption of the derived model (``P1``, ``P2``, ...) and the patched value
refers to it, so the provenance rule of the data files still holds.

New keys may only be created where the schema allows them (e.g. ``eta_s``
on a turbine section); everything else must already exist.
"""

from __future__ import annotations

import copy
import re
from numbers import Number

# keys that may be added to an item of these lists
_ADDABLE = {
    "sections": {"h_out", "eta_s"},
    "heaters": {"ttd_u", "ttd_l", "dp_water", "line_pr"},
    "leakages": {"fraction", "m"},
}
# keys that may be deleted (alternatives of each other)
_DELETABLE = {
    "sections": {"h_out", "eta_s"},
    "leakages": {"fraction", "m"},
}
# leaves stored as plain strings, not {value, unit, ref}
_STRING_KEYS = {
    "label", "cylinder", "type", "extraction", "source", "destination",
    "drive", "reheat_after", "pressure_mode",
}

_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[([^\]]+)\])?$")


class PatchError(ValueError):
    """Raised for a patch that cannot be applied."""


def _parse(path: str) -> list[tuple[str, str | None]]:
    if not isinstance(path, str) or not path:
        raise PatchError("path must be a non-empty string")
    tokens = []
    for part in path.split("."):
        m = _TOKEN.match(part)
        if not m:
            raise PatchError(f"invalid path element '{part}' in '{path}'")
        tokens.append((m.group(1), m.group(2)))
    return tokens


def _select(items: list, selector: str, path: str):
    for item in items:
        if isinstance(item, dict) and item.get("label") == selector:
            return item
    if selector.isdigit() and int(selector) < len(items):
        return items[int(selector)]
    labels = [i.get("label") for i in items if isinstance(i, dict) and "label" in i]
    raise PatchError(
        f"no item '{selector}' in '{path}'"
        + (f"; available labels: {labels}" if labels else f"; {len(items)} items")
    )


def _resolve(params: dict, path: str):
    """Return (container, key, list_name) of the patched leaf."""
    tokens = _parse(path)
    node = params
    list_name = None
    for k, (key, selector) in enumerate(tokens):
        last = k == len(tokens) - 1
        if not isinstance(node, dict):
            raise PatchError(f"'{path}': cannot descend into a value")
        if last and selector is None:
            return node, key, list_name
        if key not in node:
            raise PatchError(
                f"'{path}': unknown key '{key}'; available: {sorted(node)}"
            )
        node = node[key]
        if selector is not None:
            if not isinstance(node, list):
                raise PatchError(f"'{path}': '{key}' is not a list")
            list_name = key
            node = _select(node, selector, path)
            if last:
                raise PatchError(f"'{path}': path must end at a value")
    raise AssertionError("unreachable")


def apply_patches(document: dict, patches: list[dict], first_id: int = 1) -> tuple[dict, list[dict]]:
    """Apply patches to a copy of a design data document.

    Returns the patched document and a change log (path, old, new, reason,
    id). The document gets one assumption ``P<n>`` per patch.
    """
    doc = copy.deepcopy(document)
    params = doc["parameters"]
    assumptions = doc.setdefault("assumptions", {})
    log = []
    for n, patch in enumerate(patches, start=first_id):
        if not isinstance(patch, dict):
            raise PatchError("each patch must be an object")
        unknown = set(patch) - {"path", "value", "reason", "op"}
        if unknown:
            raise PatchError(f"unknown patch fields {sorted(unknown)}")
        path = patch.get("path")
        reason = (patch.get("reason") or "").strip()
        if not reason:
            raise PatchError(f"'{path}': a reason is required for every patch")
        op = patch.get("op", "set")
        if op not in ("set", "delete"):
            raise PatchError(f"'{path}': op must be 'set' or 'delete'")
        container, key, list_name = _resolve(params, path)
        pid = f"P{n}"
        exists = key in container
        old = container.get(key)
        old_value = old.get("value") if isinstance(old, dict) else old

        if op == "delete":
            if not exists:
                raise PatchError(f"'{path}': nothing to delete")
            if key not in _DELETABLE.get(list_name, set()):
                raise PatchError(f"'{path}': this key cannot be deleted")
            del container[key]
            new_value = None
        else:
            if "value" not in patch:
                raise PatchError(f"'{path}': 'value' is required for op 'set'")
            value = patch["value"]
            if not exists and key not in _ADDABLE.get(list_name, set()):
                raise PatchError(f"'{path}': unknown key '{key}'")
            if key in _STRING_KEYS:
                if not isinstance(value, str):
                    raise PatchError(f"'{path}': expected a string")
                container[key] = value
            else:
                if not isinstance(value, Number) or isinstance(value, bool):
                    if not (key == "p_out" and value == "condenser"):
                        raise PatchError(f"'{path}': expected a number")
                if isinstance(old, dict):
                    container[key] = dict(old, value=value, ref=pid)
                else:
                    container[key] = value
            new_value = value

        # rows (list items) carry one ref for all their fields
        if list_name is not None or not isinstance(container.get(key), dict):
            refs = container.get("ref")
            if refs is not None or list_name is not None:
                refs = [] if refs is None else (refs if isinstance(refs, list) else [refs])
                container["ref"] = [*refs, pid]
        assumptions[pid] = {
            "item": path,
            "choice": f"{'delete' if op == 'delete' else 'set'}: "
                      f"{old_value!r} -> {new_value!r}",
            "rationale": f"{reason} (agent patch)",
        }
        log.append({
            "id": pid, "path": path, "op": op, "old": old_value,
            "new": new_value, "reason": reason,
        })
    return doc, log
