"""Exception types raised by the SEP engine. All derive from SEPError."""

from __future__ import annotations


class SEPError(Exception):
    """Base class for every error the engine raises on bad or infeasible input."""


class UnknownCourseError(SEPError):
    def __init__(self, code: str, context: str = "") -> None:
        self.code = code
        where = f" (referenced by {context})" if context else ""
        super().__init__(f"Course '{code}' is not in the catalog{where}.")


class CycleError(SEPError):
    """Prerequisites form a loop, so no valid course order exists."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(
            "Prerequisite cycle detected: " + " -> ".join(cycle)
            + " (each course lists the next one as a prerequisite, so none of them can ever be taken)."
        )


class InfeasibleRequirementError(SEPError):
    """A requirement group cannot be satisfied with the available options."""


class SchedulingError(SEPError):
    """No semester assignment satisfies the scheduling constraints."""
