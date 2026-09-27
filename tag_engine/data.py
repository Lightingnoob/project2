"""
TAG datasets: load data/uc_tag_requirements_*.json, validate it, and build the read-only normalized
model (models.TagDataset) that the selectors and evaluator use.

The source JSON is never modified. It's deep-frozen on load (dicts become read-only mappings, lists
become tuples) and kept on TagDataset.raw; the normalized objects only reference it. Unresolved values
stay None. They are never filled in or read as "no requirement".

One file per admission cycle (uc_tag_requirements_2027_2028.json, later _2028_2029.json, ...). The
registry picks the dataset whose coverage.entry_terms include a student's entry term, so a new cycle
is a new file, not new code.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .models import (
    AnnouncedExclusions, AutomationPolicy, CourseDeadline, DecisionRelease, EntryTerm, ExcludedMajorRule,
    ExclusionList, FilingWindow, GeneralEducationPolicy, GeRecommendation, GeRequiredException, GpaPolicy,
    GpaRule, IneligibilityRule, MajorGpaException, MajorGuaranteePolicy, MajorPreparationPolicy, MajorPrepScope,
    PreEvaluation, SharedRequirement, TagCampus, TagDataset, TermDeadline, TermRequirements,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATASET_GLOB = "uc_tag_requirements_*.json"
SUPPORTED_SCHEMA_MAJOR = "1"
UNRESOLVED_RESULTS = ("needs_review", "ineligible", "not_applicable")    # never 'eligible'

log = logging.getLogger("uvicorn.error")


class TagDatasetError(ValueError):
    """The dataset doesn't have the structure the engine relies on. The message names the location."""


# =====================================================================
# Read-only JSON
# =====================================================================
def freeze(value: Any) -> Any:
    """Deep read-only copy: dict -> MappingProxyType, list -> tuple."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Plain JSON-compatible copy of a frozen value (for serializing or comparing with the file)."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


# =====================================================================
# Located, typed access (errors say where)
# =====================================================================
def _fail(where: str, message: str) -> TagDatasetError:
    return TagDatasetError(f"{where}: {message}")


def _map(obj: Mapping, key: str, where: str, *, required: bool = True) -> Mapping | None:
    v = obj.get(key)
    if v is None:
        if required:
            raise _fail(where, f"missing object '{key}'")
        return None
    if not isinstance(v, Mapping):
        raise _fail(f"{where}.{key}", f"expected an object, got {type(v).__name__}")
    return v


def _seq(obj: Mapping, key: str, where: str, *, required: bool = True) -> tuple:
    v = obj.get(key)
    if v is None:
        if required:
            raise _fail(where, f"missing list '{key}'")
        return ()
    if not isinstance(v, tuple):
        raise _fail(f"{where}.{key}", f"expected a list, got {type(v).__name__}")
    return v


def _str(obj: Mapping, key: str, where: str, *, required: bool = True) -> str | None:
    v = obj.get(key)
    if v is None:
        if required:
            raise _fail(where, f"missing '{key}'")
        return None
    if not isinstance(v, str) or not v.strip():
        raise _fail(f"{where}.{key}", f"expected a non-empty string, got {v!r}")
    return v


def _bool(obj: Mapping, key: str, where: str) -> bool | None:
    v = obj.get(key)
    if v is not None and not isinstance(v, bool):
        raise _fail(f"{where}.{key}", f"expected true/false/null, got {v!r}")
    return v


def _number(obj: Mapping, key: str, where: str) -> float | None:
    v = obj.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _fail(f"{where}.{key}", f"expected a number, got {v!r}")
    return v


def _gpa(obj: Mapping, key: str, where: str) -> float | None:
    v = _number(obj, key, where)
    if v is not None and not 0 < v <= 4.0:
        raise _fail(f"{where}.{key}", f"GPA {v} is outside 0–4.0")
    return v


def _pages(obj: Mapping | None) -> tuple[int, ...]:
    return tuple(p for p in (obj or {}).get("source_pages") or () if isinstance(p, int))


def _term(value: Any, where: str) -> EntryTerm:
    try:
        return EntryTerm.parse(value)
    except (ValueError, KeyError, TypeError) as e:
        raise _fail(where, f"bad term {thaw(value)!r} ({e})") from None


def _deadline(obj: Mapping | None, where: str, event: str | None = None) -> TermDeadline | None:
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        raise _fail(where, f"expected a term deadline object, got {obj!r}")
    return TermDeadline(_term(obj, where), _str(obj, "boundary", where), event)


def _iso_date(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _fail(where, f"expected an ISO date, got {value!r}") from None


_TERM_REQUIREMENTS_KEY = re.compile(r"(winter|spring|summer|fall)_(\d{4})_requirements")


# =====================================================================
# Normalization
# =====================================================================
def load_tag_dataset(path: Path | str) -> TagDataset:
    """Read and normalize one dataset file. Raises TagDatasetError (a ValueError) or OSError."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TagDatasetError(f"{path.name}: not valid JSON ({e})") from None
    return normalize_tag_dataset(raw, path=path)


def normalize_tag_dataset(raw: Mapping[str, Any], *, path: Path | None = None) -> TagDataset:
    """raw TAG JSON -> TagDataset. The input isn't modified (a frozen copy is kept on `.raw`)."""
    if not isinstance(raw, Mapping):
        raise TagDatasetError(f"{path.name if path else 'dataset'}: the top level must be an object")
    src = freeze(raw)
    where = path.name if path else "dataset"
    try:
        return _dataset(src, where, path)
    except (KeyError, TypeError, AttributeError) as e:     # structure the checks above didn't anticipate
        raise TagDatasetError(f"{where}: unexpected structure ({type(e).__name__}: {e})") from None


def _dataset(src: Mapping, where: str, path: Path | None) -> TagDataset:
    version = _str(src, "schema_version", where)
    if version.split(".")[0] != SUPPORTED_SCHEMA_MAJOR:
        raise _fail(where, f"schema_version {version} isn't supported (expected {SUPPORTED_SCHEMA_MAJOR}.x)")
    source = _map(src, "source", where)
    coverage = _map(src, "coverage", where)
    coverage_terms = tuple(sorted({_term(t, f"{where}.coverage.entry_terms") for t in _seq(coverage, "entry_terms", where)}))
    if not coverage_terms:
        raise _fail(f"{where}.coverage", "entry_terms is empty")

    issues = {_str(i, "id", f"{where}.source_review_issues"): i for i in _seq(src, "source_review_issues", where, required=False)}
    shared_scope, shared = _shared(_map(src, "shared_requirements", where), f"{where}.shared_requirements")
    campuses = tuple(_campus(c, f"{where}.campuses[{i}]") for i, c in enumerate(_seq(src, "campuses", where)))
    automation = _automation(_map(src, "automation", where), f"{where}.automation")
    ineligibility = _ineligibility(_map(src, "ineligibility_rules", where, required=False), f"{where}.ineligibility_rules")

    # Consistency: unique campuses, campus terms inside the coverage, review-issue references resolve
    ids = [c.id for c in campuses]
    if len(ids) != len(set(ids)):
        raise _fail(where, f"duplicate campus ids in {ids}")
    for c in campuses:
        stray = [t.key for t in c.entry_terms if t not in coverage_terms]
        if stray:
            raise _fail(f"{where}.campuses[{c.id}]", f"entry terms {stray} aren't in coverage.entry_terms")
    referenced = [*automation.blocking_issue_ids,
                  *(i for s in shared for i in s.review_issue_ids),
                  *(i for c in campuses for tr in c.term_requirements.values() for i in tr.review_issue_ids)]
    dangling = sorted({i for i in referenced if i not in issues})
    if dangling:
        raise _fail(where, f"review issue ids {dangling} aren't defined in source_review_issues")

    return TagDataset(
        dataset_id=_str(src, "dataset_id", where),
        schema_version=version,
        title=_str(source, "title", f"{where}.source"),
        publisher=_str(source, "publisher", f"{where}.source", required=False),
        accurate_as_of=_str(source, "accurate_as_of", f"{where}.source", required=False),
        extracted_on=_str(src, "extracted_on", where, required=False),
        subject_to_change=source.get("subject_to_change") is not False,     # unknown -> assume it can change
        source_filename=_str(source, "filename", f"{where}.source", required=False),
        coverage_terms=coverage_terms,
        shared_scope=shared_scope,
        shared_requirements=shared,
        ineligibility_rules=ineligibility,
        campuses=campuses,
        review_issues=MappingProxyType(issues),
        automation=automation,
        raw=src,
        path=path,
    )


def _shared(sr: Mapping, where: str) -> tuple[tuple[EntryTerm, ...], tuple[SharedRequirement, ...]]:
    scope = sr.get("scope")
    scope_terms = tuple(_term(t, f"{where}.scope") for t in (scope if isinstance(scope, tuple) else (scope,)) if t)
    if not scope_terms:
        raise _fail(where, "missing 'scope' (the entry term(s) these requirements are dated for)")
    if sr.get("operator", "AND") != "AND":
        raise _fail(f"{where}.operator", f"only AND is supported, got {sr.get('operator')!r}")
    items = []
    for i, item in enumerate(_seq(sr, "items", where)):
        w = f"{where}.items[{i}]"
        items.append(SharedRequirement(
            id=_str(item, "id", w),
            number=_number(item, "source_item_number", w),
            category=_str(item, "category", w),
            text=_str(item, "requirement", w),
            deadline=_deadline(item.get("deadline"), f"{w}.deadline", item.get("deadline_event")),
            printed_deadline=_deadline(item.get("deadline_as_printed"), f"{w}.deadline_as_printed"),
            review_status=_str(item, "review_status", w, required=False),
            automatic_deadline_evaluation_allowed=_bool(item, "automatic_deadline_evaluation_allowed", w) is not False,
            review_issue_ids=tuple(item.get("review_issue_ids") or ()),
            source_pages=_pages(item),
            raw=item,
        ))
    ids = [s.id for s in items]
    if len(ids) != len(set(ids)):
        raise _fail(where, "duplicate requirement ids")
    return scope_terms, tuple(items)


def _ineligibility(ir: Mapping | None, where: str) -> tuple[IneligibilityRule, ...]:
    if ir is None:
        return ()
    if ir.get("operator", "ANY_DISQUALIFIES") != "ANY_DISQUALIFIES":
        raise _fail(f"{where}.operator", f"only ANY_DISQUALIFIES is supported, got {ir.get('operator')!r}")
    return tuple(IneligibilityRule(_str(r, "id", f"{where}.items[{i}]"), _str(r, "condition", f"{where}.items[{i}]"),
                                   _pages(r)) for i, r in enumerate(_seq(ir, "items", where)))


def _automation(a: Mapping, where: str) -> AutomationPolicy:
    result = a.get("unresolved_rule_result", "needs_review")
    if result not in UNRESOLVED_RESULTS:
        raise _fail(f"{where}.unresolved_rule_result", f"must be one of {UNRESOLVED_RESULTS}, got {result!r}")
    return AutomationPolicy(
        ready_for_display=_bool(a, "ready_for_display_and_filtering", where) is True,
        # Only an explicit true allows a final automated decision
        sufficient_for_final_decision=_bool(a, "sufficient_for_final_automated_TAG_eligibility", where) is True,
        blocking_issue_ids=tuple(a.get("blocking_issue_ids") or ()),
        unresolved_rule_result=result,
    )


def _campus(c: Mapping, where: str) -> TagCampus:
    cid = _str(c, "id", where)
    where = f"{where}({cid})"
    entry_terms = tuple(sorted(_term(t, f"{where}.entry_terms") for t in _seq(c, "entry_terms", where)))
    if not entry_terms:
        raise _fail(where, "entry_terms is empty")
    announced = c.get("future_exclusions")
    announced = announced if isinstance(announced, tuple) else (announced,) if announced else ()
    term_requirements = {}
    for key, value in c.items():
        m = _TERM_REQUIREMENTS_KEY.fullmatch(key)
        if m:
            term = EntryTerm.of(m.group(1), int(m.group(2)))
            term_requirements[term.key] = _term_requirements(term, value, f"{where}.{key}")
    return TagCampus(
        id=cid,
        name=_str(c, "name", where),
        entry_terms=entry_terms,
        exclusions=_exclusions(_map(c, "excluded_majors", where), f"{where}.excluded_majors"),
        announced_exclusions=tuple(_announced(a, f"{where}.future_exclusions") for a in announced),
        gpa=_gpa_policy(_map(c, "minimum_uc_transferable_gpa", where), f"{where}.minimum_uc_transferable_gpa"),
        general_education=_ge_policy(_map(c, "general_education", where), f"{where}.general_education"),
        major_guarantee=_guarantee(_map(c, "major_guarantee", where), f"{where}.major_guarantee"),
        major_preparation=_preparation(_map(c, "major_preparation", where), f"{where}.major_preparation"),
        filing_windows=tuple(_window(w, f"{where}.tag_filing_windows[{i}]")
                             for i, w in enumerate(_seq(c, "tag_filing_windows", where, required=False))),
        pre_evaluation=_pre_evaluation(_map(c, "coursework_pre_evaluation", where, required=False),
                                       f"{where}.coursework_pre_evaluation"),
        contact=c.get("contact") or MappingProxyType({}),
        term_requirements=MappingProxyType(term_requirements),
        raw=c,
    )


def _excluded_rule(r: Mapping, where: str) -> ExcludedMajorRule:
    degrees = tuple(r.get("degrees") or ()) + ((r["degree"],) if r.get("degree") else ())
    return ExcludedMajorRule(
        scope_type=_str(r, "scope_type", where), name=_str(r, "name", where), degrees=degrees,
        emphasis=_str(r, "emphasis", where, required=False), college=_str(r, "college", where, required=False),
        exclusion_scope=_str(r, "exclusion_scope", where, required=False),
        pre_major_name=_str(r, "pre_major_name", where, required=False), note=_str(r, "note", where, required=False))


def _exclusions(x: Mapping, where: str) -> ExclusionList:
    terms = []
    if x.get("applies_to_entry_term") is not None:
        terms.append(_term(x["applies_to_entry_term"], f"{where}.applies_to_entry_term"))
    terms += [_term(t, f"{where}.applies_to_entry_terms") for t in x.get("applies_to_entry_terms") or ()]
    return ExclusionList(
        rules=tuple(_excluded_rule(r, f"{where}.rules[{i}]") for i, r in enumerate(_seq(x, "rules", where))),
        terms=tuple(sorted(set(terms))) or None,
        all_majors_open=_bool(x, "all_majors_open_as_printed", where),
        source_pages=_pages(x))


def _announced(a: Mapping, where: str) -> AnnouncedExclusions:
    return AnnouncedExclusions(
        effective_term=_term(_map(a, "effective_entry_term", where), f"{where}.effective_entry_term"),
        rules=tuple(_excluded_rule(r, f"{where}.rules[{i}]") for i, r in enumerate(_seq(a, "rules", where))),
        in_addition_to_existing=_bool(a, "in_addition_to_existing_exclusions", where) is True,
        source_pages=_pages(a))


def _gpa_policy(g: Mapping, where: str) -> GpaPolicy:
    rules = []
    for i, r in enumerate(_seq(g, "rules", where)):
        w = f"{where}.rules[{i}]"
        rules.append(GpaRule(
            scope_type=_str(r, "scope_type", w), scope_name=_str(r, "scope_name", w, required=False),
            minimum_gpa=_gpa(r, "minimum_gpa", w),
            major_exceptions=tuple(MajorGpaException(_str(e, "major", f"{w}.major_exceptions[{j}]"),
                                                     _gpa(e, "minimum_gpa", f"{w}.major_exceptions[{j}]"))
                                   for j, e in enumerate(_seq(r, "major_exceptions", w, required=False)))))
    scope = _str(g, "deadline_scope", where, required=False)
    return GpaPolicy(
        rules=tuple(rules),
        deadline=_deadline(g.get("deadline"), f"{where}.deadline"),
        deadline_term=_term(scope.removesuffix("_entry"), f"{where}.deadline_scope") if scope else None,
        exceptions_override_parent=_bool(g, "major_exception_overrides_parent_rule", where) is not False,
        source_pages=_pages(g))


def _patterns(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):                      # {"operator": "OR", "items": [...]}
        return tuple(value.get("items") or ())
    return tuple(value or ())


def _ge_policy(g: Mapping, where: str) -> GeneralEducationPolicy:
    patterns = _patterns(g.get("patterns"))
    required = []
    for i, e in enumerate(_seq(g, "required_exceptions", where, required=False)):
        w = f"{where}.required_exceptions[{i}]"
        scope_type = e.get("scope_type") or "major"
        name = e.get("major") if scope_type == "major" else e.get("name")
        if not name:
            raise _fail(w, "needs a 'major' (or scope_type + name)")
        required.append(GeRequiredException(scope_type, name, _bool(e, "completion_required", w),
                                            _patterns(e.get("acceptable_patterns")) or patterns))
    recommendations = []
    for i, r in enumerate(_seq(g, "recommendations", where, required=False)):
        w = f"{where}.recommendations[{i}]"
        names = tuple(dict.fromkeys(n for n in (r.get("normalized_name"), r.get("name_as_printed"), r.get("name")) if n))
        if not names:
            raise _fail(w, "needs a name")
        recommendations.append(GeRecommendation(_str(r, "scope_type", w), names, r.get("recommendation_strength"),
                                                _bool(r, "completion_required", w)))
    return GeneralEducationPolicy(patterns, _bool(g, "completion_required_by_default", where),
                                  tuple(required), tuple(recommendations), _pages(g))


def _guarantee(g: Mapping, where: str) -> MajorGuaranteePolicy:
    screening = g.get("screening_major_names")
    return MajorGuaranteePolicy(
        status=_str(g, "status", where, required=False),
        pre_major_only_majors=tuple(_str(e, "major", f"{where}.exceptions[{i}]")
                                    for i, e in enumerate(_seq(g, "exceptions", where, required=False))
                                    if e.get("guarantee") == "pre_major_only"),
        conditional_rules=_seq(g, "rules", where, required=False),
        screening_major_names=tuple(screening) if screening is not None else None,
        details_url=_str(g, "screening_major_details_url", where, required=False),
        non_screening_undescribed=_bool(g, "non_screening_major_guarantee_not_separately_described", where) is True,
        source_pages=_pages(g))


def _preparation(p: Mapping, where: str) -> MajorPreparationPolicy:
    scopes = []
    for i, s in enumerate(_seq(p, "required_for", where, required=False)):
        w = f"{where}.required_for[{i}]"
        units = tuple(dict.fromkeys(n for n in (s.get("normalized_name"), s.get("name"), s.get("name_as_printed"),
                                                s.get("college")) if n))
        scopes.append(MajorPrepScope(_str(s, "scope_type", w), units,
                                     tuple(s.get("majors") or s.get("major_groups") or ()),
                                     _str(s, "requirement", w, required=False)))
    courses = p.get("individual_courses")
    separate = _map(p, "separate_major_preparation_gpa", where, required=False) or {}
    return MajorPreparationPolicy(
        required_for=tuple(scopes),
        individual_courses=courses if courses is None else tuple(courses),
        details_status=_str(p, "individual_course_details_status", where, required=False),
        separate_gpa_required_for=separate.get("required_for"),
        separate_gpa_thresholds=separate.get("numeric_thresholds"),
        separate_gpa_details=separate.get("details"),
        details_urls=tuple(p.get("details_urls") or ()),
        source_pages=_pages(p))


def _window(w: Mapping, where: str) -> FilingWindow:
    term = _term(_map(w, "entry_term", where), f"{where}.entry_term")
    if w.get("start") and w.get("end"):
        return FilingWindow(term, _iso_date(w["start"], f"{where}.start"), _iso_date(w["end"], f"{where}.end"),
                            None, None, False, _pages(w))
    start_md, end_md = _str(w, "start_month_day", where), _str(w, "end_month_day", where)
    year = w.get("year_explicit_in_source") or w.get("inferred_calendar_year")
    inferred = not w.get("year_explicit_in_source")
    if year is None:                                    # no year at all: keep month/day only
        return FilingWindow(term, None, None, start_md, end_md, True, _pages(w))
    return FilingWindow(term, _iso_date(f"{year}-{start_md}", f"{where}.start_month_day"),
                        _iso_date(f"{year}-{end_md}", f"{where}.end_month_day"), start_md, end_md, inferred, _pages(w))


def _pre_evaluation(p: Mapping | None, where: str) -> PreEvaluation | None:
    if p is None:
        return None
    releases = []
    for i, d in enumerate(_seq(p, "decision_release_dates", where, required=False)):
        w = f"{where}.decision_release_dates[{i}]"
        explicit = d.get("year_explicit_in_source")
        releases.append(DecisionRelease(_term(_str(d, "entry_term", w), w), _str(d, "month_day", w),
                                        explicit or d.get("inferred_calendar_year"), not explicit))
    return PreEvaluation(_bool(p, "provided", where), _bool(p, "wait_for_TAG_decision_before_UC_application", where),
                         tuple(releases), _pages(p))


def _term_requirements(term: EntryTerm, t: Mapping, where: str) -> TermRequirements:
    deadlines = []
    for i, d in enumerate(_seq(t, "course_deadlines", where, required=False)):
        w = f"{where}.course_deadlines[{i}]"
        ordinal = d.get("course_ordinal")
        count = ordinal if ordinal is not None else d.get("minimum_course_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise _fail(w, "needs course_ordinal or minimum_course_count (a positive integer)")
        deadlines.append(CourseDeadline(_str(d, "uc_subject_area", w), count, ordinal is not None,
                                        _deadline(_map(d, "deadline", w), f"{w}.deadline")))
    return TermRequirements(
        term=term,
        course_deadlines=tuple(deadlines),
        other_qualifications_deadline=_deadline(t.get("all_other_TAG_qualifications_deadline"),
                                                f"{where}.all_other_TAG_qualifications_deadline"),
        application_dates=t.get("application_and_TAU_dates"),
        do_not_copy_application_dates=_bool(t, "do_not_copy_fall_2027_application_dates", where) is True,
        review_issue_ids=tuple(t.get("review_issue_ids") or ()),
        source_pages=_pages(t))


# =====================================================================
# Registry: every dataset in data/, looked up by entry term
# =====================================================================
@dataclass(frozen=True)
class TagDatasetRegistry:
    datasets: tuple[TagDataset, ...] = ()
    errors: tuple[str, ...] = ()          # files that failed to load (already logged)

    def for_term(self, term: EntryTerm | str | None) -> TagDataset | None:
        """The dataset covering this entry term (the most recently extracted one if several do)."""
        if term is None:
            return None
        term = EntryTerm.parse(term)
        covering = [d for d in self.datasets if term in d.coverage_terms]
        return max(covering, key=_freshness) if covering else None

    @property
    def latest(self) -> TagDataset | None:
        """The dataset with the latest coverage (it also carries any announced future rules)."""
        return max(self.datasets, key=lambda d: (max(d.coverage_terms), _freshness(d))) if self.datasets else None

    def select(self, term: EntryTerm | str | None) -> TagDataset | None:
        """for_term(term), falling back to the latest dataset (for announced rules and references)."""
        return self.for_term(term) or self.latest


def _freshness(d: TagDataset) -> tuple[str, str]:
    return (d.accurate_as_of or "", d.extracted_on or "")


def load_registry(data_dir: Path | str = DATA_DIR, pattern: str = DATASET_GLOB) -> TagDatasetRegistry:
    """
    Load every dataset file. A broken file is logged and skipped so the API keeps running; with no
    usable dataset, TAG evaluations say the data is unavailable (needs_review) instead of failing.
    """
    datasets: list[TagDataset] = []
    errors: list[str] = []
    for path in sorted(Path(data_dir).glob(pattern)):
        try:
            datasets.append(load_tag_dataset(path))
        except (OSError, ValueError) as e:
            errors.append(str(e) if isinstance(e, TagDatasetError) else f"{path.name}: {e}")
            log.error("TAG dataset %s could not be loaded (%s). The API keeps running without it.", path, e)
    if not datasets and not errors:
        log.warning("No TAG dataset found in %s (%s); TAG evaluations will report the data as unavailable.",
                    data_dir, pattern)
    return TagDatasetRegistry(tuple(sorted(datasets, key=lambda d: d.coverage_terms)), tuple(errors))


def campus_directory(registry: TagDatasetRegistry) -> list[dict[str, Any]]:
    """Every TAG campus across the loaded datasets, with the entry terms their rules cover (for the
    planner's TAG University dropdown)."""
    campuses: dict[str, dict[str, Any]] = {}
    for d in sorted(registry.datasets, key=lambda d: d.coverage_terms):
        for c in d.campuses:
            entry = campuses.setdefault(c.id, {"id": c.id, "terms": set()})
            entry.update(name=c.name, tag_url=c.contact.get("tag_url"))   # the newest dataset's name/URL win
            entry["terms"].update(c.entry_terms)
    return [{"id": e["id"], "name": e["name"], "tag_url": e["tag_url"],
             "entry_terms": [{"key": t.key, "label": t.label} for t in sorted(e["terms"])]}
            for e in sorted(campuses.values(), key=lambda e: e["name"])]


def registry_from(datasets: Sequence[TagDataset]) -> TagDatasetRegistry:
    """A registry over already-loaded datasets (tests, or callers that load files themselves)."""
    return TagDatasetRegistry(tuple(sorted(datasets, key=lambda d: d.coverage_terms)))

