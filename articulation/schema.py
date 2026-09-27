"""
The articulation dataset schema: one JSON file per agreement in data/articulation/ (see its README.md).

    {
      "schema_version": "1.0.0",
      "dataset_type": "transfer_articulation",
      "dataset_id": "...",
      "source": {"publisher": "ASSIST", "title": "...", "url": "...", "retrieved_on": "YYYY-MM-DD"},
      "agreement": {"from_institution_id": "chabot", "to_institution_id": "csueb", "major": "Computer Science",
                    "degree": "B.S.", "academic_year": "2026-2027"},
      "institutions": [{"id": "chabot", "name": "Chabot College"}, ...],
      "courses": [{"code": "...", "title": "...", "units": 4, "prereqs": [...], "coreqs": [...]}, ...],
      "requirements": [{"id": "...", "receiving": [{"code": "...", "title": "..."}],
                        "articulation": {"type": "course" | "all_of" | "one_of" | "none" | "other", ...}}, ...],
      "requirement_groups": [{"id": "...", "title": "...", "choose": 1, "requirement_ids": [...]}]
    }

`courses` are the sending college's courses, in the planner's Course terms. A requirement is one course
(or set of courses) at the receiving university and what the sending college articulates to it. Validation
only checks that the file is well formed; whether the planner can use it is decided in loader.py.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_MAJOR = "1"
DATASET_TYPE = "transfer_articulation"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Strict):
    publisher: str = Field(min_length=1, examples=["ASSIST"])
    title: str | None = None
    url: str | None = Field(default=None, examples=["https://assist.org/transfer/results?..."])
    retrieved_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: list[str] = Field(default_factory=list)


class Agreement(_Strict):
    from_institution_id: str = Field(min_length=1, examples=["chabot"])
    to_institution_id: str = Field(min_length=1, examples=["csueb"])
    major: str = Field(min_length=1, examples=["Computer Science"])
    degree: str | None = Field(default=None, examples=["B.S."])
    academic_year: str = Field(pattern=r"^\d{4}-\d{4}$", examples=["2026-2027"])

    @field_validator("academic_year")
    @classmethod
    def _consecutive(cls, v: str) -> str:
        start, end = (int(x) for x in v.split("-"))
        if end != start + 1:
            raise ValueError(f"academic_year must be two consecutive years, got {v!r}")
        return v


class Institution(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, description="Official name.")
    aliases: list[str] = Field(default_factory=list, description="Other names it goes by (rule-derived ones "
                                                                 "such as 'Cal State East Bay' need not be listed).")


class Course(_Strict):
    """A sending-college course. `units` is required: the planner never guesses units."""

    code: str = Field(min_length=1, examples=["MATH 1"])
    title: str = Field(min_length=1)
    units: float | None = Field(default=None, gt=0, description="Semester units. Null = not stated in the source.")
    prereqs: list[str] = Field(default_factory=list, description="ALL must be completed in an earlier term.")
    coreqs: list[str] = Field(default_factory=list, description="Must be taken in the same term or earlier.")


class ReceivingCourse(_Strict):
    code: str = Field(min_length=1, examples=["CS 101"])
    title: str | None = None
    units: float | None = Field(default=None, gt=0)


class CourseArticulation(_Strict):
    type: Literal["course"]
    course: str = Field(min_length=1)


class AllOfArticulation(_Strict):
    type: Literal["all_of"]
    courses: list[str] = Field(min_length=2)


class OneOfArticulation(_Strict):
    """Any one option satisfies the requirement; an option is one course or a bundle taken together."""

    type: Literal["one_of"]
    options: list[list[str]] = Field(min_length=2)

    @field_validator("options")
    @classmethod
    def _non_empty(cls, v: list[list[str]]) -> list[list[str]]:
        if any(not option for option in v):
            raise ValueError("every option needs at least one course")
        return v


class NoArticulation(_Strict):
    type: Literal["none"]
    reason: str | None = Field(default=None, examples=["No course articulated"])


class OtherArticulation(_Strict):
    """A structure the schema doesn't model, recorded as printed; the pathway is then unsupported."""

    type: Literal["other"]
    description: str = Field(min_length=1)


Articulation = Annotated[CourseArticulation | AllOfArticulation | OneOfArticulation | NoArticulation
                         | OtherArticulation, Field(discriminator="type")]


class Requirement(_Strict):
    id: str = Field(min_length=1)
    receiving: list[ReceivingCourse] = Field(min_length=1)
    articulation: Articulation
    notes: list[str] = Field(default_factory=list)


class RequirementGroup(_Strict):
    """Complete `choose` of the listed requirements (requirements in no group are all required)."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    choose: int = Field(ge=1)
    requirement_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _enough(self) -> RequirementGroup:
        if self.choose > len(self.requirement_ids):
            raise ValueError(f"group {self.id!r} chooses {self.choose} of {len(self.requirement_ids)} requirements")
        return self


class ArticulationDataset(_Strict):
    schema_version: str
    dataset_type: Literal["transfer_articulation"]
    dataset_id: str = Field(min_length=1)
    source: Source
    agreement: Agreement
    institutions: list[Institution] = Field(min_length=2)
    courses: list[Course]
    requirements: list[Requirement] = Field(min_length=1)
    requirement_groups: list[RequirementGroup] = Field(default_factory=list)
    prerequisite_source: Source | None = Field(
        default=None, description="Where the courses' prereqs/coreqs come from (e.g. the college catalog).")
    notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _supported(cls, v: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", v) or v.split(".")[0] != SCHEMA_MAJOR:
            raise ValueError(f"schema_version {v!r} isn't supported (expected {SCHEMA_MAJOR}.x.y)")
        return v

    @model_validator(mode="after")
    def _references(self) -> ArticulationDataset:
        """Every id the file refers to is defined in it, and nothing is defined twice."""
        def unique(values: list[str], what: str) -> None:
            dupes = sorted({v for v in values if values.count(v) > 1})
            if dupes:
                raise ValueError(f"duplicate {what}: {dupes}")

        unique([i.id for i in self.institutions], "institution ids")
        unique([c.code for c in self.courses], "course codes")
        unique([r.id for r in self.requirements], "requirement ids")
        unique([g.id for g in self.requirement_groups], "requirement group ids")
        ids = {i.id for i in self.institutions}
        for end in ("from_institution_id", "to_institution_id"):
            if getattr(self.agreement, end) not in ids:
                raise ValueError(f"agreement.{end} {getattr(self.agreement, end)!r} isn't in institutions")
        requirement_ids = {r.id for r in self.requirements}
        grouped = [rid for g in self.requirement_groups for rid in g.requirement_ids]
        unique(grouped, "requirements in more than one group")
        missing = sorted(set(grouped) - requirement_ids)
        if missing:
            raise ValueError(f"requirement_groups name unknown requirements: {missing}")
        return self
