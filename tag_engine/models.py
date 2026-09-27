"""
Types for the TAG (Transfer Admission Guarantee) engine.

The engine is a pipeline, and each stage has its own types:

    data/uc_tag_requirements_*.json    the source JSON: loaded as-is, deep-frozen, never modified
            |  data.normalize_tag_dataset()
            v
    TagDataset, TagCampus, ...         frozen dataclasses: terms parsed, the rules the engine reads typed,
            |                          source pages and URLs kept; the frozen JSON stays on `.raw`
            |  evaluator.evaluate_tag_eligibility(dataset, campus, entry_term, student)
            v
    TagEvaluation, TagCheck, ...       Pydantic models, served as-is by the API

`None` in a normalized field means what `null` means in the source: "not specified or unresolved in the
TAG matrix". It never means "no requirement".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# =====================================================================
# Terms and deadlines
# =====================================================================
SEASON_RANK = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}   # order within a calendar year
_TERM_RE = re.compile(r"\s*([A-Za-z]+)[\s_]+(\d{4})\s*")


@dataclass(frozen=True, order=True)
class EntryTerm:
    """An admission (entry) term such as Fall 2027. Instances sort chronologically."""

    year: int
    season_rank: int = field(repr=False)
    season: str = field(compare=False)

    @classmethod
    def of(cls, season: str, year: int) -> EntryTerm:
        s = str(season).strip().lower()
        if s not in SEASON_RANK:
            raise ValueError(f"unknown season {season!r}")
        return cls(int(year), SEASON_RANK[s], s)

    @classmethod
    def parse(cls, value: EntryTerm | str | Mapping[str, Any]) -> EntryTerm:
        """'fall_2027', 'Fall 2027' or {'season': 'fall', 'year': 2027}."""
        if isinstance(value, EntryTerm):
            return value
        if isinstance(value, Mapping):
            return cls.of(value["season"], value["year"])
        m = _TERM_RE.fullmatch(str(value))
        if not m:
            raise ValueError(f"not an entry term: {value!r} (expected e.g. 'fall_2027' or 'Fall 2027')")
        return cls.of(m.group(1), int(m.group(2)))

    @property
    def key(self) -> str:
        return f"{self.season}_{self.year}"

    @property
    def label(self) -> str:
        return f"{self.season.capitalize()} {self.year}"


@dataclass(frozen=True)
class TermDeadline:
    """A deadline at a term boundary, e.g. 'end of summer 2026'. The source gives no calendar dates for
    these, and none are invented."""

    term: EntryTerm
    boundary: str                     # 'end' | 'by_term' | 'during'
    event: str | None = None          # e.g. 'TAG submission'

    @property
    def label(self) -> str:
        base = {"end": f"End of {self.term.label}", "by_term": f"By {self.term.label}",
                "during": f"During {self.term.label}"}.get(self.boundary, self.term.label)
        return f"{base} (at {self.event})" if self.event else base


# =====================================================================
# Normalized dataset (built by data.normalize_tag_dataset)
# =====================================================================
Pages = tuple[int, ...]


@dataclass(frozen=True)
class MajorGpaException:
    major: str
    minimum_gpa: float | None


@dataclass(frozen=True)
class GpaRule:
    scope_type: str                   # 'campus' | 'all_majors' (campus-wide) or a unit: 'college' | 'school' | ...
    scope_name: str | None
    minimum_gpa: float | None
    major_exceptions: tuple[MajorGpaException, ...] = ()


@dataclass(frozen=True)
class GpaPolicy:
    rules: tuple[GpaRule, ...]
    deadline: TermDeadline | None
    deadline_term: EntryTerm | None   # the entry term the printed deadline belongs to ('fall_2027_entry')
    exceptions_override_parent: bool
    source_pages: Pages


@dataclass(frozen=True)
class ExcludedMajorRule:
    scope_type: str                   # 'major' | 'all_majors_in_school' | 'all_majors_in_college' | 'program_category'
    name: str
    degrees: tuple[str, ...] = ()     # only these degrees are excluded (empty: any degree)
    emphasis: str | None = None       # only this emphasis is excluded
    college: str | None = None        # college/school the named major belongs to
    exclusion_scope: str | None = None   # 'full_major_guarantee': only the full-major guarantee is excluded
    pre_major_name: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ExclusionList:
    rules: tuple[ExcludedMajorRule, ...]
    terms: tuple[EntryTerm, ...] | None   # entry terms the list applies to (None: not stated)
    all_majors_open: bool | None
    source_pages: Pages


@dataclass(frozen=True)
class AnnouncedExclusions:
    """Exclusions the matrix announces for a later entry term (e.g. UC Irvine from Fall 2028)."""

    effective_term: EntryTerm
    rules: tuple[ExcludedMajorRule, ...]
    in_addition_to_existing: bool     # the current exclusions continue alongside these
    source_pages: Pages


@dataclass(frozen=True)
class GeRequiredException:
    """A major (or unit) whose TAG requires completing a GE pattern (e.g. UC Merced Public Health)."""

    scope_type: str                   # 'major' or a unit scope
    name: str
    completion_required: bool | None
    acceptable_patterns: tuple[str, ...]


@dataclass(frozen=True)
class GeRecommendation:
    scope_type: str
    names: tuple[str, ...]            # normalized name first, then the name as printed
    strength: str | None              # e.g. 'highly_recommended'
    completion_required: bool | None


@dataclass(frozen=True)
class GeneralEducationPolicy:
    patterns: tuple[str, ...]         # patterns TAG accepts, e.g. ('Cal-GETC', 'IGETC')
    completion_required_by_default: bool | None
    required_exceptions: tuple[GeRequiredException, ...]
    recommendations: tuple[GeRecommendation, ...]
    source_pages: Pages


@dataclass(frozen=True)
class MajorGuaranteePolicy:
    status: str | None                # e.g. 'yes_if_all_TAG_requirements_met', 'depends_on_pre_major'
    pre_major_only_majors: tuple[str, ...]
    conditional_rules: tuple[Mapping[str, Any], ...]     # e.g. UC Santa Barbara's pre-major rules, as printed
    screening_major_names: tuple[str, ...] | None        # None: the matrix doesn't list them
    details_url: str | None
    non_screening_undescribed: bool
    source_pages: Pages


@dataclass(frozen=True)
class MajorPrepScope:
    """Which majors a major-preparation requirement covers, as the matrix states it."""

    scope_type: str                   # 'all_majors_in_college', 'named_majors_in_college', 'selective_majors', ...
    unit_names: tuple[str, ...]       # the college/school (normalized name first)
    majors: tuple[str, ...]           # named majors or major groups
    requirement: str | None


@dataclass(frozen=True)
class MajorPreparationPolicy:
    required_for: tuple[MajorPrepScope, ...]
    individual_courses: tuple[Any, ...] | None   # None: the matrix doesn't enumerate them (never "no courses")
    details_status: str | None
    separate_gpa_required_for: str | tuple[str, ...] | None
    separate_gpa_thresholds: Mapping[str, Any] | None   # None: thresholds not listed
    separate_gpa_details: str | None
    details_urls: tuple[str, ...]
    source_pages: Pages


@dataclass(frozen=True)
class FilingWindow:
    term: EntryTerm
    start: date | None
    end: date | None
    start_month_day: str | None       # 'MM-DD' when the source gives no year
    end_month_day: str | None
    year_inferred: bool               # the year comes from the extractor's inference, not the PDF
    source_pages: Pages


@dataclass(frozen=True)
class DecisionRelease:
    term: EntryTerm
    month_day: str                    # 'MM-DD'
    year: int | None
    year_inferred: bool


@dataclass(frozen=True)
class PreEvaluation:
    provided: bool | None
    wait_for_decision_before_uc_application: bool | None
    decision_releases: tuple[DecisionRelease, ...]
    source_pages: Pages


@dataclass(frozen=True)
class CourseDeadline:
    subject_area: str                 # 'UC-E' | 'UC-M'
    count: int
    ordinal: bool                     # True: "the count-th course" (course_ordinal), False: "at least count courses"
    deadline: TermDeadline


@dataclass(frozen=True)
class TermRequirements:
    """Term-specific campus criteria, e.g. UC Merced's `spring_2028_requirements`."""

    term: EntryTerm
    course_deadlines: tuple[CourseDeadline, ...]
    other_qualifications_deadline: TermDeadline | None
    application_dates: Mapping[str, Any] | None   # None: not listed for this term
    do_not_copy_application_dates: bool
    review_issue_ids: tuple[str, ...]
    source_pages: Pages


@dataclass(frozen=True)
class TagCampus:
    id: str
    name: str
    entry_terms: tuple[EntryTerm, ...]
    exclusions: ExclusionList
    announced_exclusions: tuple[AnnouncedExclusions, ...]
    gpa: GpaPolicy
    general_education: GeneralEducationPolicy
    major_guarantee: MajorGuaranteePolicy
    major_preparation: MajorPreparationPolicy
    filing_windows: tuple[FilingWindow, ...]
    pre_evaluation: PreEvaluation | None
    contact: Mapping[str, Any]
    term_requirements: Mapping[str, TermRequirements]   # keyed by EntryTerm.key
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class SharedRequirement:
    """One of the criteria every TAG campus requires. The evaluator reads its shape from `raw`
    (minimum_units, course_requirements, ...), so new items with known shapes need no code."""

    id: str
    number: int | None
    category: str
    text: str
    deadline: TermDeadline | None
    printed_deadline: TermDeadline | None     # set when the source's deadline is flagged for review
    review_status: str | None
    automatic_deadline_evaluation_allowed: bool
    review_issue_ids: tuple[str, ...]
    source_pages: Pages
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class IneligibilityRule:
    id: str
    condition: str
    source_pages: Pages


@dataclass(frozen=True)
class AutomationPolicy:
    ready_for_display: bool
    sufficient_for_final_decision: bool
    blocking_issue_ids: tuple[str, ...]
    unresolved_rule_result: str       # evaluation status for unresolved rules ('needs_review')


@dataclass(frozen=True)
class TagDataset:
    dataset_id: str
    schema_version: str
    title: str
    publisher: str | None
    accurate_as_of: str | None
    extracted_on: str | None
    subject_to_change: bool
    source_filename: str | None
    coverage_terms: tuple[EntryTerm, ...]
    shared_scope: tuple[EntryTerm, ...]       # entry terms the shared requirements (and their dates) cover
    shared_requirements: tuple[SharedRequirement, ...]
    ineligibility_rules: tuple[IneligibilityRule, ...]
    campuses: tuple[TagCampus, ...]
    review_issues: Mapping[str, Mapping[str, Any]]
    automation: AutomationPolicy
    raw: Mapping[str, Any]
    path: Path | None = None

    @property
    def label(self) -> str:
        """'UC TAG matrix 2027–28' style name for messages."""
        years = sorted({t.year for t in self.coverage_terms})
        span = f"{years[0]}–{str(years[-1])[2:]}" if len(years) > 1 else str(years[0]) if years else ""
        return f"UC TAG matrix {span}".strip()


# =====================================================================
# Evaluation input
# =====================================================================
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


TagFact = bool | float | dict[str, bool | float]


class TagGeStatus(_Model):
    """The student's GE progress, from the planner's GE engine (not from the TAG data)."""

    pattern: str | None = Field(default=None, examples=["CAL-GETC", "IGETC", "7-Course Pattern"])
    status: Literal["completed", "planned", "in_progress", "not_started"] | None = None
    completion_term: str | None = Field(default=None, description="Term the pattern is (planned to be) complete.",
                                        examples=["Spring 2028"])


class TagStudentProfile(_Model):
    """
    What the evaluator knows about the student. Every field is optional: a missing value is "unknown",
    never "no" (an unknown disqualifying condition is not assumed false, a missing GPA is not assumed
    sufficient). Fields the planner doesn't collect yet stay empty and produce needs_review.
    """

    intended_major: str | None = Field(default=None, examples=["Computer Science"])
    major_degree: str | None = Field(default=None, examples=["B.S."])
    major_emphasis: str | None = None
    major_college: str | None = Field(default=None, description="College at the TAG campus that offers the major.")
    major_school: str | None = Field(default=None, description="School at the TAG campus that offers the major.")
    major_program_category: str | None = Field(default=None, examples=["Undeclared programs"])
    uc_transferable_gpa: float | None = Field(default=None, ge=0, le=4.0,
                                              description="As of the campus's GPA deadline.")
    transferable_semester_units: float | None = Field(
        default=None, ge=0, description="UC-transferable semester units completed so far.")
    ccc_transferable_semester_units: float | None = Field(
        default=None, ge=0, description="Of those, units completed at California community colleges.")
    uc_subject_course_counts: dict[str, int] | None = Field(
        default=None, description="Completed courses per UC subject area, e.g. {'UC-E': 1, 'UC-M': 1}.")
    completed_courses: list[str] = Field(default_factory=list, description="Community college course codes.")
    ge: TagGeStatus | None = None
    major_preparation_complete: bool | None = Field(
        default=None, description="From a major-prep engine for the TAG campus (not the TAG matrix).")
    requirement_facts: dict[str, TagFact] = Field(
        default_factory=dict,
        description="Confirmed values keyed by shared requirement id: true/false, a number of units, "
                    "or an object such as {'UC-E': 1, 'UC-M': 1} or {'units': 34, 'last_regular_session_ccc': true}.")
    student_flags: dict[str, bool] = Field(
        default_factory=dict,
        description="Keyed by ineligibility rule id: true = the disqualifying condition applies.")


# =====================================================================
# Evaluation output
# =====================================================================
TagEvaluationStatus = Literal["eligible", "ineligible", "needs_review", "not_applicable"]
TagCheckStatus = Literal["met", "not_met", "unknown", "manual_review", "info"]
TagCheckGroup = Literal["coverage", "eligibility", "shared", "campus", "timeline"]


class TermRef(_Model):
    key: str = Field(examples=["fall_2027"])
    label: str = Field(examples=["Fall 2027"])

    @classmethod
    def of(cls, term: EntryTerm) -> TermRef:
        return cls(key=term.key, label=term.label)


class CampusRef(_Model):
    id: str = Field(examples=["riverside"])
    name: str = Field(examples=["UC Riverside"])


class GpaThreshold(_Model):
    minimum_gpa: float | None
    scope_type: str = Field(examples=["major", "college", "campus"])
    scope_name: str | None = Field(default=None, examples=["Computer Science"])
    parent_scope_type: str | None = None
    parent_scope_name: str | None = Field(default=None,
                                          examples=["Marlan and Rosemary Bourns College of Engineering (BCOE)"])


class GpaResolution(_Model):
    """The minimum UC-transferable GPA rule that applies to one major (most specific rule wins)."""

    status: Literal["resolved", "ambiguous", "unresolved"]
    minimum_gpa: float | None = Field(description="Set when resolved.")
    rule: GpaThreshold | None = None
    candidates: list[GpaThreshold] = Field(default_factory=list,
                                           description="Every rule that could apply, when ambiguous.")
    deadline: str | None = Field(default=None, examples=["End of Summer 2026"])
    deadline_term: TermRef | None = Field(default=None, description="Entry term the printed deadline is for.")
    explanation: str
    source_pages: list[int] = Field(default_factory=list)


class TagCheck(_Model):
    id: str = Field(examples=["campus.gpa", "shared.units_at_tag_submission"])
    group: TagCheckGroup
    status: TagCheckStatus
    title: str
    explanation: str
    required: str | None = None
    student: str | None = Field(default=None, description="What the planner knows about the student.")
    deadline: str | None = None
    urls: list[str] = Field(default_factory=list)
    source_pages: list[int] = Field(default_factory=list)
    review_issue_ids: list[str] = Field(default_factory=list)


class TagReviewItem(_Model):
    """An issue the TAG data itself flags (kept visible, never silently corrected)."""

    id: str
    severity: str
    issue: str
    resolution: str | None = None
    source_pages: list[int] = Field(default_factory=list)


class TagDatasetRef(_Model):
    id: str
    label: str = Field(examples=["UC TAG matrix 2027–28"])
    title: str
    publisher: str | None = None
    accurate_as_of: str | None = None
    extracted_on: str | None = None
    subject_to_change: bool
    source_filename: str | None = None
    entry_terms: list[TermRef]

    @classmethod
    def of(cls, d: TagDataset) -> TagDatasetRef:
        return cls(id=d.dataset_id, label=d.label, title=d.title, publisher=d.publisher,
                   accurate_as_of=d.accurate_as_of, extracted_on=d.extracted_on, subject_to_change=d.subject_to_change,
                   source_filename=d.source_filename, entry_terms=[TermRef.of(t) for t in d.coverage_terms])


class OrganizationRef(_Model):
    id: str | None = Field(default=None, examples=["donald_bren_ics"], description="Null when the student named it.")
    type: str = Field(examples=["school", "college"])
    name: str = Field(examples=["Donald Bren School of Information and Computer Sciences"])


class MajorAffiliationRef(_Model):
    """Where the major sits at the TAG campus (data/uc_major_affiliations.json, or the student's own answer)."""

    status: Literal["stated", "resolved", "ambiguous", "unresolved"] = Field(
        description="stated: the student named the college/school. resolved: one organization offers the major. "
                    "ambiguous: several offer a major by this name. unresolved: it couldn't be verified.")
    major: str | None = Field(default=None, description="As the campus names it.", examples=["Computer Science"])
    degree: str | None = Field(default=None, examples=["B.S."])
    school: OrganizationRef | None = None
    college: OrganizationRef | None = None
    candidates: list[list[OrganizationRef]] = Field(
        default_factory=list, description="When ambiguous: each organization offering it, with its parents.")
    reason: str | None = Field(default=None, description="Why it isn't resolved.")
    source_urls: list[str] = Field(default_factory=list, description="Where the affiliation comes from.")


class TagSourceContext(_Model):
    dataset: TagDatasetRef | None = None
    campus_tag_url: str | None = None
    contact: dict[str, str] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list, description="Every URL the checks cite.")


class TagEvaluation(_Model):
    status: TagEvaluationStatus
    headline: str = Field(examples=["TAG needs review"])
    summary: str
    campus: CampusRef | None = None
    entry_term: TermRef | None = None
    rules_term: TermRef | None = Field(
        default=None, description="Entry term whose rules were applied; null when none are loaded for entry_term.")
    major: str | None = None
    major_affiliation: MajorAffiliationRef | None = Field(
        default=None, description="The major's school/college at the campus, used for school- and college-level rules.")
    minimum_gpa: GpaResolution | None = None
    checks: list[TagCheck] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    review_items: list[TagReviewItem] = Field(default_factory=list)
    source: TagSourceContext = Field(default_factory=TagSourceContext)
    reference: TagEvaluation | None = Field(
        default=None,
        description="When no rules are loaded for entry_term: the campus's most recent published rules for an "
                    "earlier term, evaluated for context only. It never sets `status`.")


TagEvaluation.model_rebuild()
