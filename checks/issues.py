"""Structured issue records shared by all checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """One finding of a check, written for a human or an agent to act on.

    Attributes
    ----------
    check : str
        Machine-readable name of the check, e.g. ``heater_ttd_u``.
    problem : str
        One-sentence description of what is wrong.
    location : str
        Component, connection or result the finding refers to.
    value : float | None
        Actual value.
    expected : tuple[float | None, float | None]
        Expected (low, high) range; None means unbounded on that side.
    unit : str
        Unit of ``value`` and ``expected``.
    severity : str
        ``"error"`` (physically wrong or far outside practice) or
        ``"warning"`` (unusual, worth a look).
    hint : str
        Suggested direction for troubleshooting.
    """

    check: str
    problem: str
    location: str
    value: float | None
    expected: tuple
    unit: str
    severity: str
    hint: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expected"] = list(self.expected)
        return d

    def __str__(self) -> str:
        lo, hi = self.expected
        rng = f"[{'-inf' if lo is None else lo}, {'inf' if hi is None else hi}]"
        val = "n/a" if self.value is None else f"{self.value:.6g}"
        return (
            f"{self.severity.upper()} {self.check} @ {self.location}: "
            f"{self.problem} (value {val} {self.unit}, expected {rng}). "
            f"Hint: {self.hint}"
        )


def errors(issues) -> list:
    """Return only the issues with severity ``error``."""
    return [i for i in issues if i.severity == ERROR]


def warnings(issues) -> list:
    """Return only the issues with severity ``warning``."""
    return [i for i in issues if i.severity == WARNING]
