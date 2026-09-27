"""
TAG eligibility: dataset + campus + entry term + what we know about the student -> TagEvaluation.

    evaluate_tag_eligibility(dataset, campus, entry_term, student)   against one dataset
    evaluate_tag(campus, entry_term, student, registry=...)           picks the dataset for the entry term

Overall status (TagEvaluation.status):
  eligible        every check is met, and the dataset says it supports a final automated decision
  ineligible      a check is definitively not met (e.g. the major is excluded for this entry term)
  needs_review    nothing is known to fail, but something is unknown or needs official verification
  not_applicable  no TAG campus selected, a campus without TAG, or no TAG at that campus for the term

Check status (TagCheck.status):
  met / not_met   decided from the data and what we know about the student
  unknown         the student information it needs isn't available (the planner doesn't collect it yet)
  manual_review   the data can't decide: unresolved, not enumerated, or flagged for review in the source
  info            context only (dates, pre-evaluation); never affects the status

Shared requirements and the campus's requirements are separate checks that must all pass (AND), for the
student's entry term only. Missing information is never read as "no requirement" or "doesn't apply".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from .affiliations import AffiliationLookup, MajorAffiliation, MajorAffiliationRegistry, default_affiliations
from .data import TagDatasetRegistry
from .models import (
    CampusRef, EntryTerm, GpaResolution, MajorAffiliationRef, OrganizationRef, SharedRequirement, TagCampus, TagCheck,
    TagDataset, TagDatasetRef, TagEvaluation, TagReviewItem, TagSourceContext, TagStudentProfile, TermRef,
    TermRequirements,
)
from .selectors import (
    UNIT_EXCLUSION_SCOPES, ExclusionMatch, MajorProfile, decision_release, describe_coverage, describe_exclusion,
    describe_prep_scope, exclusion_rules_for_term, filing_window, find_campus, fmt_gpa, format_release,
    format_window, ge_requirement, join_names, major_preparation_for, match_exclusions, norm, pattern_matches,
    reference_term, resolve_gpa_rule, same_major, shared_requirements_apply, term_requirements,
)

NO_CAMPUS = ("", "none")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# =====================================================================
# Public API
# =====================================================================
def evaluate_tag(campus: str | None, entry_term: EntryTerm | str | None, student: TagStudentProfile | None = None,
                 *, registry: TagDatasetRegistry, affiliations: MajorAffiliationRegistry | None = None) -> TagEvaluation:
    """Evaluate against the dataset that covers `entry_term`, or the latest one (for rules it announces
    for later terms, and the reference view) when none does."""
    term = EntryTerm.parse(entry_term) if entry_term else None
    return evaluate_tag_eligibility(registry.select(term), campus, term, student, affiliations=affiliations)


def evaluate_tag_eligibility(dataset: TagDataset | None, campus: str | None, entry_term: EntryTerm | str | None,
                             student: TagStudentProfile | None = None, *, include_reference: bool = True,
                             affiliations: MajorAffiliationRegistry | None = None) -> TagEvaluation:
    """
    Evaluate one student against one TAG campus for one entry term.

    campus        campus id or name ('riverside' / 'UC Riverside'); None or 'None' = no TAG campus chosen
    entry_term    'fall_2027', 'Fall 2027' or an EntryTerm: the term the student would enter the campus
    student       what's known about the student (see TagStudentProfile); everything missing is unknown
    affiliations  which school/college offers each major (default: data/uc_major_affiliations.json), for the
                  school- and college-level rules when the student doesn't name them. A major it can't place
                  stays unknown, so those rules need review.
    """
    student = student or TagStudentProfile()
    affiliations = default_affiliations() if affiliations is None else affiliations
    major = MajorProfile.from_student(student)
    major_text = student.intended_major.strip() if student.intended_major and student.intended_major.strip() else None

    if campus is None or norm(campus) in NO_CAMPUS:
        return TagEvaluation(status="not_applicable", headline="No TAG campus selected",
                             summary="TAG is optional and isn't needed for regular UC transfer planning.",
                             major=major_text)
    if dataset is None:
        return TagEvaluation(status="needs_review", headline="TAG data unavailable",
                             summary="The TAG requirements data couldn't be loaded, so TAG can't be checked right now.",
                             major=major_text)
    found = find_campus(dataset, campus)
    if found is None:
        return TagEvaluation(
            status="not_applicable", headline=f"{campus} doesn't offer TAG",
            summary=f"TAG is offered at {join_names(c.name for c in dataset.campuses)} ({dataset.label}). "
                    f"{campus} can still be your target university.",
            major=major_text, source=TagSourceContext(dataset=TagDatasetRef.of(dataset)))
    campus_ref = CampusRef(id=found.id, name=found.name)
    if entry_term is None:
        return TagEvaluation(status="needs_review", headline="Entry term not known",
                             summary=f"TAG rules depend on when you'd enter {found.name}; no entry term was given.",
                             campus=campus_ref, major=major_text, source=_source(dataset, found, []))

    lookup = affiliations.lookup(found.id, major.name, major.degree) if major.name else None
    major = MajorProfile.from_student(student, lookup)
    ctx = _Ctx(dataset, found, EntryTerm.parse(entry_term), student, major, major_text, affiliations, lookup)
    term = ctx.term
    if term not in dataset.coverage_terms:
        return _uncovered_term(ctx, include_reference)
    if term not in found.entry_terms:
        offered = join_names(t.label for t in found.entry_terms)
        others = join_names(c.name for c in dataset.campuses if term in c.entry_terms)
        return TagEvaluation(
            status="not_applicable", headline=f"{found.name} doesn't offer TAG for {term.label} entry",
            summary=f"In the {dataset.label}, {found.name} TAG is for {offered} entry."
                    + (f" {term.label} TAG is offered at {others} only." if others else ""),
            campus=campus_ref, entry_term=TermRef.of(term), major=major_text, source=_source(dataset, found, []))

    gpa = resolve_gpa_rule(found, major, term)
    tr = term_requirements(found, term)
    checks = [_coverage_check(ctx), _disqualifying_conditions_check(ctx)]
    if shared_requirements_apply(dataset, term):
        checks += [_shared_check(ctx, item) for item in dataset.shared_requirements]
    if tr is not None:
        checks += _term_requirement_checks(ctx, tr)
    elif not shared_requirements_apply(dataset, term):
        checks.append(_unlisted_shared_check(ctx))
    checks += _campus_checks(ctx, gpa)
    checks += _timeline_checks(ctx)
    return _finish(ctx, checks, gpa)


# =====================================================================
# Context + result assembly
# =====================================================================
@dataclass(frozen=True)
class _Ctx:
    dataset: TagDataset
    campus: TagCampus
    term: EntryTerm
    student: TagStudentProfile
    major: MajorProfile
    major_text: str | None
    affiliations: MajorAffiliationRegistry
    affiliation: AffiliationLookup | None          # the major at this campus, from the affiliation data

    @property
    def major_label(self) -> str:
        return self.major_text or "your major"


def _affiliation_ref(ctx: _Ctx) -> MajorAffiliationRef | None:
    """Where the major sits at the campus, and where that comes from (for the API and the TAG panel)."""
    s = ctx.student
    if any(u and u.strip() for u in (s.major_school, s.major_college)):      # the student's answer wins
        return MajorAffiliationRef(
            status="stated", major=ctx.major.name, degree=ctx.major.degree,
            school=OrganizationRef(type="school", name=s.major_school) if s.major_school else None,
            college=OrganizationRef(type="college", name=s.major_college) if s.major_college else None)
    lookup = ctx.affiliation
    if lookup is None:
        return None

    def ref(o) -> OrganizationRef | None:
        return o and OrganizationRef(id=o.id, type=o.type, name=o.name)

    def chain(a: MajorAffiliation) -> list[OrganizationRef]:
        return [ref(o) for o in a.organizations]

    a = lookup.affiliation
    return MajorAffiliationRef(
        status=lookup.status, major=a.major if a else ctx.major.name, degree=a.degree if a else ctx.major.degree,
        school=ref(a.school) if a else None, college=ref(a.college) if a else None,
        candidates=[chain(c) for c in lookup.candidates], reason=lookup.reason, source_urls=_affiliation_urls(ctx))


def _affiliation_urls(ctx: _Ctx) -> list[str]:
    return list(ctx.affiliation.source_urls) if ctx.affiliation else []


def _combine(checks: Iterable[TagCheck], unresolved: str) -> str:
    statuses = {c.status for c in checks}
    if "not_met" in statuses:
        return "ineligible"
    if statuses & {"unknown", "manual_review"}:
        return unresolved                    # the dataset's automation.unresolved_rule_result ('needs_review')
    return "eligible"


def _finish(ctx: _Ctx, checks: list[TagCheck], gpa: GpaResolution) -> TagEvaluation:
    d, auto = ctx.dataset, ctx.dataset.automation
    status = _combine(checks, auto.unresolved_rule_result)
    headline, summary = _summarize(ctx, status, checks)
    if status == "eligible" and not auto.sufficient_for_final_decision:
        status, headline = auto.unresolved_rule_result, "TAG needs review"
        summary = (f"Based on the information currently available, every {ctx.campus.name} TAG requirement in the "
                   f"{d.label} appears satisfied, but the TAG data itself isn't complete enough for a final "
                   f"automated decision. Confirm with {ctx.campus.name}.")
    review_ids = [i for c in checks for i in c.review_issue_ids]
    if not auto.sufficient_for_final_decision:
        review_ids += auto.blocking_issue_ids      # why no automated result can be final with this dataset
    return TagEvaluation(
        status=status, headline=headline, summary=summary,
        campus=CampusRef(id=ctx.campus.id, name=ctx.campus.name), entry_term=TermRef.of(ctx.term),
        rules_term=TermRef.of(ctx.term), major=ctx.major_text, major_affiliation=_affiliation_ref(ctx),
        minimum_gpa=gpa, checks=checks,
        blocking_reasons=[c.explanation for c in checks if c.status == "not_met"],
        review_items=_review_items(d, review_ids), source=_source(d, ctx.campus, checks))


def _summarize(ctx: _Ctx, status: str, checks: list[TagCheck]) -> tuple[str, str]:
    campus, term = ctx.campus.name, ctx.term.label
    not_met = [c for c in checks if c.status == "not_met"]
    if status == "ineligible":
        excluded = next((c for c in not_met if c.id == "campus.major_exclusion"), None)
        if excluded:
            return f"TAG unavailable for {ctx.major_label} at {campus}", excluded.explanation
        more = f" ({len(not_met) - 1} more below.)" if len(not_met) > 1 else ""
        return f"TAG requirements not met at {campus}", not_met[0].explanation + more
    if status == "eligible":
        return ("TAG requirements appear to be met",
                f"Based on the information currently available, every {campus} TAG requirement for {term} entry "
                f"appears satisfied. TAG criteria can change; confirm with {campus} before relying on TAG.")
    unknown = sum(c.status == "unknown" for c in checks)
    manual = sum(c.status == "manual_review" for c in checks)
    parts = []
    if unknown:
        parts.append(f"{unknown} can't be checked from the planner yet")
    if manual:
        parts.append(f"{manual} need{'s' if manual == 1 else ''} verification with {campus} or official sources")
    return ("TAG needs review",
            f"Based on the information currently available, no requirement is known to be unmet, but "
            f"{join_names(parts)}.")


def _review_items(dataset: TagDataset, ids: Iterable[str]) -> list[TagReviewItem]:
    items = []
    for i in dict.fromkeys(ids):
        issue = dataset.review_issues.get(i)
        if issue is not None:
            items.append(TagReviewItem(id=i, severity=str(issue.get("severity") or "requires_review"),
                                       issue=str(issue.get("issue") or ""), resolution=issue.get("resolution"),
                                       source_pages=list(issue.get("source_pages") or ())))
    return items


def _source(d: TagDataset, c: TagCampus, checks: Iterable[TagCheck]) -> TagSourceContext:
    contact = {k: str(v) for k, v in c.contact.items()
               if k in ("phone", "email", "advisor_url", "contact_url") and v}
    return TagSourceContext(dataset=TagDatasetRef.of(d), campus_tag_url=c.contact.get("tag_url"), contact=contact,
                            links=list(dict.fromkeys(u for ch in checks for u in ch.urls)))


# =====================================================================
# Coverage, disqualifying conditions
# =====================================================================
def _coverage_check(ctx: _Ctx) -> TagCheck:
    d = ctx.dataset
    as_of = f", accurate as of {_iso_label(d.accurate_as_of)}" if d.accurate_as_of else ""
    change = " and subject to change" if d.subject_to_change else ""
    return TagCheck(id="coverage", group="coverage", status="info",
                    title=f"{ctx.campus.name} TAG for {ctx.term.label} entry",
                    explanation=f"Checked against the {d.label} ({d.publisher or 'University of California'}{as_of}"
                                f"{change}).")


def _disqualifying_conditions_check(ctx: _Ctx) -> TagCheck:
    rules = ctx.dataset.ineligibility_rules
    base = dict(id="eligibility.disqualifying_conditions", group="eligibility", title="No disqualifying conditions",
                required="None of these applies", source_pages=sorted({p for r in rules for p in r.source_pages}))
    if not rules:
        return TagCheck(**base, status="met", explanation="The matrix lists no disqualifying conditions.")
    flags = ctx.student.student_flags
    listing = "; ".join(_sentence_piece(r.condition) for r in rules)
    applies = [r for r in rules if flags.get(r.id) is True]
    if applies:
        return TagCheck(**base, status="not_met", student="Reported: " + "; ".join(_sentence_piece(r.condition) for r in applies),
                        explanation="TAG isn't available when any of these applies, and you reported: "
                                    + "; ".join(_sentence_piece(r.condition) for r in applies) + ".")
    if any(r.id not in flags for r in rules):
        return TagCheck(**base, status="unknown", student="Not confirmed",
                        explanation=f"TAG isn't available if any of these applies: {listing}. "
                                    "The planner doesn't ask about these yet.")
    return TagCheck(**base, status="met", student="None apply (reported)",
                    explanation=f"You reported that none of these applies: {listing}.")


# =====================================================================
# Shared requirements (every TAG campus). The requirement's shape decides how it's checked.
# =====================================================================
_ACRONYMS = {"tag": "TAG", "uc": "UC", "ccc": "CCC", "tau": "TAU", "gpa": "GPA", "ge": "GE", "english": "English"}


def _shared_check(ctx: _Ctx, item: SharedRequirement) -> TagCheck:
    raw = item.raw
    fact = ctx.student.requirement_facts.get(item.id)
    base: dict[str, Any] = dict(
        id=f"shared.{item.id}", group="shared", title=_humanize(item.id),
        deadline=item.deadline.label if item.deadline else None, urls=_urls(raw),
        source_pages=list(item.source_pages), review_issue_ids=list(item.review_issue_ids))

    # Flagged in the source: shown, never passed or failed automatically
    if item.review_status or not item.automatic_deadline_evaluation_allowed:
        printed = item.printed_deadline.label if item.printed_deadline else "a date"
        issue = " ".join(str(ctx.dataset.review_issues[i].get("issue") or "") for i in item.review_issue_ids
                         if i in ctx.dataset.review_issues).strip()
        reported = (f" You reported it {'complete' if fact else 'not complete'}, but that can't settle the date."
                    if isinstance(fact, bool) else "")
        base["deadline"] = f"Printed as “{printed}” (needs verification)"
        required = f"Complete the {raw['pattern']}" if raw.get("pattern") else _strip_period(item.text)
        return TagCheck(**base, status="manual_review", required=required,
                        explanation=f"{item.text} The source contains a date that requires official verification "
                                    f"(it prints “{printed}”), so this isn't passed or failed automatically."
                                    + (f" {issue}" if issue else "") + reported)

    if isinstance(fact, bool):
        return TagCheck(**base, status="met" if fact else "not_met", required=_strip_period(item.text),
                        student="Confirmed" if fact else "Reported as not met",
                        explanation=f"{item.text} {'Confirmed.' if fact else 'Reported as not met.'}")

    if "minimum_units" in raw:
        return _minimum_units_check(ctx, item, base, fact)
    if "maximum_units" in raw:
        return _maximum_units_check(ctx, item, base, fact)
    if "maximum_units_still_needed" in raw:
        return _units_still_needed_check(item, base, fact)
    if "minimum_ccc_units" in raw:
        return _ccc_check(ctx, item, base, fact)
    if "course_requirements" in raw:
        return _course_requirements_check(ctx, item, base, fact)
    if item.category == "application":
        return _application_check(item, base)
    if raw.get("requirements_reference") == "campuses":
        return TagCheck(**base, status="info", explanation=f"{item.text} See the {ctx.campus.name} requirements.")
    if item.category == "academic_standing":
        return TagCheck(**base, status="unknown", required=_strip_period(item.text), student="Not tracked",
                        explanation=f"{item.text} The planner doesn't track grades or academic standing.")
    return TagCheck(**base, status="manual_review", required=_strip_period(item.text),
                    explanation=f"{item.text} The planner can't check this requirement automatically.")


def _unit_pair(obj: Mapping[str, Any]) -> tuple[float, str]:
    sem, qtr = obj.get("semester"), obj.get("quarter")
    return sem, f"{_n(sem)} semester" + (f" ({_n(qtr)} quarter)" if qtr is not None else "") + " units"


def _minimum_units_check(ctx: _Ctx, item: SharedRequirement, base: dict, fact: Any) -> TagCheck:
    need, text = _unit_pair(item.raw["minimum_units"])
    required = f"At least {text}"
    if isinstance(fact, (int, float)):
        ok = fact >= need
        return TagCheck(**base, status="met" if ok else "not_met", required=required, student=f"{_n(fact)} units",
                        explanation=f"{item.text} You reported {_n(fact)}" + ("." if ok else f", {_n(need - fact)} short."))
    have = ctx.student.transferable_semester_units
    if have is not None and have >= need:
        return TagCheck(**base, status="met", required=required, student=f"{_n(have)} completed",
                        explanation=f"{item.text} You've completed {_n(have)} already.")
    if have is not None:
        return TagCheck(**base, status="unknown", required=required, student=f"{_n(have)} so far",
                        explanation=f"{item.text} You have {_n(have)} so far; the planner can't tell whether you'll "
                                    f"have {_n(need)} in time.")
    return TagCheck(**base, status="unknown", required=required, student="Not entered",
                    explanation=f"{item.text} Your UC-transferable units aren't entered in the planner.")


def _maximum_units_check(ctx: _Ctx, item: SharedRequirement, base: dict, fact: Any) -> TagCheck:
    cap, text = _unit_pair(item.raw["maximum_units"])
    limits = item.raw.get("apply_lower_division_unit_limits_before_comparison")
    required = f"No more than {text}" + (" after lower-division limits" if limits else "")
    note = f" {item.raw['source_note']}" if item.raw.get("source_note") else ""
    if isinstance(fact, (int, float)):
        ok = fact <= cap
        return TagCheck(**base, status="met" if ok else "not_met", required=required, student=f"{_n(fact)} units",
                        explanation=f"{item.text} You reported {_n(fact)}.")
    have = ctx.student.transferable_semester_units
    if have is not None and have <= cap:
        return TagCheck(**base, status="met", required=required, student=f"{_n(have)} so far",
                        explanation=f"{item.text} You have {_n(have)} so far.{note}")
    if have is not None:
        return TagCheck(**base, status="unknown", required=required, student=f"{_n(have)} so far",
                        explanation=f"{item.text} You have {_n(have)}, but UC applies lower-division unit limits "
                                    f"before comparing, so the count that matters may be lower.{note}")
    return TagCheck(**base, status="unknown", required=required, student="Not entered",
                    explanation=f"{item.text}{note}")


def _units_still_needed_check(item: SharedRequirement, base: dict, fact: Any) -> TagCheck:
    cap, text = _unit_pair(item.raw["maximum_units_still_needed"])
    term = item.raw.get("term")
    if term and not base.get("deadline"):
        base["deadline"] = EntryTerm.parse(term).label + (f" (or {item.raw['alternative_term']})"
                                                          if item.raw.get("alternative_term") else "")
    required = f"At most {_n(cap)} units still needed for junior standing"
    if isinstance(fact, (int, float)):
        ok = fact <= cap
        return TagCheck(**base, status="met" if ok else "not_met", required=required, student=f"{_n(fact)} units needed",
                        explanation=f"{item.text} You reported needing {_n(fact)}.")
    return TagCheck(**base, status="unknown", required=required, student="Not entered",
                    explanation=f"{item.text} The planner doesn't know how many units you'll have before that term.")


def _ccc_check(ctx: _Ctx, item: SharedRequirement, base: dict, fact: Any) -> TagCheck:
    need, text = _unit_pair(item.raw["minimum_ccc_units"])
    institution = item.raw.get("last_regular_session_institution_type") or "California community college"
    fact = fact if isinstance(fact, Mapping) else {}
    units = fact.get("units")
    if isinstance(units, (int, float)) and not isinstance(units, bool):
        units_status = "met" if units >= need else "not_met"
    else:
        units = ctx.student.ccc_transferable_semester_units
        units_status = "met" if units is not None and units >= need else "unknown"
    last = fact.get("last_regular_session_ccc")
    last_status = "unknown" if not isinstance(last, bool) else "met" if last else "not_met"
    parts = {units_status, last_status}
    status = "not_met" if "not_met" in parts else "met" if parts == {"met"} else "unknown"
    student = ", ".join(filter(None, [f"{_n(units)} CCC units" if units is not None else None,
                                      "last term at a CCC" if last is True else
                                      "last term not at a CCC" if last is False else None])) or "Not entered"
    return TagCheck(**base, status=status, required=f"{text} at a {institution}; your last regular term there",
                    student=student, explanation=f"{item.text}" + (
                        "" if status != "unknown" else " The planner doesn't know your community college units "
                                                       "or last regular-session institution yet."))


def _course_requirements_check(ctx: _Ctx, item: SharedRequirement, base: dict, fact: Any) -> TagCheck:
    reqs = item.raw["course_requirements"]
    reported = isinstance(fact, Mapping)
    counts = fact if reported else ctx.student.uc_subject_course_counts
    statuses, needed = [], []
    for part in reqs.get("items") or ():
        area, need = part.get("uc_subject_area"), part.get("minimum_course_count") or 1
        needed.append(f"{need} {area} course{'s' if need != 1 else ''}")
        have = counts.get(area) if counts else None
        if isinstance(have, (int, float)) and not isinstance(have, bool) and have >= need:
            statuses.append("met")
        else:
            statuses.append("not_met" if reported and have is not None else "unknown")
    status = "not_met" if "not_met" in statuses else "met" if statuses and set(statuses) == {"met"} else "unknown"
    student = ", ".join(f"{a}: {_n(v)}" for a, v in counts.items()) if counts else "Not entered"
    return TagCheck(**base, status=status, required=" + ".join(needed), student=student,
                    explanation=item.text + ("" if counts else " The planner doesn't know which of your courses "
                                                               "count as UC-E or UC-M yet."))


def _application_check(item: SharedRequirement, base: dict) -> TagCheck:
    raw = item.raw
    window, required = raw.get("filing_window"), None
    if isinstance(window, Mapping) and window.get("start") and window.get("end"):
        start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        span = _span(start, end)
        base["deadline"], required = span, f"Submit {span}"
    elif raw.get("deadline_date"):
        when = _date_label(date.fromisoformat(raw["deadline_date"]))
        base["deadline"], required = f"By {when}", f"Submit by {when}"
    return TagCheck(**base, status="unknown", required=required, student="Not confirmed",
                    explanation=f"{item.text} The planner doesn't record application steps yet.")


# =====================================================================
# Term-specific criteria (e.g. UC Merced's Spring 2028 requirements)
# =====================================================================
def _term_requirement_checks(ctx: _Ctx, tr: TermRequirements) -> list[TagCheck]:
    campus, term = ctx.campus.name, tr.term.label
    counts = ctx.student.uc_subject_course_counts or {}
    pages = list(tr.source_pages)
    checks = []
    for cd in tr.course_deadlines:
        what = (f"{_ordinal(cd.count)} {cd.subject_area} course" if cd.ordinal
                else f"{cd.count} {cd.subject_area} course{'s' if cd.count != 1 else ''}")
        have = counts.get(cd.subject_area)
        ok = have is not None and have >= cd.count
        checks.append(TagCheck(
            id=f"term.{tr.term.key}.{cd.subject_area.lower().replace('-', '_')}_{cd.count}", group="shared",
            status="met" if ok else "unknown", title=_upper_first(what if cd.ordinal else f"at least {what}"),
            required=f"Complete your {what}" if cd.ordinal else f"At least {what}",
            student=f"{have} completed" if have is not None else "Not entered", deadline=cd.deadline.label,
            explanation=f"For {term} entry, {campus} requires {'your' if cd.ordinal else 'at least'} {what} by the "
                        f"{cd.deadline.label.lower()}.",
            source_pages=pages))
    scope = join_names(t.label for t in ctx.dataset.shared_scope)
    if tr.other_qualifications_deadline:
        when = tr.other_qualifications_deadline.label
        checks.append(TagCheck(
            id=f"term.{tr.term.key}.other_qualifications", group="shared", status="manual_review",
            title="All other TAG qualifications", deadline=when,
            explanation=f"Every other TAG qualification (units, grades, community college attendance, ...) must be "
                        f"met by the {when.lower()}. The matrix dates its shared criteria for {scope} entry and "
                        f"doesn't list them separately for {term} entry, so confirm them with {campus}.",
            source_pages=pages))
    if tr.application_dates is None:
        carry = f" The {scope} dates don't carry over." if tr.do_not_copy_application_dates else ""
        checks.append(TagCheck(
            id=f"term.{tr.term.key}.application_dates", group="shared", status="manual_review",
            title="UC application and TAU dates", deadline="Not listed",
            explanation=f"The matrix doesn't list the UC application or Transfer Academic Update dates for "
                        f"{term} entry.{carry}",
            review_issue_ids=list(tr.review_issue_ids), source_pages=pages))
    return checks


def _unlisted_shared_check(ctx: _Ctx) -> TagCheck:
    scope = join_names(t.label for t in ctx.dataset.shared_scope)
    return TagCheck(id="shared.unlisted", group="shared", status="manual_review", title="Shared TAG criteria",
                    explanation=f"The matrix dates its shared TAG criteria for {scope} entry and lists none for "
                                f"{ctx.term.label} entry. Confirm them with {ctx.campus.name}.")


# =====================================================================
# Campus requirements
# =====================================================================
def _campus_checks(ctx: _Ctx, gpa: GpaResolution) -> list[TagCheck]:
    exclusion = _major_exclusion_check(ctx)
    checks = [exclusion]
    if exclusion.status != "not_met":
        guarantee = _major_guarantee_check(ctx)
        if guarantee is not None:
            checks.append(guarantee)
    return checks + [_gpa_check(ctx, gpa), _ge_check(ctx), _major_preparation_check(ctx)]


def _major_exclusion_check(ctx: _Ctx) -> TagCheck:
    c, term, major = ctx.campus, ctx.term, ctx.major_label
    # School- and college-level rules are decided with the affiliation data: cite it
    by_unit = any(r.scope_type in UNIT_EXCLUSION_SCOPES for r, _, _ in exclusion_rules_for_term(c, term))
    base = dict(id="campus.major_exclusion", group="campus", title="Major open to TAG", required="Not excluded",
                source_pages=list(c.exclusions.source_pages), urls=_affiliation_urls(ctx) if by_unit else [])
    if not ctx.major.name:
        return TagCheck(**{**base, "urls": []}, status="unknown", student="Not entered",
                        explanation=f"Enter your intended major to check {c.name}'s TAG exclusions for {term.label} entry.")
    matches = [m for m in match_exclusions(c, term, ctx.major) if m.effect == "excluded"]
    definite = [m for m in matches if m.result == "match"]
    if definite:
        m = definite[0]
        pages = [a.source_pages for a in c.announced_exclusions if a.effective_term == m.effective_term]
        return TagCheck(**{**base, "source_pages": list(pages[0]) if pages and m.basis == "announced" else base["source_pages"]},
                        status="not_met", student=major, explanation=_excluded_sentence(ctx, m))
    possible = [m for m in matches if m.result == "possible"]
    if len(possible) == 1:
        m = possible[0]
        return TagCheck(**base, status="manual_review", student=major,
                        explanation=f"{c.name} excludes {describe_exclusion(m.rule)} from TAG for {term.label} entry, "
                                    f"and the planner can't rule that out for {major}: {m.reason}.")
    if possible:
        items = "; ".join(f"{describe_exclusion(m.rule)} ({m.reason})" for m in possible)
        return TagCheck(**base, status="manual_review", student=major,
                        explanation=f"{c.name} excludes some majors from TAG for {term.label} entry, and the planner "
                                    f"can't rule these out for {major}: {items}.")
    applicable = exclusion_rules_for_term(c, term)
    if not applicable and term in c.entry_terms:
        return TagCheck(**base, status="met", student=major,
                        explanation=f"{c.name} doesn't exclude any major from TAG for {term.label} entry.")
    announced = "among the exclusions announced" if term not in c.entry_terms else "on the TAG exclusion list"
    return TagCheck(**base, status="met", student=major,
                    explanation=f"{major} isn't {announced} for {c.name} {term.label} entry.")


def _excluded_sentence(ctx: _Ctx, m: ExclusionMatch) -> str:
    c, rule = ctx.campus.name, m.rule
    if rule.scope_type in UNIT_EXCLUSION_SCOPES:
        what = f"{ctx.major_label} is in {c}'s {rule.name}, whose majors are excluded from TAG"
    elif rule.scope_type == "program_category":
        what = f"{rule.name} are excluded from TAG at {c}"
    else:
        what = f"{describe_exclusion(rule)} is excluded from TAG at {c}"
    if m.basis == "announced":
        when = f" starting with {m.effective_term.label} entry, as announced in the {ctx.dataset.label}"
    elif m.basis == "announced_existing":
        when = (f" for {ctx.term.label} entry: the {ctx.dataset.label} lists it now and announces the "
                f"{m.effective_term.label} exclusions in addition to it")
    else:
        when = f" for {ctx.term.label} entry"
    return f"{what}{when}." + (f" {rule.note}" if rule.note else "")


def _major_guarantee_check(ctx: _Ctx) -> TagCheck | None:
    c, g, major = ctx.campus, ctx.campus.major_guarantee, ctx.major_label
    if not ctx.major.name:
        return None
    base = dict(id="campus.major_guarantee", group="campus", title="Major guarantee",
                required="TAG covers the full major", source_pages=list(g.source_pages))
    pre_major = [m for m in match_exclusions(c, ctx.term, ctx.major) if m.effect == "pre_major_only"]
    if pre_major or any(same_major(x, ctx.major.name) for x in g.pre_major_only_majors):
        rule = pre_major[0].rule if pre_major else None
        target = rule.pre_major_name if rule and rule.pre_major_name else f"the {major} pre-major"
        return TagCheck(**base, status="manual_review",
                        explanation=f"At {c.name}, TAG for {major} guarantees admission to {target} only; the full "
                                    f"major isn't guaranteed.")
    if g.status == "yes_if_all_TAG_requirements_met":
        return TagCheck(**base, status="met",
                        explanation=f"{c.name} TAG guarantees admission to your major once every TAG requirement is met.")
    if g.status == "depends_on_pre_major":
        rules = "; ".join(_guarantee_rule_text(r) for r in g.conditional_rules)
        return TagCheck(**base, status="manual_review",
                        explanation=(f"{rules[0].upper()}{rules[1:]}. " if rules else "")
                                    + f"The matrix doesn't list which majors have a pre-major, so check whether {major} does.")
    if g.status == "yes_for_all_screening_majors_as_printed":
        if g.screening_major_names is not None and any(same_major(n, ctx.major.name) for n in g.screening_major_names):
            return TagCheck(**base, status="met", explanation=f"{c.name} TAG guarantees {major}, a screening major.")
        unlisted = " or describe the guarantee for other majors" if g.non_screening_undescribed else ""
        return TagCheck(**base, status="manual_review", urls=[g.details_url] if g.details_url else [],
                        explanation=f"{c.name} TAG guarantees screening majors, but the matrix doesn't list which majors "
                                    f"screen{unlisted}. Check how it applies to {major}.")
    return TagCheck(**base, status="manual_review",
                    explanation=f"The matrix describes {c.name}'s major guarantee as “{g.status}”, which the planner "
                                f"can't interpret automatically.")


def _guarantee_rule_text(rule: Mapping[str, Any]) -> str:
    who = "majors with a pre-major" if rule.get("has_pre_major") else "majors without a pre-major"
    where = f" in the {rule['college']}" if rule.get("college") else ""
    what = {"full_major": "the full major", "pre_major_only": "the pre-major only"}.get(rule.get("guarantee"),
                                                                                      str(rule.get("guarantee")))
    extra = ", with more requirements after transfer for the full major" \
        if rule.get("additional_requirements_after_transfer_for_full_major") else ""
    return f"{who}{where} are guaranteed {what}{extra}"


def _gpa_check(ctx: _Ctx, res: GpaResolution) -> TagCheck:
    gpa = ctx.student.uc_transferable_gpa
    deadline = res.deadline
    if deadline is None and res.deadline_term and res.deadline_term.key != ctx.term.key:
        deadline = f"Not listed for {ctx.term.label} (printed for {res.deadline_term.label} entry)"
    base = dict(id="campus.gpa", group="campus", title="Minimum UC-transferable GPA", deadline=deadline,
                student=fmt_gpa(gpa) if gpa is not None else "Not entered", source_pages=res.source_pages)
    if res.status == "resolved":
        minimum, rule = res.minimum_gpa, res.rule
        scope = f" ({rule.scope_name})" if rule.scope_type not in ("campus", "all_majors") and rule.scope_name else ""
        required = f"{fmt_gpa(minimum)}{scope}"
        if gpa is None:
            return TagCheck(**base, status="unknown", required=required,
                            explanation=f"{res.explanation} Your UC-transferable GPA isn't entered in the planner.")
        if gpa >= minimum:
            return TagCheck(**base, status="met", required=required,
                            explanation=f"Your UC-transferable GPA ({fmt_gpa(gpa)}) meets the {fmt_gpa(minimum)} minimum. "
                                        f"{res.explanation}")
        by = f" required by the {res.deadline.lower()}" if res.deadline else ""
        return TagCheck(**base, status="not_met", required=required,
                        explanation=f"Your UC-transferable GPA ({fmt_gpa(gpa)}) is below the {fmt_gpa(minimum)} "
                                    f"minimum{by}. {res.explanation}")
    values = sorted({t.minimum_gpa for t in res.candidates if t.minimum_gpa is not None})
    if res.status == "ambiguous" and values:
        span = fmt_gpa(values[0]) if len(values) == 1 else f"{fmt_gpa(values[0])}–{fmt_gpa(values[-1])}"
        tail = ""
        if gpa is not None:
            tail = (f" Your GPA ({fmt_gpa(gpa)}) meets every listed minimum." if gpa >= values[-1] else
                    f" Your GPA ({fmt_gpa(gpa)}) is below every listed minimum." if gpa < values[0] else
                    f" Whether your GPA ({fmt_gpa(gpa)}) is enough depends on which one applies.")
        return TagCheck(**base, status="manual_review", required=f"{span}, depending on the college or school",
                        explanation=f"{res.explanation}{tail} Confirm which college or school offers your major.")
    return TagCheck(**base, status="manual_review", required="Not determined", explanation=res.explanation)


def _ge_check(ctx: _Ctx) -> TagCheck:
    c = ctx.campus
    f = ge_requirement(c, ctx.major)
    ge = ctx.student.ge
    accepted = join_names(f.patterns, "or")
    student = "Not entered"
    if ge and ge.pattern:
        student = ge.pattern + (f" · {ge.status}" if ge.status else "") + (
            f" ({ge.completion_term})" if ge.completion_term and ge.status == "planned" else "")
    base = dict(id="campus.general_education", group="campus", title="GE pattern for TAG", student=student,
                source_pages=list(c.general_education.source_pages))
    rec = ""
    if f.recommendation:
        r = f.recommendation
        rec = (f" Completing {accepted} is {r.strength} for the {r.unit}." if r.applies else
               f" If your major is in the {r.unit}, completing {accepted} is {r.strength}.")
    if f.required is True:
        matches = pattern_matches(ge.pattern if ge else None, f.patterns)
        required = f"Complete {accepted}"
        if matches is None:
            return TagCheck(**base, status="unknown", required=required,
                            explanation=f"{f.reason}. Your GE pattern isn't known to the planner.")
        if not matches:
            return TagCheck(**base, status="not_met", required=required,
                            explanation=f"{f.reason}, and your plan follows the {ge.pattern}.")
        if ge.status == "completed":
            return TagCheck(**base, status="met", required=required, explanation=f"{f.reason}; you've completed it.")
        planned = f" by {ge.completion_term}" if ge.completion_term else ""
        return TagCheck(**base, status="manual_review", required=required,
                        explanation=f"{f.reason}. Your plan completes {ge.pattern}{planned}, and the matrix doesn't "
                                    f"give a completion deadline for this requirement, so confirm it with {c.name}.")
    if f.required is None:
        return TagCheck(**base, status="manual_review", required="Unclear", explanation=f"{f.reason}.{rec}")
    return TagCheck(**base, status="met", required="Not required",
                    explanation=f"Completing a GE pattern isn't required for TAG at {c.name} (it accepts {accepted}). "
                                f"Your GE plan still comes from the planner's GE requirements.{rec}")


def _major_preparation_check(ctx: _Ctx) -> TagCheck:
    c, major = ctx.campus, ctx.major_label
    f = major_preparation_for(c, ctx.major)
    base = dict(id="campus.major_preparation", group="campus", title="Major preparation", urls=list(f.urls),
                source_pages=list(c.major_preparation.source_pages))
    separate = f" It also requires {f.separate_gpa}." if f.separate_gpa else ""
    scopes = join_names(describe_prep_scope(s) for s in f.scopes)
    said = [s.requirement.rstrip(". ") for s in f.scopes if s.requirement]
    said = f" The matrix says: “{'; '.join(said)}.”" if said else ""
    basis = (f" (the matrix's GPA table lists {major} under the {f.unit_inferred_from})"
             if f.unit_inferred_from else "")
    if f.courses is None:                    # not enumerated in the matrix: never an empty requirement
        required = "Courses not listed in the TAG matrix"
        if f.applies == "yes":
            return TagCheck(**base, status="manual_review", required=required,
                            explanation=f"{c.name} requires major preparation for {scopes}, which includes {major}"
                                        f"{basis}. The TAG matrix doesn't list the courses, so additional "
                                        f"major-preparation requirements must be verified with the campus.{said}{separate}")
        if f.applies == "possible":
            return TagCheck(**base, status="manual_review", required=required,
                            explanation=f"{c.name} requires major preparation for {scopes}. The TAG matrix doesn't list "
                                        f"the courses or name every major it covers, so additional major-preparation "
                                        f"requirements for {major} must be verified.{said}{separate}")
        listed = join_names(describe_prep_scope(s) for s in c.major_preparation.required_for)
        return TagCheck(**base, status="met", required="Not required for this major",
                        explanation=f"{c.name}'s TAG major-preparation requirements cover {listed}, which "
                                    f"doesn't include {major}.")
    if f.applies == "no":
        return TagCheck(**base, status="met", required="Not required for this major",
                        explanation=f"{c.name}'s TAG major-preparation requirements don't cover {major}.")
    done = ctx.student.major_preparation_complete
    status = "unknown" if done is None else "met" if done else "not_met"
    return TagCheck(**base, status=status, required=f"Major preparation for {scopes}",
                    explanation=f"{c.name} requires major preparation for {scopes}." + (
                        " The planner's major-preparation status for this campus isn't available." if done is None else "")
                    + separate)


# =====================================================================
# Timeline (info only)
# =====================================================================
def _timeline_checks(ctx: _Ctx) -> list[TagCheck]:
    c, term = ctx.campus, ctx.term
    checks = []
    window = filing_window(c, term)
    if window and not shared_requirements_apply(ctx.dataset, term):   # otherwise the shared TAG application item shows it
        checks.append(TagCheck(id="timeline.filing_window", group="timeline", status="info", title="TAG filing period",
                               required=format_window(window), source_pages=list(window.source_pages),
                               explanation=f"Submit the {c.name} TAG for {term.label} entry {format_window(window)}."))
    pre = c.pre_evaluation
    if pre is not None and pre.provided is not None:
        if pre.provided:
            wait = "; wait for the TAG decision before you submit the UC application" \
                if pre.wait_for_decision_before_uc_application else ""
            text = f"{c.name} pre-evaluates your coursework as part of TAG{wait}."
        else:
            text = f"{c.name} doesn't pre-evaluate coursework for TAG."
        checks.append(TagCheck(id="timeline.pre_evaluation", group="timeline", status="info",
                               title="Coursework pre-evaluation", explanation=text, source_pages=list(pre.source_pages)))
    release = decision_release(c, term)
    if release is not None:
        checks.append(TagCheck(id="timeline.decision_release", group="timeline", status="info",
                               title="TAG decision release", required=format_release(release),
                               explanation=f"{c.name} releases {term.label} TAG decisions {format_release(release)}.",
                               source_pages=list(pre.source_pages) if pre else []))
    return checks


# =====================================================================
# Entry terms without published rules
# =====================================================================
def _uncovered_term(ctx: _Ctx, include_reference: bool) -> TagEvaluation:
    """
    No rules are loaded for this entry term. Only rules the matrix explicitly announces for it (e.g. UC
    Irvine's Fall 2028 exclusions) are applied. The campus's most recent earlier rules are attached as a
    reference, which never sets the status: rules for one term are never applied to another.
    """
    d, c, term = ctx.dataset, ctx.campus, ctx.term
    coverage = describe_coverage(d)
    checks = [TagCheck(id="coverage", group="coverage", status="manual_review", title=f"TAG rules for {term.label} entry",
                       required=f"{c.name} criteria for {term.label} entry", student=None,
                       explanation=f"The loaded TAG data ({d.label}) covers {coverage}. {c.name}'s criteria for "
                                   f"{term.label} entry aren't in it, so they can't be checked yet.")]
    if exclusion_rules_for_term(c, term):
        checks.append(_major_exclusion_check(ctx))
    ref_term = reference_term(c, term) if include_reference else None
    reference = evaluate_tag_eligibility(d, c.id, ref_term, ctx.student, include_reference=False,
                                         affiliations=ctx.affiliations) if ref_term else None

    status = _combine(checks, d.automation.unresolved_rule_result)
    excluded = next((x for x in checks if x.status == "not_met"), None)
    if excluded is not None:
        headline, summary = f"TAG unavailable for {ctx.major_label} at {c.name}", excluded.explanation
    else:
        headline = f"TAG rules for {term.label} entry aren't available yet"
        summary = f"The loaded matrix covers {coverage}. " + (
            f"For reference, under {c.name}'s {ref_term.label} rules: {reference.headline}." if reference
            else f"Confirm {c.name}'s {term.label} criteria when UC publishes them.")
    return TagEvaluation(status=status, headline=headline, summary=summary,
                         campus=CampusRef(id=c.id, name=c.name), entry_term=TermRef.of(term), rules_term=None,
                         major=ctx.major_text, major_affiliation=_affiliation_ref(ctx), checks=checks,
                         blocking_reasons=[x.explanation for x in checks if x.status == "not_met"],
                         source=_source(d, c, checks), reference=reference)


# =====================================================================
# Text helpers
# =====================================================================
def _n(x: float | int | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _humanize(ident: str) -> str:
    words = [_ACRONYMS.get(w, w) for w in ident.split("_")]
    return _upper_first(" ".join(words).replace("seven course", "seven-course").replace("campus specific", "campus-specific"))


def _strip_period(text: str) -> str:
    return text.rstrip(". ")


def _sentence_piece(text: str) -> str:
    """'Already earned a bachelor degree or higher.' -> 'already earned a bachelor degree or higher'."""
    text = _strip_period(text)
    return text[0].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third", 4: "fourth"}.get(n, f"#{n}")


def _urls(raw: Mapping[str, Any]) -> list[str]:
    urls = [v for k, v in raw.items() if k.endswith("_url") and isinstance(v, str)]
    urls += [u for u in raw.get("details_urls") or () if isinstance(u, str)]
    return list(dict.fromkeys(urls))


def _date_label(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _iso_label(value: str) -> str:
    try:
        return _date_label(date.fromisoformat(value))
    except ValueError:
        return value


def _span(start: date, end: date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{_MONTHS[start.month - 1]} {start.day}–{end.day}, {start.year}"
    if start.year == end.year:
        return f"{_MONTHS[start.month - 1]} {start.day} – {_date_label(end)}"
    return f"{_date_label(start)} – {_date_label(end)}"
