"""
Articulation dataset -> planner pathway (sep_engine's Course + TransferAgreement).

Each requirement's articulation maps onto what the engine can express:

    course                          -> a required course
    all_of                          -> required courses (all of them)
    one_of, single-course options   -> RequirementGroup(choose=1): take ONE
    requirement_groups: choose k of
      requirements met by one course -> RequirementGroup(choose=k)
    none                            -> left out (nothing to take at the college) and reported with every plan

Anything else is refused, never flattened: choosing between course bundles, a group whose requirements
need several courses, an `other` structure, a course without units, a course the file doesn't describe,
or a prerequisite cycle all make the pathway unsupported, with the reason. Prerequisites come from the
courses' own prereqs/coreqs (the engine's semantics); a file without any is planned in no prerequisite
order, with a warning. GE is not part of an articulation agreement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sep_engine.errors import CycleError
from sep_engine.models import Course, GroupCategory, RequirementGroup, TransferAgreement
from sep_engine.toposort import topological_sort

from .institutions import name_variants
from .models import Pathway
from .schema import DATASET_TYPE, ArticulationDataset, Requirement


class NotArticulationData(ValueError):
    """The file isn't an articulation dataset, or isn't a valid one (the message says what's wrong)."""


class UnsupportedAgreement(Exception):
    """A valid agreement that uses a structure (or lacks data) the planner can't plan safely."""


def read_dataset(path: Path, raw: Any = None) -> ArticulationDataset:
    """Parse and validate one file (or its already-parsed JSON). Raises NotArticulationData with a readable reason."""
    if raw is None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise NotArticulationData(f"not readable JSON ({e})") from None
    if not isinstance(raw, dict) or raw.get("dataset_type") != DATASET_TYPE:
        kind = raw.get("dataset_type") if isinstance(raw, dict) else type(raw).__name__
        raise NotArticulationData(f"not a {DATASET_TYPE} dataset (dataset_type is {kind!r}) and not an "
                                  f"ASSIST-extracted agreement")
    return validate_dataset(raw)


def validate_dataset(raw: dict[str, Any]) -> ArticulationDataset:
    try:
        return ArticulationDataset.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'file'}: {err['msg']}"
                             for err in e.errors()[:5])
        more = f" (+{e.error_count() - 5} more)" if e.error_count() > 5 else ""
        raise NotArticulationData(f"invalid articulation data: {problems}{more}") from None


def short_name(name: str) -> str:
    """'California State University, East Bay' -> 'CSUEB'; no acronym -> the name itself."""
    acronyms = sorted((v for v in name_variants(name) if " " not in v and len(name.split()) > 1), key=len)
    return acronyms[0].upper() if acronyms else name


def build_pathway(ds: ArticulationDataset, file: str) -> Pathway:
    """The planner pathway for one dataset. Raises UnsupportedAgreement when it can't be planned safely."""
    ag = ds.agreement
    names = {i.id: i.name for i in ds.institutions}
    college, university = names[ag.from_institution_id], names[ag.to_institution_id]
    uni = short_name(university)
    reqs = {r.id: r for r in ds.requirements}
    grouped = {rid for g in ds.requirement_groups for rid in g.requirement_ids}

    def label(r: Requirement) -> str:
        codes = " + ".join(c.code for c in r.receiving)
        titles = " + ".join(c.title for c in r.receiving if c.title)
        return f"{uni} {codes}" + (f" · {titles}" if titles else "")

    required: list[str] = []
    groups: list[RequirementGroup] = []
    not_articulated: list[str] = []
    for r in ds.requirements:
        if r.id in grouped:
            continue
        a = r.articulation
        if a.type == "course":
            required.append(a.course)
        elif a.type == "all_of":
            required.extend(a.courses)
        elif a.type == "one_of":
            bundles = [" + ".join(o) for o in a.options if len(o) > 1]
            if bundles:
                raise UnsupportedAgreement(
                    f"{label(r)} can be met by different course bundles ({' or '.join(' + '.join(o) for o in a.options)}), "
                    f"and the planner can only choose between single courses")
            groups.append(RequirementGroup(id=r.id, name=f"{label(r)} (choose one)", category=GroupCategory.MAJOR,
                                           choose=1, options=[o[0] for o in a.options]))
        elif a.type == "none":
            not_articulated.append(label(r) + (f" ({a.reason})" if a.reason else ""))
        else:
            raise UnsupportedAgreement(f"{label(r)} uses a structure the planner can't read: {a.description}")
    for g in ds.requirement_groups:
        members = [reqs[rid] for rid in g.requirement_ids]
        complex_ = [label(m) for m in members if m.articulation.type != "course"]
        if complex_:
            raise UnsupportedAgreement(
                f"\"{g.title}\" asks to complete {g.choose} of {len(members)} requirements, and "
                f"{'; '.join(complex_)} isn't met by a single course, which the planner can't choose between")
        groups.append(RequirementGroup(id=g.id, name=g.title, category=GroupCategory.MAJOR, choose=g.choose,
                                       options=[m.articulation.course for m in members]))
    required = list(dict.fromkeys(required))
    named = [*required, *(o for grp in groups for o in grp.options)]
    if not named:
        raise UnsupportedAgreement(f"it articulates no {college} course")

    # Every course the plan could need (named courses and their prerequisites) must be described, with units
    records = {c.code: c for c in ds.courses}
    needed, stack = [], list(dict.fromkeys(named))
    while stack:
        code = stack.pop()
        if code in needed:
            continue
        rec = records.get(code)
        if rec is None:
            by = next((c.code for c in ds.courses if code in (*c.prereqs, *c.coreqs)), None)
            raise UnsupportedAgreement(f"{code} " + (f"(a prerequisite of {by}) " if by else "")
                                       + "has no course record (title and units) in the file")
        if rec.units is None:
            raise UnsupportedAgreement(f"no units are stated for {code}")
        needed.append(code)
        stack.extend((*rec.prereqs, *rec.coreqs))
    try:
        catalog = {c.code: Course(code=c.code, title=c.title, units=c.units, prereqs=c.prereqs, coreqs=c.coreqs)
                   for c in ds.courses if c.units is not None}
    except ValueError as e:                                  # e.g. a course listed as its own prerequisite
        raise UnsupportedAgreement(f"a course record can't be used ({e})") from None
    try:
        topological_sort(catalog, needed, ())
    except CycleError as e:
        raise UnsupportedAgreement(str(e).rstrip(".")) from None
    agreement = TransferAgreement(college=college, university=university, major=ag.major,
                                  degree=f"{ag.major}, {ag.degree}" if ag.degree else None,
                                  required_courses=required, requirement_groups=groups)

    prereqs = any(c.prereqs or c.coreqs for c in ds.courses)
    notes = []
    if not prereqs:
        notes.append(f"{file} lists no prerequisites, so this plan doesn't order courses by prerequisite. "
                     f"Check each course's prerequisites in the {college} catalog.")
    if not_articulated:
        notes.append(f"{len(not_articulated)} {uni} requirement{'s have' if len(not_articulated) > 1 else ' has'} "
                     f"no {college} course articulated, so the plan can't include "
                     f"{'them' if len(not_articulated) > 1 else 'it'}: {'; '.join(not_articulated)}.")
    return Pathway(college=college, university=university, college_ref=ag.from_institution_id,
                   university_ref=ag.to_institution_id, major=ag.major, degree=ag.degree,
                   academic_year=ag.academic_year, ge_pattern=None, agreement=agreement, catalog=catalog,
                   source="assist", dataset_id=ds.dataset_id, file=file, publisher=ds.source.publisher,
                   source_url=ds.source.url, retrieved_on=ds.source.retrieved_on, not_articulated=not_articulated,
                   prerequisites_enforced=prereqs, notes=notes)
