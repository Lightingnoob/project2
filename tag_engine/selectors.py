"""
Read-side helpers over a normalized TagDataset: campus/term lookup, the exclusion matcher, the GPA
resolver, and the GE / major-preparation / timeline lookups. The evaluator composes them.

Nothing here guesses. When the data (or what we know about the student's major) can't decide, the
result says so: an exclusion match is 'possible', a GPA resolution is 'ambiguous' or 'unresolved',
a GE requirement is None. The evaluator turns those into needs_review.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .models import (
    DecisionRelease, EntryTerm, ExcludedMajorRule, FilingWindow, GpaResolution, GpaRule, GpaThreshold, MajorPrepScope,
    TagCampus, TagDataset, TagStudentProfile, TermRef, TermRequirements,
)

if TYPE_CHECKING:
    from .affiliations import AffiliationLookup

# =====================================================================
# Name matching (majors, colleges/schools, GE patterns)
# =====================================================================
_DEGREE = re.compile(r"^(?P<base>.*?)[\s,]*\(?\b(?P<degree>B\.?\s*Mus\.?|B\.?\s*(?:F\.?\s*A|A|S|M)\.?|A\.?\s*B\.?"
                     r"|Bachelor of (?:Fine Arts|Arts|Science|Music))\)?\s*$", re.IGNORECASE)
_DEGREE_KEYS = {"bachelorofscience": "bs", "bachelorofarts": "ba", "ab": "ba", "bacheloroffinearts": "bfa",
                "bachelorofmusic": "bm", "bmus": "bm"}
_PARENS = re.compile(r"\(([^)]*)\)")


def norm(text: str | None) -> str:
    """Case-, accent- and punctuation-insensitive key: 'Ecology & Evolutionary Biology' ->
    'ecology and evolutionary biology'."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("w/", " with ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def split_degree(name: str) -> tuple[str, str | None]:
    """'Psychology, B.S.' / 'Psychology B.S.' / 'Psychology, Bachelor of Science' -> ('Psychology', ...);
    names without a degree are returned unchanged."""
    m = _DEGREE.match(name.strip())
    if m and m.group("base").strip():
        return m.group("base").strip(), m.group("degree")
    return name.strip(), None


def degree_key(degree: str) -> str:
    """'B.F.A.' -> 'bfa'; 'Bachelor of Science' -> 'bs'; 'A.B.' (UC Davis) -> 'ba'; 'B.Mus.' -> 'bm'."""
    letters = re.sub(r"[^a-z]", "", degree.lower())
    return _DEGREE_KEYS.get(letters, letters)


def same_major(a: str | None, b: str | None) -> bool:
    """Exact major match (degree suffix, case and punctuation ignored). 'Computer Science' does NOT match
    'Computer Science w/ Business Applications'."""
    return bool(a and b) and norm(split_degree(a)[0]) == norm(split_degree(b)[0])


def unit_keys(name: str) -> set[str]:
    """Keys a college/school name can be referred to by: its name, its name without the parenthetical,
    and the parenthetical abbreviation ('Marlan and Rosemary Bourns College of Engineering (BCOE)' ->
    'bcoe')."""
    keys = {norm(name), norm(_PARENS.sub(" ", name))}
    keys |= {norm(a) for a in _PARENS.findall(name)}
    return {k for k in keys if k}


def unit_relation(a: str, b: str) -> Literal["same", "maybe", "different"]:
    """
    'same': the names (or their abbreviations) are equal. 'maybe': one name contains the other as whole
    words ('Robert Mehrabian College of Engineering' vs 'College of Engineering'), which is suggestive
    but not proof. 'different' otherwise.
    """
    ka, kb = unit_keys(a), unit_keys(b)
    if ka & kb:
        return "same"
    if any(f" {x} " in f" {y} " or f" {y} " in f" {x} " for x in ka for y in kb):
        return "maybe"
    return "different"


def unit_match(names: Iterable[str], units: Iterable[str]) -> bool | None:
    """Is the student's college/school one of `names`? True / False, or None when unknown or only a
    'maybe' match."""
    units = tuple(units)
    if not units:
        return None
    relations = {unit_relation(n, u) for n in names for u in units}
    return True if "same" in relations else None if "maybe" in relations else False


def join_names(names: Iterable[str], conj: str = "and") -> str:
    names = list(dict.fromkeys(names))
    if len(names) <= 2:
        return f" {conj} ".join(names)
    return f"{', '.join(names[:-1])}, {conj} {names[-1]}"


def fmt_gpa(gpa: float | None) -> str:
    return "not listed" if gpa is None else f"{gpa:.1f}" if round(gpa, 1) == gpa else f"{gpa:.2f}"


# =====================================================================
# The student's major, as far as we know it
# =====================================================================
@dataclass(frozen=True)
class MajorProfile:
    name: str | None
    degree: str | None
    emphasis: str | None
    units: tuple[str, ...]            # its college/school at the TAG campus (official names): the student's
                                      # answer, else the affiliation data
    program_category: str | None
    unit_aliases: tuple[str, ...] = ()                # other names of those units, for matching only
    unit_options: tuple[tuple[str, ...], ...] = ()    # units unknown, but the data lists the major under these
    unit_note: str | None = None                      # why the units aren't known (a clause)

    @property
    def unit_names(self) -> tuple[str, ...]:
        return self.units + self.unit_aliases

    def in_unit(self, names: Iterable[str]) -> bool | None:
        """Is the major in the unit called any of `names`? None when unknown, when only a 'maybe' name matches,
        or when it's true for some of the organizations the data lists the major under but not all."""
        names = tuple(names)
        if self.unit_names:
            return unit_match(names, self.unit_names)
        found = {unit_match(names, option) for option in self.unit_options}
        return found.pop() if len(found) == 1 else None

    @classmethod
    def from_student(cls, s: TagStudentProfile, lookup: AffiliationLookup | None = None) -> MajorProfile:
        """The student's own college/school wins; otherwise `lookup`, the major's affiliation at the campus."""
        base, degree = split_degree(s.intended_major) if s.intended_major and s.intended_major.strip() else (None, None)
        units = tuple(u for u in (s.major_college, s.major_school) if u and u.strip())
        aliases, options, note = (), (), None
        if not units and lookup is not None:
            if lookup.affiliation is not None:
                units = lookup.affiliation.units
                aliases = tuple(n for n in lookup.affiliation.unit_names if n not in units)
            else:
                options = tuple(a.unit_names for a in lookup.candidates)
                note = lookup.reason
        return cls(name=base, degree=s.major_degree or degree, emphasis=s.major_emphasis, units=units,
                   program_category=s.major_program_category, unit_aliases=aliases, unit_options=options,
                   unit_note=note)


# =====================================================================
# Campus + term lookup
# =====================================================================
def campus_keys(campus_id: str, *names: str) -> set[str]:
    """Keys a campus can be named by: its id, and each name with and without 'UC' ('UC Santa Barbara',
    'Santa Barbara')."""
    keys = {norm(campus_id)}
    for n in names:
        keys |= {norm(n), norm(n).removeprefix("uc ")}
    return {k for k in keys if k}


def find_campus(dataset: TagDataset, ref: str) -> TagCampus | None:
    """By id ('santa_barbara'), name ('UC Santa Barbara') or name without 'UC' ('Santa Barbara')."""
    key = norm(ref)
    return next((c for c in dataset.campuses if key in campus_keys(c.id, c.name)), None)


def campuses_offering(dataset: TagDataset, term: EntryTerm) -> list[TagCampus]:
    return [c for c in dataset.campuses if term in c.entry_terms]


def describe_coverage(dataset: TagDataset) -> str:
    """'Fall 2027 entry (and Spring 2028 entry at UC Merced only)'."""
    full, partial = [], []
    for term in dataset.coverage_terms:
        offering = campuses_offering(dataset, term)
        if len(offering) == len(dataset.campuses):
            full.append(term.label)
        else:
            partial.append(f"{term.label} entry at {join_names(c.name for c in offering)} only")
    if not full:
        return join_names(partial)
    return f"{join_names(full)} entry" + (f" (and {join_names(partial)})" if partial else "")


def reference_term(campus: TagCampus, term: EntryTerm) -> EntryTerm | None:
    """The campus's most recent earlier term of the same season (Fall 2027 for Fall 2028)."""
    earlier = [t for t in campus.entry_terms if t.season == term.season and t < term]
    return max(earlier) if earlier else None


def shared_requirements_apply(dataset: TagDataset, term: EntryTerm) -> bool:
    """The shared requirements (and their dates) are printed for specific entry terms only."""
    return term in dataset.shared_scope


def term_requirements(campus: TagCampus, term: EntryTerm) -> TermRequirements | None:
    return campus.term_requirements.get(term.key)


# =====================================================================
# Exclusions
# =====================================================================
MatchResult = Literal["match", "possible", "no_match"]
ExclusionBasis = Literal["current", "announced", "announced_existing", "announced_earlier"]
UNIT_EXCLUSION_SCOPES = {"all_majors_in_school": "school", "all_majors_in_college": "college"}

# Program categories the matcher can recognize from a major's name. A category missing here can only be
# matched through TagStudentProfile.major_program_category; otherwise it stays a possible match.
PROGRAM_CATEGORY_TERMS = {"undeclared programs": ("undeclared",)}


@dataclass(frozen=True)
class ExclusionMatch:
    rule: ExcludedMajorRule
    result: MatchResult
    basis: ExclusionBasis
    effective_term: EntryTerm | None      # for announced exclusions: the term they start with
    reason: str                           # why the match is only possible

    @property
    def effect(self) -> Literal["excluded", "pre_major_only"]:
        """Some entries only remove the full-major guarantee (TAG still covers the pre-major)."""
        return "pre_major_only" if self.rule.exclusion_scope == "full_major_guarantee" else "excluded"


def exclusion_rules_for_term(campus: TagCampus, term: EntryTerm) -> list[tuple[ExcludedMajorRule, ExclusionBasis, EntryTerm | None]]:
    """
    The exclusion rules that apply to one entry term, evaluated by term:
      - the current list, for the terms it states (the campus's matrix terms if it doesn't say);
      - an announced list (e.g. UC Irvine from Fall 2028), only for its effective term, together with the
        current list when it says it's in addition to it. Terms before it never see it; later terms get
        it as 'announced_earlier', which can only produce a possible match.
    """
    rules: list[tuple[ExcludedMajorRule, ExclusionBasis, EntryTerm | None]] = []
    current_terms = campus.exclusions.terms if campus.exclusions.terms is not None else campus.entry_terms
    if term in current_terms:
        rules += [(r, "current", None) for r in campus.exclusions.rules]
    for announced in campus.announced_exclusions:
        eff = announced.effective_term
        if term == eff:
            rules += [(r, "announced", eff) for r in announced.rules]
            if announced.in_addition_to_existing and term not in current_terms:
                rules += [(r, "announced_existing", eff) for r in campus.exclusions.rules]
        elif term > eff and term not in current_terms:
            rules += [(r, "announced_earlier", eff) for r in announced.rules]
    return rules


def match_exclusion(rule: ExcludedMajorRule, major: MajorProfile) -> tuple[MatchResult, str]:
    """Does this exclusion rule cover the student's major? Supports every scope type in the matrix; an
    unknown scope type is a possible match (never silently ignored)."""
    scope = rule.scope_type
    if scope == "major":
        if not major.name:
            return "possible", "your intended major isn't known"
        if not same_major(rule.name, major.name):
            return "no_match", ""
        missing = []
        if rule.degrees:
            if major.degree is None:
                missing.append(f"the {join_names(rule.degrees, 'and')} degree{'s' if len(rule.degrees) > 1 else ''}")
            elif degree_key(major.degree) not in {degree_key(d) for d in rule.degrees}:
                return "no_match", ""
        if rule.emphasis:
            if major.emphasis is None:
                missing.append(f"the {rule.emphasis} emphasis")
            elif norm(major.emphasis) != norm(rule.emphasis):
                return "no_match", ""
        if rule.college and major.in_unit([rule.college]) is False:
            return "no_match", ""
        if missing:
            return "possible", f"it covers only {join_names(missing)}, and yours isn't known"
        return "match", ""
    if scope in UNIT_EXCLUSION_SCOPES:
        kind = UNIT_EXCLUSION_SCOPES[scope]
        found = major.in_unit([rule.name])
        if found is None and major.units:
            return "possible", f"'{join_names(major.units)}' may be the {rule.name}"
        if found is None:
            return "possible", major.unit_note or f"your major's {kind} at this campus isn't known"
        return ("match", "") if found else ("no_match", "")
    if scope == "program_category":
        if major.program_category:
            return ("match", "") if norm(major.program_category) == norm(rule.name) else ("no_match", "")
        terms = PROGRAM_CATEGORY_TERMS.get(norm(rule.name))
        if terms and major.name:
            words = norm(major.name).split()
            return ("match", "") if any(t in words for t in terms) else ("no_match", "")
        return "possible", "your program category isn't known"
    return "possible", f"the matrix uses an exclusion scope ('{scope}') the planner can't evaluate"


def match_exclusions(campus: TagCampus, term: EntryTerm, major: MajorProfile) -> list[ExclusionMatch]:
    """Every exclusion rule for this term that matches or may match (no_match results are dropped)."""
    out = []
    for rule, basis, eff in exclusion_rules_for_term(campus, term):
        result, reason = match_exclusion(rule, major)
        if result == "match" and basis == "announced_earlier":
            result, reason = "possible", (f"it was announced to start with {eff.label}, and the rules for "
                                          f"{term.label} aren't published in the loaded data")
        if result != "no_match":
            out.append(ExclusionMatch(rule, result, basis, eff, reason))
    return out


def describe_exclusion(rule: ExcludedMajorRule) -> str:
    """'Computer Science', 'every major in the Donald Bren School of ...', 'Dance (B.A.)', ..."""
    if rule.scope_type in UNIT_EXCLUSION_SCOPES:
        return f"every major in the {rule.name}"
    if rule.scope_type == "program_category":
        return rule.name
    qualifiers = [join_names(rule.degrees, "and")] if rule.degrees else []
    if rule.emphasis:
        qualifiers.append(f"{rule.emphasis} emphasis")
    return f"{rule.name} ({', '.join(qualifiers)})" if qualifiers else rule.name


# =====================================================================
# Minimum GPA: most specific rule wins (major exception > college/school > campus-wide)
# =====================================================================
CAMPUS_WIDE_SCOPES = ("campus", "all_majors")


def _threshold(rule: GpaRule) -> GpaThreshold:
    return GpaThreshold(minimum_gpa=rule.minimum_gpa, scope_type=rule.scope_type, scope_name=rule.scope_name)


def _range(thresholds: list[GpaThreshold]) -> str:
    values = sorted({t.minimum_gpa for t in thresholds if t.minimum_gpa is not None})
    if not values:
        return "not listed"
    return fmt_gpa(values[0]) if len(values) == 1 else f"{fmt_gpa(values[0])}–{fmt_gpa(values[-1])}"


def resolve_gpa_rule(campus: TagCampus, major: MajorProfile, term: EntryTerm | None = None) -> GpaResolution:
    """
    The minimum UC-transferable GPA for this major at this campus, by specificity:
      1. a named-major exception (overrides its college/school rule; e.g. UC Riverside Computer Science 3.6
         over BCOE's 3.0) — unless the student names a different college/school for the major;
      2. the college/school rule for the student's stated college/school;
      3. a campus-wide rule.
    Never the lowest or first value found: when the student's college/school decides the rule and isn't
    known, the result is 'ambiguous' with every candidate listed.
    """
    policy = campus.gpa
    deadline_term = TermRef.of(policy.deadline_term) if policy.deadline_term else None
    deadline = policy.deadline.label if policy.deadline else None
    if term is not None and policy.deadline_term is not None and term != policy.deadline_term:
        deadline = None                     # the printed deadline is for another entry term

    def result(status, explanation, *, rule=None, candidates=()):
        return GpaResolution(status=status, minimum_gpa=rule.minimum_gpa if rule and status == "resolved" else None,
                             rule=rule, candidates=list(candidates), deadline=deadline, deadline_term=deadline_term,
                             explanation=explanation, source_pages=list(policy.source_pages))

    if not policy.rules:
        return result("unresolved", f"The TAG matrix lists no minimum GPA for {campus.name}.")

    # 1. Named-major exceptions
    if major.name:
        hits = [(r, e) for r in policy.rules for e in r.major_exceptions
                if same_major(e.major, major.name)
                and (r.scope_name is None or major.in_unit([r.scope_name]) is not False)]
        if hits and policy.exceptions_override_parent:
            thresholds = [GpaThreshold(minimum_gpa=e.minimum_gpa, scope_type="major", scope_name=e.major,
                                       parent_scope_type=r.scope_type, parent_scope_name=r.scope_name) for r, e in hits]
            if len({t.minimum_gpa for t in thresholds}) == 1:
                t, (parent, _) = thresholds[0], hits[0]
                if t.minimum_gpa is None:
                    return result("unresolved", f"The matrix lists {major.name} separately at {campus.name} "
                                                f"but gives no minimum GPA for it.", candidates=thresholds)
                over = (f"the {fmt_gpa(parent.minimum_gpa)} minimum for the {parent.scope_name}" if parent.scope_name
                        else f"{campus.name}'s {fmt_gpa(parent.minimum_gpa)} campus-wide minimum")
                return result("resolved", f"{t.scope_name} has its own minimum, {fmt_gpa(t.minimum_gpa)}, which "
                                          f"overrides {over}.", rule=t)
            return result("ambiguous", f"{campus.name} lists different minimums for {major.name} in "
                                       f"{join_names(t.parent_scope_name or campus.name for t in thresholds)}; "
                                       f"which applies depends on where you'd enroll.", candidates=thresholds)

    units = [r for r in policy.rules if r.scope_type not in CAMPUS_WIDE_SCOPES]
    general = [r for r in policy.rules if r.scope_type in CAMPUS_WIDE_SCOPES]
    unit_kind = join_names(sorted({r.scope_type for r in units}), "or")

    # 2. The student's college/school
    if units and major.unit_names:
        matched = [r for r in units if major.in_unit([r.scope_name]) is True]
        maybe = [r for r in units if major.in_unit([r.scope_name]) is None]
        if not matched and maybe:
            return result("ambiguous", f"'{join_names(major.units)}' may match "
                                       f"{join_names((r.scope_name for r in maybe), 'or')}.",
                          candidates=[_threshold(r) for r in maybe])
        if len(matched) == 1:
            r = matched[0]
            if r.minimum_gpa is None:
                return result("unresolved", f"The matrix gives no minimum GPA for the {r.scope_name}.",
                              candidates=[_threshold(r)])
            return result("resolved", f"Minimum for majors in the {r.scope_name}.", rule=_threshold(r))
        if len(matched) > 1:
            return result("ambiguous", f"More than one {unit_kind} rule matches what you entered.",
                          candidates=[_threshold(r) for r in matched])
        if not general:
            return result("unresolved", f"{campus.name} sets the minimum by {unit_kind}, and "
                                        f"{join_names(major.units)} isn't one it lists "
                                        f"({join_names(r.scope_name for r in units)}).",
                          candidates=[_threshold(r) for r in units])

    # ... unknown: every unit rule (and any campus-wide rule it would override) could apply
    if units and not major.unit_names:
        candidates = [_threshold(r) for r in (*units, *general)]
        who = major.name or "your major"
        return result("ambiguous", f"{campus.name} sets the minimum by {unit_kind} ({_range(candidates)}), "
                                   f"and the planner doesn't know which {unit_kind} offers {who}.",
                      candidates=candidates)

    # 3. Campus-wide
    if len(general) == 1:
        r = general[0]
        if r.minimum_gpa is None:
            return result("unresolved", f"The matrix gives no campus-wide minimum GPA for {campus.name}.",
                          candidates=[_threshold(r)])
        return result("resolved", f"{campus.name}'s minimum applies to all majors.", rule=_threshold(r))
    if general:
        return result("ambiguous", f"{campus.name} lists more than one campus-wide minimum.",
                      candidates=[_threshold(r) for r in general])
    return result("unresolved", f"The planner can't match {campus.name}'s GPA rules to your major.",
                  candidates=[_threshold(r) for r in policy.rules])


def major_units(campus: TagCampus, major: MajorProfile) -> tuple[tuple[str, ...], bool]:
    """
    The major's college/school at this campus (all its names), and whether it was inferred: the student's own
    answer or the affiliation data first, otherwise the parent of a named-major GPA exception (the matrix lists
    UC Riverside Computer Science under BCOE). The inference is used for recommendations and major-prep
    applicability only, never to rule out an exclusion.
    """
    if major.units:
        return major.unit_names, False
    if not major.name:
        return (), False
    parents = [r.scope_name for r in campus.gpa.rules for e in r.major_exceptions
               if r.scope_name and same_major(e.major, major.name)]
    return tuple(dict.fromkeys(parents)), bool(parents)


# =====================================================================
# General education (TAG rules only; the planner's GE engine decides the actual courses)
# =====================================================================
@dataclass(frozen=True)
class GeRecommendationFinding:
    strength: str                     # 'highly recommended'
    unit: str                         # 'School of Business'
    applies: bool | None              # None: the major's college/school isn't known


@dataclass(frozen=True)
class GeFinding:
    required: bool | None             # None: a rule may apply but the planner can't tell
    patterns: tuple[str, ...]         # patterns that satisfy the requirement (or that TAG accepts)
    reason: str
    recommendation: GeRecommendationFinding | None


def ge_requirement(campus: TagCampus, major: MajorProfile) -> GeFinding:
    policy = campus.general_education
    units, _ = major_units(campus, major)
    required: bool | None = policy.completion_required_by_default
    patterns = policy.patterns
    reason = ("Completing a GE pattern isn't required for TAG at this campus"
              if required is False else "The matrix doesn't say whether GE completion is required")
    for exc in policy.required_exceptions:
        applies = (same_major(exc.name, major.name) if major.name else None) if exc.scope_type == "major" \
            else unit_match([exc.name], units)
        if applies and exc.completion_required:
            return GeFinding(True, exc.acceptable_patterns,
                             f"{campus.name} requires completing {join_names(exc.acceptable_patterns, 'or')} "
                             f"for {exc.name}", None)
        if applies is None and exc.completion_required and required is not True:
            required, reason = None, (f"{campus.name} requires completing {join_names(exc.acceptable_patterns, 'or')} "
                                      f"for {exc.name}, and the planner can't tell whether that includes your major")
    recommendation = None
    for rec in policy.recommendations:
        applies = unit_match(rec.names, units)
        if applies is not False:
            recommendation = GeRecommendationFinding((rec.strength or "recommended").replace("_", " "),
                                                     rec.names[0], applies)
            break
    return GeFinding(required, patterns, reason, recommendation)


def pattern_matches(student_pattern: str | None, patterns: Iterable[str]) -> bool | None:
    """'CAL-GETC' matches 'Cal-GETC'. None when the student's pattern isn't known."""
    if not student_pattern:
        return None
    return norm(student_pattern) in {norm(p) for p in patterns}


# =====================================================================
# Major preparation (the matrix names who needs it, rarely which courses)
# =====================================================================
@dataclass(frozen=True)
class MajorPrepFinding:
    applies: Literal["yes", "possible", "no"]
    scopes: tuple[MajorPrepScope, ...]       # the requirements that apply (or may apply)
    unit_inferred_from: str | None           # set when the major's college/school came from the GPA table
    courses: tuple | None                    # None: not enumerated in the matrix (never "no courses")
    separate_gpa: str | None                 # a separate major-prep GPA that applies or may apply
    urls: tuple[str, ...]


def _prep_scope_applies(scope: MajorPrepScope, major: MajorProfile, units: tuple[str, ...]) -> bool | None:
    kind = scope.scope_type
    if kind in ("all_majors_in_college", "all_majors_in_school"):
        return unit_match(scope.unit_names, units)
    if kind in ("named_majors_in_college", "major_groups_in_college"):
        if not major.name:
            return None
        if any(same_major(m, major.name) for m in scope.majors):
            return units == () or unit_match(scope.unit_names, units) is not False
        # A major can belong to a named group ('Biological Sciences') without sharing its name
        return False if kind == "named_majors_in_college" else None
    return None                              # 'selective_majors', 'specific_majors', ...: which majors isn't listed


def major_preparation_for(campus: TagCampus, major: MajorProfile) -> MajorPrepFinding:
    policy = campus.major_preparation
    units, inferred = major_units(campus, major)
    yes, maybe = [], []
    for scope in policy.required_for:
        applies = _prep_scope_applies(scope, major, units)
        if applies:
            yes.append(scope)
        elif applies is None:
            maybe.append(scope)
    applies = "yes" if yes else "possible" if maybe else "no"

    separate = None
    req_for = policy.separate_gpa_required_for
    if isinstance(req_for, tuple):          # named majors / groups
        if major.name and any(same_major(m, major.name) for m in req_for):
            separate = "a separate major-preparation GPA"
        elif not major.name or applies != "no":
            separate = f"a separate major-preparation GPA for {join_names(req_for)}"
    elif isinstance(req_for, str) and applies != "no":
        separate = f"a separate major-preparation GPA for {req_for}"
    if separate and policy.separate_gpa_thresholds is None:
        separate += " (thresholds not listed in the matrix" + (
            f"; {policy.separate_gpa_details.rstrip('.')}" if policy.separate_gpa_details else "") + ")"
    inferred_unit = join_names(units) if inferred and yes else None
    return MajorPrepFinding(applies, tuple(yes or maybe), inferred_unit, policy.individual_courses, separate,
                            policy.details_urls)


def describe_prep_scope(scope: MajorPrepScope) -> str:
    kind, units, majors = scope.scope_type, scope.unit_names, scope.majors
    if kind in ("all_majors_in_college", "all_majors_in_school"):
        return f"all majors in the {units[0]}"
    if kind == "named_majors_in_college":
        return f"{join_names(majors)} ({units[0]})" if units else join_names(majors)
    if kind == "major_groups_in_college":
        return f"the {join_names(majors)} major groups" + (f" ({units[0]})" if units else "")
    return kind.replace("_", " ")          # 'selective majors', 'specific majors', 'all screening majors'


# =====================================================================
# Timeline
# =====================================================================
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month_day(md: str) -> str:
    month, day = (int(x) for x in md.split("-"))
    return f"{_MONTHS[month - 1]} {day}"


def filing_window(campus: TagCampus, term: EntryTerm) -> FilingWindow | None:
    return next((w for w in campus.filing_windows if w.term == term), None)


def format_window(w: FilingWindow) -> str:
    """'Sep 1–30, 2026', 'May 1–31, 2027 (year inferred)'."""
    if w.start and w.end:
        if (w.start.year, w.start.month) == (w.end.year, w.end.month):
            text = f"{_MONTHS[w.start.month - 1]} {w.start.day}–{w.end.day}, {w.start.year}"
        else:
            text = f"{_MONTHS[w.start.month - 1]} {w.start.day}, {w.start.year} – {_MONTHS[w.end.month - 1]} {w.end.day}, {w.end.year}"
        return f"{text} (year inferred)" if w.year_inferred else text
    return f"{_month_day(w.start_month_day)} – {_month_day(w.end_month_day)} (year not listed)"


def decision_release(campus: TagCampus, term: EntryTerm) -> DecisionRelease | None:
    pre = campus.pre_evaluation
    return next((d for d in pre.decision_releases if d.term == term), None) if pre else None


def format_release(d: DecisionRelease) -> str:
    base = _month_day(d.month_day)
    if d.year is None:
        return f"{base} (year not listed)"
    return f"{base}, {d.year}" + (" (year inferred)" if d.year_inferred else "")
