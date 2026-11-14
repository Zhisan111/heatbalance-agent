"""Every number in design_data.yaml must carry a source or an assumption."""

from numbers import Number

import pytest

from plants.sub300 import model


FILES = {
    "design_data.yaml": ["parameters", "calibration", "plausibility"],
    "validation_en16093727.yaml": ["parameters", "published", "tolerances"],
}
BLOCKS = [(name, block) for name, blocks in FILES.items() for block in blocks]


def _load(name):
    return model.load_design_data(model.DATA_FILE.with_name(name))


@pytest.fixture(scope="module")
def data():
    return model.load_design_data()


def _refs(node):
    ref = node.get("ref")
    if ref is None:
        return []
    return ref if isinstance(ref, list) else [ref]


def _walk(node, path, inherited, out):
    """Collect (path, refs) for every numeric value below ``node``."""
    if isinstance(node, dict):
        refs = _refs(node) or inherited
        for key, value in node.items():
            if key in ("ref", "unit"):
                continue
            _walk(value, f"{path}.{key}", refs, out)
    elif isinstance(node, list):
        for k, value in enumerate(node):
            _walk(value, f"{path}[{k}]", inherited, out)
    elif isinstance(node, Number) and not isinstance(node, bool):
        out.append((path, inherited))


@pytest.mark.parametrize("name, block", BLOCKS)
def test_every_number_has_a_known_reference(name, block):
    data = _load(name)
    known = set(data["references"]) | set(data["assumptions"])
    found = []
    _walk(data[block], block, [], found)
    assert found, f"no numbers found in {block}"
    for path, refs in found:
        assert refs, f"{path} has no ref"
        unknown = set(refs) - known
        assert not unknown, f"{path} refers to unknown {unknown}"


@pytest.mark.parametrize("name", FILES)
def test_references_are_documented(name):
    data = _load(name)
    for key, ref in data["references"].items():
        assert key.startswith("S")
        for field in ("title", "url", "locator", "quality"):
            assert ref.get(field), f"{key} lacks {field}"
    for key, a in data["assumptions"].items():
        assert key[0] in "AV"
        for field in ("item", "choice", "rationale"):
            assert a.get(field), f"{key} lacks {field}"


def test_load_params_returns_plain_values(data):
    params = model.load_params()
    assert params["main_steam"]["p"] == data["parameters"]["main_steam"]["p"]["value"]
    assert "ref" not in params["turbine"]["sections"][0]
    assert len(params["heaters"]) == 8
