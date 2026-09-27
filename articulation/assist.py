"""
Articulation agreements extracted from ASSIST PDFs (data/articulation/*.json, schema 1.x with requirement
trees), recognized by content: an `agreement` block plus `requirements` carrying `articulation_status` and
`requirement_groups` carrying an `operator`.

    requirement   one receiving (university) course or bundle, and the college course expression articulated
                  to it: COURSE | AND | OR (nested), or no_course_articulated
    group         AND (all) | OR (one child) | MINIMUM (enough children to reach N units and M items)

The planner needs concrete course sets, so the tree is expanded into ALTERNATIVES, each a complete, valid way
to meet every requirement:
    - an OR of single courses stays ONE choice ("take one of CSCI 15 / CSCI 19A"), which the scheduler's
      Dijkstra step resolves;
    - an OR of bundles or of groups, and a MINIMUM, become separate alternatives (never merged into an AND);
    - no_course_articulated is never planned at the college: it is carried as remaining_after_transfer.
The planner compares alternatives and schedules the cheapest; the others stay valid options.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from sep_engine.models import Course, GroupCategory, RequirementGroup, TransferAgreement

from datastores.common import code_key, id_to_key
from datastores.prerequisites import PrereqRecord, record_from

from .models import Pathway

MAX_ALTERNATIVES = 5000


class UnsupportedAssist(Exception):
    """A well-formed agreement whose structure can't be expanded safely (the message says why)."""


def is_assist_dataset(raw: Any) -> bool:
    return (isinstance(raw, dict) and isinstance(raw.get("agreement"), dict)
            and isinstance(raw.get("requirements"), list) and isinstance(raw.get("requirement_groups"), list)
            and all(isinstance(r, dict) and "articulation_status" in r for r in raw["requirements"])
            and str(raw.get("schema_version", "")).startswith("1."))


# =====================================================================
# Parsed agreement
# =====================================================================
@dataclass(frozen=True)
class TargetCourse:
    code: str
    title: str | None
    units: float | None


@dataclass(frozen=True)
class SendingCourse:
    key: str                  # 'MTH8'
    code: str                 # 'MTH 8'
    title: str
    units: float | None
    cross_listed: tuple[str, ...]


@dataclass
class AssistRequirement:
    id: str
    targets: list[TargetCourse]
    status: str                        # articulated | no_course_articulated | ...
    expr: dict | None
    note: str | None
    label: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "target_courses": [t.__dict__ for t in self.targets], "label": self.label,
                "articulation_status": self.status, "source_note": self.note}


@dataclass
class AssistGroup:
    id: str
    title: str | None
    operator: str
    requirement_ids: list[str]
    group_ids: list[str]
    instruction: str | None
    constraints: dict


@dataclass
class AssistAgreement:
    file: str
    dataset_id: str
    schema_version: str
    source: dict
    academic_year: str
    from_id: str
    to_id: str
    institutions: dict[str, str]
    major: str
    degree: str | None
    concentration: str | None
    scope: str | None
    complete_degree: bool | None
    courses: dict[str, SendingCourse]                 # college course key -> course
    requirements: dict[str, AssistRequirement]
    groups: dict[str, AssistGroup]
    root: tuple[str, str]                             # ('group', id) or ('and', '')
    root_children: list[tuple[str, str]]
    prereq_records: dict[str, PrereqRecord] = field(default_factory=dict)
    policies: list[dict] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    alternatives: list["Alternative"] = field(default_factory=list)

    @property
    def uni_short(self) -> str:
        from .loader import short_name
        return short_name(self.institutions[self.to_id])

    def code(self, key: str) -> str:
        c = self.courses.get(key)
        return c.code if c else key

    def expr_text(self, expr: dict | None) -> str:
        if not expr:
            return "no course articulated"
        op = expr.get("operator")
        if op == "COURSE":
            return self.code(id_to_key(expr["course_id"]))
        parts = [self.expr_text(o) for o in expr.get("operands") or []]
        text = f" {'and' if op == 'AND' else 'or'} ".join(parts)
        return f"({text})" if len(parts) > 1 else text

    def cross_listed(self) -> dict[str, str]:
        """Every cross-listed code key -> the course key the agreement uses ('CSCI28' -> 'MTH8')."""
        return {code_key(x): c.key for c in self.courses.values() for x in c.cross_listed}


@dataclass(frozen=True)
class Alternative:
    required: frozenset[str] = frozenset()                                   # college course keys (all of them)
    choices: tuple[tuple[str, tuple[str, ...]], ...] = ()                   # (requirement id, ONE of these keys)
    remaining: tuple[str, ...] = ()                                          # requirement ids done after transfer
    path: tuple[str, ...] = ()                                               # which OR/MINIMUM branches were taken

    def merge(self, other: Alternative) -> Alternative:
        choices = dict(self.choices)
        for rid, opts in other.choices:
            choices.setdefault(rid, opts)
        return Alternative(self.required | other.required, tuple(choices.items()),
                           tuple(dict.fromkeys(self.remaining + other.remaining)), self.path + other.path)

    def signature(self) -> tuple:
        return (self.required, tuple(sorted(self.choices)), tuple(sorted(self.remaining)))


# =====================================================================
# Parsing
# =====================================================================
def parse_assist(raw: dict, file: str) -> AssistAgreement:
    ag = raw["agreement"]
    for k in ("from_institution_id", "to_institution_id", "academic_year", "major"):
        if not ag.get(k):
            raise UnsupportedAssist(f"agreement.{k} is missing")
    insts = {i["id"]: i.get("name") or i["id"] for i in raw.get("institutions") or [] if i.get("id")}
    for end in ("from_institution_id", "to_institution_id"):
        if ag[end] not in insts:
            raise UnsupportedAssist(f"agreement.{end} {ag[end]!r} isn't in institutions")
    frm, to = ag["from_institution_id"], ag["to_institution_id"]
    all_courses = {c["id"]: c for c in raw.get("courses") or [] if c.get("id")}
    sending = {}
    for c in all_courses.values():
        if c.get("institution_id") == frm:
            key = id_to_key(c["id"])
            sending[key] = SendingCourse(key, c.get("code") or key, c.get("title") or c.get("code") or key,
                                         c.get("units"), tuple(c.get("cross_listed_codes") or ()))

    def target_of(r: dict) -> list[TargetCourse]:
        expr = r.get("target_requirement") or ({"operator": "COURSE", "course_id": r["target_course_id"]}
                                               if r.get("target_course_id") else None)
        ids = _leaf_ids(expr) if expr else []
        out = []
        for cid in ids:
            c = all_courses.get(cid)
            out.append(TargetCourse(c["code"], c.get("title"), c.get("units")) if c else
                       TargetCourse(cid.split(":", 1)[-1], None, None))
        return out

    reqs: dict[str, AssistRequirement] = {}
    for r in raw["requirements"]:
        expr = r.get("source_requirement")
        for cid in _leaf_ids(expr) if expr else []:
            if id_to_key(cid) not in sending:
                raise UnsupportedAssist(f"requirement {r['id']} names {cid}, which has no course record in the file")
        reqs[r["id"]] = AssistRequirement(r["id"], target_of(r), r.get("articulation_status") or "unknown", expr,
                                          r.get("source_note"))
    groups: dict[str, AssistGroup] = {}
    for g in raw["requirement_groups"]:
        op = str(g.get("operator") or "").upper()
        if op not in ("AND", "OR", "MINIMUM"):
            raise UnsupportedAssist(f"group {g.get('id')!r} uses operator {g.get('operator')!r}, which the planner "
                                    f"doesn't read")
        groups[g["id"]] = AssistGroup(g["id"], g.get("title"), op, list(g.get("requirement_ids") or []),
                                      list(g.get("group_ids") or []), g.get("source_instruction"),
                                      dict(g.get("selection_constraints") or {}))
    for g in groups.values():
        for rid in g.requirement_ids:
            if rid not in reqs:
                raise UnsupportedAssist(f"group {g.id!r} names unknown requirement {rid!r}")
        for gid in g.group_ids:
            if gid not in groups:
                raise UnsupportedAssist(f"group {g.id!r} names unknown group {gid!r}")
    root_id = ag.get("root_requirement_group_id")
    if root_id and root_id not in groups:
        raise UnsupportedAssist(f"root_requirement_group_id {root_id!r} isn't a group")
    if root_id:
        root_children = [("group", root_id)]
    else:
        nested = {gid for g in groups.values() for gid in g.group_ids}
        grouped = {rid for g in groups.values() for rid in g.requirement_ids}
        root_children = [("group", gid) for gid in groups if gid not in nested]
    grouped_all = {rid for g in groups.values() for rid in g.requirement_ids}
    root_children += [("req", rid) for rid in reqs if rid not in grouped_all]

    prd = raw.get("prerequisite_data") or {}
    records = {}
    for cid, rec in (prd.get("records") or {}).items():
        if isinstance(rec, dict):
            pr = record_from(cid, rec, f"agreement:{file}")
            records[pr.key] = pr
    agreement = AssistAgreement(
        file=file, dataset_id=raw.get("dataset_id") or file, schema_version=str(raw.get("schema_version")),
        source=raw.get("source") or {}, academic_year=ag["academic_year"], from_id=frm, to_id=to, institutions=insts,
        major=ag["major"], degree=ag.get("degree"), concentration=ag.get("concentration"),
        scope=ag.get("extracted_requirement_scope"), complete_degree=ag.get("complete_degree_requirements_in_source"),
        courses=sending, requirements=reqs, groups=groups, root=("and", ""), root_children=root_children,
        prereq_records=records, policies=list(raw.get("transfer_policies") or []),
        recommendations=list(raw.get("recommendations") or []))
    uni = agreement.uni_short
    for r in reqs.values():
        codes = " + ".join(t.code for t in r.targets) or r.id
        titles = " + ".join(t.title for t in r.targets if t.title)
        r.label = f"{uni} {codes}" + (f" · {titles}" if titles else "")
    agreement.alternatives = expand(agreement)
    return agreement


def _leaf_ids(expr: dict | None) -> list[str]:
    if not expr:
        return []
    if expr.get("operator") == "COURSE":
        return [expr["course_id"]]
    return [cid for o in expr.get("operands") or [] for cid in _leaf_ids(o)]


# =====================================================================
# Expansion into alternatives
# =====================================================================
def _dnf(expr: dict) -> list[frozenset[str]]:
    """Course sets that each satisfy the expression; supersets of another set are dropped (OR(A, A and B) = A)."""
    op = expr.get("operator")
    if op == "COURSE":
        return [frozenset({id_to_key(expr["course_id"])})]
    parts = [_dnf(o) for o in expr.get("operands") or []]
    if op == "AND":
        sets = [frozenset().union(*combo) for combo in itertools.product(*parts)] if parts else [frozenset()]
    elif op == "OR":
        sets = [s for p in parts for s in p]
    else:
        raise UnsupportedAssist(f"course expression operator {op!r} isn't supported")
    sets = list(dict.fromkeys(sets))
    return [s for s in sets if not any(o < s for o in sets)]


def _product(lists: list[list[Alternative]]) -> list[Alternative]:
    out = [Alternative()]
    for options in lists:
        if len(out) * len(options) > MAX_ALTERNATIVES:
            raise UnsupportedAssist(f"the requirement tree has more than {MAX_ALTERNATIVES} combinations")
        out = [a.merge(b) for a in out for b in options]
    return _dedupe(out)


def _dedupe(alts: list[Alternative]) -> list[Alternative]:
    seen, out = set(), []
    for a in alts:
        if a.signature() not in seen:
            seen.add(a.signature())
            out.append(a)
    return out


def expand(ag: AssistAgreement) -> list[Alternative]:
    def req_alts(rid: str) -> list[Alternative]:
        r = ag.requirements[rid]
        if r.status != "articulated" or not r.expr:
            return [Alternative(remaining=(rid,))]
        sets = _dnf(r.expr)
        if len(sets) == 1:
            return [Alternative(required=sets[0])]
        if all(len(s) == 1 for s in sets):
            return [Alternative(choices=((rid, tuple(next(iter(s)) for s in sets)),))]
        return [Alternative(required=s, path=(f"{r.label}: {' + '.join(ag.code(k) for k in sorted(s))}",))
                for s in sets]

    def label_of(kind: str, xid: str) -> str:
        if kind == "req":
            return ag.requirements[xid].label
        g = ag.groups[xid]
        return g.title or g.instruction or xid

    def node_alts(kind: str, xid: str, stack: tuple = ()) -> list[Alternative]:
        if kind == "req":
            return req_alts(xid)
        if xid in stack:
            raise UnsupportedAssist(f"group {xid!r} contains itself")
        g = ag.groups[xid]
        children = [("req", r) for r in g.requirement_ids] + [("group", c) for c in g.group_ids]
        if g.operator == "AND":
            return _product([node_alts(k, i, stack + (xid,)) for k, i in children])
        if g.operator == "OR":
            if not children:
                raise UnsupportedAssist(f"OR group {xid!r} has no inputs")
            name = g.instruction or g.title or xid
            return _dedupe([Alternative(a.required, a.choices, a.remaining, (f"{name}: {label_of(k, i)}",) + a.path)
                            for k, i in children for a in node_alts(k, i, stack + (xid,))])
        return minimum_alts(g, children, stack + (xid,))

    def minimum_alts(g: AssistGroup, children: list[tuple[str, str]], stack: tuple) -> list[Alternative]:
        if any(k != "req" for k, _ in children):
            raise UnsupportedAssist(f"MINIMUM group {g.id!r} contains groups, which the planner can't count")
        min_units = float(g.constraints.get("minimum_units") or 0)
        min_items = int(g.constraints.get("minimum_items") or 0)
        if not (min_units or min_items):
            raise UnsupportedAssist(f"MINIMUM group {g.id!r} has no minimum_units or minimum_items")

        def units(rids: tuple[str, ...]) -> float:
            seen: dict[str, float] = {}
            for rid in rids:
                for t in ag.requirements[rid].targets:
                    if t.units is None:
                        raise UnsupportedAssist(f"{ag.requirements[rid].label} has no receiving units, so "
                                                f"\"{g.instruction or g.title}\" can't be counted")
                    seen[t.code] = t.units
            return sum(seen.values())

        rids = [i for _, i in children]
        found: list[tuple[str, ...]] = []
        for k in range(1, len(rids) + 1):
            for combo in itertools.combinations(rids, k):
                if any(set(f) <= set(combo) for f in found):
                    continue
                if len(combo) >= min_items and units(combo) >= min_units:
                    found.append(combo)
                    if len(found) > MAX_ALTERNATIVES:
                        raise UnsupportedAssist(f"\"{g.instruction or g.title}\" has too many combinations")
        if not found:
            raise UnsupportedAssist(f"no combination of \"{g.instruction or g.title}\" reaches its minimum")
        name = g.instruction or g.title or g.id
        out = []
        for combo in found:
            path = (f"{name}: {'; '.join(ag.requirements[r].label for r in combo)}",)
            for a in _product([req_alts(r) for r in combo]):
                out.append(Alternative(a.required, a.choices, a.remaining, path + a.path))
        return _dedupe(out)

    alts = _product([node_alts(k, i) for k, i in ag.root_children])
    if not alts:
        raise UnsupportedAssist("the requirement tree has no satisfiable combination")
    return alts


# =====================================================================
# Pathway (for the store listing; the planner builds per-request plans from `assist`)
# =====================================================================
def to_engine(ag: AssistAgreement, alt: Alternative) -> tuple[list[str], list[RequirementGroup]]:
    required = [ag.code(k) for k in sorted(alt.required, key=lambda k: ag.code(k))]
    groups = [RequirementGroup(id=rid, name=f"{ag.requirements[rid].label} (choose one)", category=GroupCategory.MAJOR,
                               choose=1, options=[ag.code(k) for k in opts]) for rid, opts in alt.choices]
    return required, groups


def build_assist_pathway(ag: AssistAgreement) -> Pathway:
    from .loader import UnsupportedAgreement

    college, university = ag.institutions[ag.from_id], ag.institutions[ag.to_id]
    missing_units = sorted(c.code for c in ag.courses.values() if c.units is None)
    used = {k for a in ag.alternatives for k in (*a.required, *(o for _, opts in a.choices for o in opts))}
    missing_units = [c for c in missing_units if code_key(c) in used]
    if missing_units:
        raise UnsupportedAgreement(f"no units are stated for {', '.join(missing_units)}")
    first = ag.alternatives[0]
    required, groups = to_engine(ag, first)
    if not required and not groups:
        if all(a.remaining and not a.required and not a.choices for a in ag.alternatives):
            raise UnsupportedAgreement(f"it articulates no {college} course")
    catalog = {c.code: Course(code=c.code, title=c.title, units=c.units) for c in ag.courses.values() if c.units}
    degree = f"{ag.major}, {ag.degree}" if ag.degree else None
    agreement = TransferAgreement(college=college, university=university, major=ag.major, degree=degree,
                                  required_courses=required, requirement_groups=groups)
    not_articulated = [r.label + (f" ({r.note})" if r.note else "") for r in ag.requirements.values()
                       if r.status != "articulated"]
    notes = []
    for p in ag.policies:
        if p.get("title"):
            notes.append(f"Transfer policy ({p.get('scope') or university}): {p['title'].capitalize()}.")
    p = Pathway(college=college, university=university, college_ref=ag.from_id, university_ref=ag.to_id,
                major=ag.major, degree=ag.degree, academic_year=ag.academic_year, ge_pattern=None, agreement=agreement,
                catalog=catalog, source="assist", dataset_id=ag.dataset_id, file=ag.file,
                publisher=ag.source.get("publisher"), source_url=ag.source.get("url"),
                retrieved_on=ag.source.get("date_published"), not_articulated=not_articulated,
                prerequisites_enforced=True, notes=notes)
    p.assist = ag
    return p
