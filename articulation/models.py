"""What the pathway store holds: planner-ready pathways, and the files it couldn't use (and why)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sep_engine.models import Course, TransferAgreement


@dataclass
class Pathway:
    """One College + University + Major (+ degree, academic year) the planner can build plans for."""

    college: str                      # display names (official ones for loaded data)
    university: str
    college_ref: str                  # canonical id, or a name the institution registry resolves (mock data)
    university_ref: str
    major: str                        # "Computer Science"
    degree: str | None                # "B.S."
    academic_year: str | None         # "2026-2027"; None = not year-specific (mock data)
    ge_pattern: str | None            # None: the data has no GE requirements, so it fits every GE choice
    agreement: TransferAgreement
    catalog: dict[str, Course]
    source: Literal["assist", "mock"] = "mock"
    dataset_id: str | None = None
    file: str | None = None           # data file it came from
    publisher: str | None = None
    source_url: str | None = None
    retrieved_on: str | None = None
    not_articulated: list[str] = field(default_factory=list)   # receiving requirements with no course to take
    prerequisites_enforced: bool = True
    notes: list[str] = field(default_factory=list)             # warnings returned with every plan
    assist: Any = None                                         # AssistAgreement (requirement tree) for ASSIST files

    def agreement_for(self, ge_pattern: str) -> TransferAgreement:
        """The agreement for one request: data without GE requirements takes the student's GE choice."""
        return self.agreement if self.ge_pattern else self.agreement.model_copy(update={"ge_pattern": ge_pattern})


@dataclass(frozen=True)
class UnsupportedPathway:
    """A well-formed agreement the planner can't plan (yet), and why."""

    college_ref: str
    university_ref: str
    college: str
    university: str
    major: str
    degree: str | None
    academic_year: str
    dataset_id: str
    file: str
    reason: str

    @property
    def message(self) -> str:
        degree = f", {self.degree}" if self.degree else ""
        return (f"Articulation data for {self.college} -> {self.university}, {self.major}{degree} ({self.academic_year}) "
                f"is loaded from {self.file}, but the planner can't use it: {self.reason}.")


@dataclass(frozen=True)
class SkippedFile:
    """A file in the articulation folder that couldn't be read as an articulation dataset."""

    file: str
    reason: str
