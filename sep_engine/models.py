"""
Data contracts for the SEP engine (Pydantic v2).

These models are the injection point for real data: have your database layer
produce JSON matching `SEPRequest` (or build the models directly) and pass it
to `generate_sep()`. Nothing in the algorithm modules depends on where the
data comes from.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Term(str, Enum):
    FALL = "Fall"
    SPRING = "Spring"
    SUMMER = "Summer"


_DAY_CODES = "MTWRFSU"   # R = Thursday, S = Saturday, U = Sunday


def _to_minutes(hhmm: str) -> int:
    hours, _, minutes = hhmm.partition(":")
    if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2):
        raise ValueError(f"time must look like 'HH:MM', got {hhmm!r}")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time out of range: {hhmm!r}")
    return h * 60 + m


class MeetingTime(BaseModel):
    """One weekly meeting block, e.g. MW 09:30–10:45."""

    model_config = ConfigDict(frozen=True)

    days: str = Field(description='Day letters, e.g. "MW" or "TR" (R = Thursday).')
    start: str = Field(description='"HH:MM", 24-hour clock.')
    end: str

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: str) -> str:
        v = v.upper()
        if not v or any(d not in _DAY_CODES for d in v):
            raise ValueError(f"days must use letters from {_DAY_CODES!r}, got {v!r}")
        return v

    @field_validator("start", "end")
    @classmethod
    def _check_time(cls, v: str) -> str:
        _to_minutes(v)
        return v

    @model_validator(mode="after")
    def _check_order(self) -> MeetingTime:
        if self.start_min >= self.end_min:
            raise ValueError(f"meeting must end after it starts ({self.start}–{self.end})")
        return self

    @property
    def start_min(self) -> int:
        return _to_minutes(self.start)

    @property
    def end_min(self) -> int:
        return _to_minutes(self.end)

    def overlaps(self, other: MeetingTime) -> bool:
        return bool(set(self.days) & set(other.days)) and \
            self.start_min < other.end_min and other.start_min < self.end_min


class Course(BaseModel):
    """A community college course."""

    code: str
    title: str
    units: float = Field(gt=0)
    prereqs: list[str] = Field(default_factory=list,
                               description="ALL must be completed in an earlier term.")
    coreqs: list[str] = Field(default_factory=list,
                              description="Must be taken in the same term or earlier.")
    terms_offered: frozenset[Term] = frozenset({Term.FALL, Term.SPRING, Term.SUMMER})
    difficulty: float = Field(default=3.0, ge=1.0, le=5.0,
                              description="Historical difficulty score, 1 (easy) to 5 (hard).")
    meetings: list[MeetingTime] = Field(default_factory=list,
                                        description="Empty = online/flexible, never conflicts.")

    @model_validator(mode="after")
    def _no_self_reference(self) -> Course:
        if self.code in self.prereqs or self.code in self.coreqs:
            raise ValueError(f"{self.code} cannot be its own prerequisite or co-requisite")
        return self

    def conflicts_with(self, other: Course) -> bool:
        return any(a.overlaps(b) for a in self.meetings for b in other.meetings)


class GroupCategory(str, Enum):
    MAJOR = "major"
    GE = "ge"


class RequirementGroup(BaseModel):
    """
    A multiple-choice requirement: take `choose` distinct courses from `options`.
    Within one category a course counts toward at most one group (e.g. one
    course can't fill two GE areas), but a major-prep course may also count
    toward a GE group.
    """

    id: str
    name: str
    category: GroupCategory
    choose: int = Field(default=1, ge=1)
    options: list[str] = Field(min_length=1)

    @field_validator("options")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _enough_options(self) -> RequirementGroup:
        if self.choose > len(self.options):
            raise ValueError(f"group {self.id!r} asks for {self.choose} courses but lists only "
                             f"{len(self.options)} options")
        return self


class TransferAgreement(BaseModel):
    """Articulation data for one Community College + University + Major combination."""

    college: str
    university: str
    major: str = Field(description="Major as students search for it, e.g. 'Computer Science'.")
    degree: str | None = Field(default=None, description="Official degree title, e.g. 'Computer Science, B.A.'.")
    ge_pattern: str = "CAL-GETC"
    required_courses: list[str] = Field(default_factory=list, description="Mandatory major prep.")
    requirement_groups: list[RequirementGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_group_ids(self) -> TransferAgreement:
        ids = [g.id for g in self.requirement_groups]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement group ids must be unique")
        return self


class StudentProfile(BaseModel):
    completed_courses: list[str] = Field(default_factory=list)
    start_term: Term = Term.FALL
    start_year: int = 2026
    include_summer: bool = False
    max_terms: int = Field(default=12, ge=1, le=30, description="Give up beyond this many terms.")


class PlanConfig(BaseModel):
    """How one plan (e.g. 'Plan A: Fastest Route') is optimized."""

    id: str
    name: str
    label: str
    description: str = ""
    weight: Literal["units", "difficulty"] = Field(
        description="Dijkstra edge weight: 'units' (shortest) or 'difficulty' (easiest).")
    max_units: float = Field(default=18, gt=0, description="Unit cap per Fall/Spring term.")
    max_summer_units: float = Field(default=9, gt=0,
                                    description="Unit cap per Summer term (half-length); min'd with max_units.")
    max_term_difficulty: float | None = Field(
        default=None, gt=0, description="Optional cap on the summed difficulty scores in one term.")
    kind: Literal["recommended", "fast", "balanced"] = Field(
        default="recommended", description="recommended = the single plan for a normal workload; "
                                            "fast / balanced = the 2-year and 3-year tracks for a heavy one.")
    min_regular_terms: int = Field(default=0, ge=0, description="Plan at least this many Fall/Spring terms.")
    max_avg_load: float | None = Field(
        default=None, gt=0, description="Pick the shortest horizon whose average load is at most this many units.")
    balance: bool = Field(default=True, description="Spread units evenly across terms instead of packing early.")
    balance_margin: float = Field(default=2.0, ge=0, description="Soft cap = target units per term + this margin.")


class SEPRequest(BaseModel):
    """Everything generate_sep() needs, in one JSON-serializable envelope."""

    catalog: list[Course]
    agreement: TransferAgreement
    profile: StudentProfile = Field(default_factory=StudentProfile)
    plans: list[PlanConfig] | None = None


def index_catalog(courses: list[Course]) -> dict[str, Course]:
    catalog: dict[str, Course] = {}
    for c in courses:
        if c.code in catalog:
            raise ValueError(f"duplicate course code in catalog: {c.code}")
        catalog[c.code] = c
    return catalog
