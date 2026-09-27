"""
Sending-college prerequisite registry (data/prerequisites/*.json) and the resolver that turns its rules into
scheduling constraints WITHOUT guessing.

Registry semantics (kept as published):
    COURSE + before                 -> the course in an earlier term          (engine prereq)
    COURSE + before_or_concurrent   -> earlier or the same term               (engine coreq)
    COURSE + same_term              -> enrolled together                      (engine coreq, reported)
    AND                             -> every item
    OR                              -> ONE item, never all of them
    CONDITION                       -> external confirmation (placement ...): never assumed satisfied
    requirement null / no record    -> unresolved: needs review, no edge invented

How an OR is resolved for one plan (first rule that applies):
    1. an alternative already satisfied by completed courses          -> nothing to add
    2. an alternative made only of courses the plan already has       -> ordering edges only
    3. an alternative that is a CONDITION (placement, approval ...)   -> needs review; no course is added, because
                                                                         the student may qualify that way
    4. the course-only alternative with the fewest new units, if every course in it is researched
                                                                      -> add those courses (+ their prerequisites)
    5. otherwise                                                      -> needs review
A course the plan would add must exist in the catalog with units; otherwise the edge is dropped and reported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import DATA_DIR, LoadReport, code_key, id_to_key, read_json, split_code

log = logging.getLogger("uvicorn.error")
PREREQ_DIR = DATA_DIR / "prerequisites"

BEFORE, CONCURRENT, SAME_TERM = "before", "before_or_concurrent", "same_term"
RESOLVED_STATUSES = {"documented", "user_clarified", "policy_dependent"}


@dataclass(frozen=True)
class PrereqRecord:
    key: str                      # 'MTH1'
    code: str                     # 'MTH 1'
    status: str                   # documented | policy_dependent | user_clarified | not_researched | ...
    requirement: dict | None      # expression; None = unresolved
    summary: str | None
    advisory: tuple[str, ...]
    current_year_confirmed: bool | None
    source: str                   # 'registry:<file>' | 'agreement:<file>'
    verification_status: str | None = None

    @property
    def researched(self) -> bool:
        return self.status in RESOLVED_STATUSES and self.requirement is not None


@dataclass
class InstitutionPrereqs:
    institution_id: str
    file: str
    reviewed_on: str | None
    records: dict[str, PrereqRecord] = field(default_factory=dict)       # key -> record
    external: dict[str, dict] = field(default_factory=dict)             # key -> {status, note}
    alias_of: dict[str, frozenset[str]] = field(default_factory=dict)   # key -> equivalent keys (incl. itself)
    source_years: dict[str, str] = field(default_factory=dict)


class PrerequisiteStore:
    def __init__(self) -> None:
        self.institutions: dict[str, InstitutionPrereqs] = {}
        self.report = LoadReport()

    def load_directory(self, folder: Path = PREREQ_DIR) -> None:
        self.institutions, self.report = {}, LoadReport()
        for path in sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []:
            try:
                raw = read_json(path)
            except (OSError, ValueError) as e:
                self.report.skip(path, f"not readable JSON ({e})")
                continue
            if not (isinstance(raw, dict) and raw.get("institution_id") and isinstance(raw.get("courses"), dict)
                    and "semantics" in raw):
                self.report.skip(path, "not a prerequisite registry (needs institution_id, semantics, courses)")
                continue
            reg = InstitutionPrereqs(raw["institution_id"], path.name, raw.get("reviewed_on"))
            for cid, r in raw["courses"].items():
                rec = record_from(cid, r, f"registry:{path.name}")
                reg.records[rec.key] = rec
            for ext in raw.get("external_references") or []:
                reg.external[id_to_key(ext["course_id"])] = {"status": ext.get("status"), "note": ext.get("note")}
            for group in raw.get("aliases") or []:
                keys = frozenset(id_to_key(x) for x in group)
                for k in keys:
                    reg.alias_of[k] = keys
            reg.source_years = {k: v.get("catalog_year") for k, v in (raw.get("sources") or {}).items()
                                if isinstance(v, dict) and v.get("catalog_year")}
            self.institutions[reg.institution_id] = reg
            self.report.loaded.append({"file": path.name, "institution_id": reg.institution_id,
                                       "records": len(reg.records), "external_references": len(reg.external),
                                       "reviewed_on": reg.reviewed_on})

    def get(self, inst_id: str) -> InstitutionPrereqs | None:
        return self.institutions.get(inst_id)

    def status(self) -> list[dict]:
        return [{"institution_id": r.institution_id, "file": r.file, "records": len(r.records),
                 "unresearched_references": len(r.external), "reviewed_on": r.reviewed_on, "status": "loaded"}
                for r in self.institutions.values()]


def record_from(course_id: str, r: dict, source: str) -> PrereqRecord:
    return PrereqRecord(
        key=id_to_key(r.get("course_id") or course_id), code=r.get("code") or split_code(id_to_key(course_id)),
        status=r.get("status") or "unknown", requirement=r.get("requirement"), summary=r.get("summary"),
        advisory=tuple(r.get("advisory") or ()), current_year_confirmed=r.get("current_year_confirmed"),
        source=source, verification_status=r.get("verification_status"))


# =====================================================================
# Resolver
# =====================================================================
@dataclass
class ReviewItem:
    course: str
    kind: str          # condition | condition_route | unresolved | not_loaded | unschedulable | same_term |
                       # clarification | policy
    message: str
    detail: str | None = None       # the rule in words, e.g. "(MTH 55 or MTH 55B or approved equivalent)"


@dataclass
class ResolvedPrereqs:
    prereqs: dict[str, list[str]] = field(default_factory=dict)     # code -> codes in an earlier term
    coreqs: dict[str, list[str]] = field(default_factory=dict)      # code -> codes same term or earlier
    added: dict[str, str] = field(default_factory=dict)             # supporting course code -> course that needs it
    reviews: list[ReviewItem] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)           # code -> where its rule came from
    statuses: dict[str, str] = field(default_factory=dict)          # code -> record status / 'not_loaded'
    unconfirmed_year: list[str] = field(default_factory=list)       # documented, not confirmed for the current year


class PrereqResolver:
    """
    Resolve the prerequisites of a set of courses for one plan.

    records(key)    -> PrereqRecord | None     (registry first, then the agreement's embedded record)
    units_of(code)  -> (display code, title, units) | None    (catalog/agreement; None = can't be scheduled)
    completed       -> course keys already done (completed + legitimately waived)
    """

    def __init__(self, records: Callable[[str], PrereqRecord | None], units_of: Callable[[str], tuple | None],
                 completed: Iterable[str], aliases: dict[str, frozenset[str]] | None = None,
                 external: dict[str, dict] | None = None) -> None:
        self.records = records
        self.units_of = units_of
        self.completed = {code_key(c) for c in completed}
        self.aliases = aliases or {}
        self.external = external or {}

    # --- helpers -------------------------------------------------------------------------------
    def _done(self, key: str) -> bool:
        return any(k in self.completed for k in self.aliases.get(key, frozenset({key})))

    def _display(self, key: str) -> str:
        info = self.units_of(key)
        return info[0] if info else split_code(key)

    def _describe(self, expr: dict) -> str:
        t = expr.get("type")
        if t == "COURSE":
            timing = {CONCURRENT: " (may be concurrent)", SAME_TERM: " (same term)"}.get(expr.get("timing"), "")
            return self._display(id_to_key(expr["course_id"])) + timing
        if t == "CONDITION":
            return expr.get("description") or "a condition"
        if t in ("AND", "OR"):
            parts = [self._describe(i) for i in expr.get("items") or []]
            joined = f" {t.lower()} ".join(parts)
            return f"({joined})" if len(parts) > 1 else joined
        return "no prerequisite" if t == "NONE" else str(t)

    @staticmethod
    def _flatten_or(expr: dict) -> list[dict]:
        out = []
        for item in expr.get("items") or []:
            out.extend(PrereqResolver._flatten_or(item) if item.get("type") == "OR" else [item])
        return out

    @staticmethod
    def _has_condition(expr: dict) -> bool:
        if expr.get("type") == "CONDITION":
            return True
        return any(PrereqResolver._has_condition(i) for i in expr.get("items") or [])

    @staticmethod
    def _courses(expr: dict) -> list[dict]:
        """COURSE leaves of a course-only AND/COURSE expression."""
        if expr.get("type") == "COURSE":
            return [expr]
        return [leaf for i in expr.get("items") or [] for leaf in PrereqResolver._courses(i)]

    def _satisfied_by_completed(self, expr: dict) -> bool:
        t = expr.get("type")
        if t == "COURSE":
            return self._done(id_to_key(expr["course_id"]))
        if t == "AND":
            return all(self._satisfied_by_completed(i) for i in expr.get("items") or [])
        if t == "OR":
            return any(self._satisfied_by_completed(i) for i in expr.get("items") or [])
        return t == "NONE"

    def _researched(self, key: str) -> bool:
        rec = self.records(key)
        return rec is not None and rec.researched

    # --- resolution ------------------------------------------------------------------------------
    def resolve(self, seeds: Iterable[str], anchor: Iterable[str] = ()) -> ResolvedPrereqs:
        """seeds: every course the plan may take (required + choice options). anchor: courses certain to be in
        the plan (required ones). Iterates until the courses it adds stop changing the OR decisions."""
        seed_keys = [code_key(s) for s in seeds]
        base_anchor = {code_key(a) for a in anchor}
        added: dict[str, str] = {}
        for _ in range(6):
            out = self._pass(seed_keys, base_anchor | {code_key(a) for a in added}, base_anchor)
            if set(out.added) == set(added):
                return out
            added = out.added
        return out

    def _pass(self, seeds: list[str], anchor: set[str], base: set[str]) -> ResolvedPrereqs:
        out = ResolvedPrereqs()
        queue, seen = list(dict.fromkeys(seeds)), set()
        while queue:
            key = queue.pop(0)
            if key in seen or self._done(key):
                continue
            seen.add(key)
            code = self._display(key)
            rec = self.records(key)
            out.prereqs.setdefault(code, [])
            out.coreqs.setdefault(code, [])
            if rec is None:
                ext = self.external.get(key)
                out.statuses[code] = (ext or {}).get("status") or "not_loaded"
                out.reviews.append(ReviewItem(code, "not_loaded", (
                    f"{code}: prerequisites not researched in the loaded data"
                    + (f" ({ext['note']})" if ext and ext.get("note") else "") + "; check the catalog.")))
                continue
            out.sources[code] = rec.source
            out.statuses[code] = rec.status
            if rec.requirement is None:
                out.reviews.append(ReviewItem(code, "unresolved", f"{code}: prerequisite rule is unresolved in the "
                                                                   f"source data; check with a counselor."))
                continue
            if rec.status == "policy_dependent":
                out.reviews.append(ReviewItem(code, "policy", f"{code}: {rec.summary or self._describe(rec.requirement)} "
                                                              f"(depends on college policy/placement)."))
            if rec.status == "user_clarified" or rec.verification_status:
                out.reviews.append(ReviewItem(code, "clarification", (
                    f"{code}: the prerequisite grouping comes from a user-supplied clarification that isn't "
                    f"independently verified.")))
            if rec.current_year_confirmed is False and rec.status == "documented":
                out.unconfirmed_year.append(code)
            for dep, timing in self._expr(code, rec.requirement, anchor, out):
                dep_code = self._display(dep)
                if dep_code == code:
                    continue
                if timing == BEFORE:
                    _append(out.prereqs[code], dep_code)
                else:
                    _append(out.coreqs[code], dep_code)
                    if timing == SAME_TERM:
                        out.reviews.append(ReviewItem(code, "same_term", (
                            f"{code} and {dep_code} must be taken in the same term; the plan allows {dep_code} in "
                            f"the same or an earlier term.")))
                if dep not in base and dep not in seeds:
                    out.added.setdefault(dep_code, code)
                queue.append(dep)
        return out

    def _expr(self, code: str, expr: dict, anchor: set[str], out: ResolvedPrereqs) -> list[tuple[str, str]]:
        """Edges (course key, timing) this expression puts on `code` for this plan; reviews go to `out`."""
        t = expr.get("type")
        if t == "NONE":
            return []
        if t == "CONDITION":
            out.reviews.append(ReviewItem(code, "condition", f"{code}: requires {expr.get('description') or 'a condition'} "
                                                             f"(needs confirmation; not assumed).",
                                          expr.get("description")))
            return []
        if t == "COURSE":
            key = id_to_key(expr["course_id"])
            if self._done(key):
                return []
            if self.units_of(key) is None:
                out.reviews.append(ReviewItem(code, "unschedulable", (
                    f"{code} requires {split_code(key)}, which has no catalog record with units, so the plan "
                    f"can't include it; check with a counselor.")))
                return []
            return [(key, expr.get("timing") or BEFORE)]
        if t == "AND":
            return [e for item in expr.get("items") or [] for e in self._expr(code, item, anchor, out)]
        if t == "OR":
            return self._or(code, expr, anchor, out)
        out.reviews.append(ReviewItem(code, "unresolved", f"{code}: unknown prerequisite rule type {t!r}."))
        return []

    def _or(self, code: str, expr: dict, anchor: set[str], out: ResolvedPrereqs) -> list[tuple[str, str]]:
        branches = self._flatten_or(expr)
        # 1. already satisfied by completed courses
        if any(self._satisfied_by_completed(b) for b in branches):
            return []
        pure = [b for b in branches if not self._has_condition(b) and b.get("type") in ("COURSE", "AND")]
        # 2. a branch made only of courses already in the plan (or completed)
        for b in pure:
            leaves = self._courses(b)
            if all(id_to_key(l["course_id"]) in anchor or self._done(id_to_key(l["course_id"])) for l in leaves):
                return [e for e in self._expr(code, b, anchor, out)]
        # 3. a condition route exists (placement, approval): the student may qualify without a course
        if any(self._has_condition(b) for b in branches):
            out.reviews.append(ReviewItem(code, "condition_route", (
                f"{code}: prerequisite is one of {self._describe(expr)}. Which route applies depends on "
                f"placement or approval, so no prerequisite course was added; confirm with a counselor."),
                self._describe(expr)))
            return []
        # 4. cheapest course-only branch whose courses are all researched and schedulable
        candidates = []
        for i, b in enumerate(pure):
            keys = [id_to_key(l["course_id"]) for l in self._courses(b)]
            new = [k for k in keys if k not in anchor and not self._done(k)]
            if all(self.units_of(k) is not None and self._researched(k) for k in new):
                candidates.append((sum(self.units_of(k)[2] or 0 for k in new), i, b))
        if candidates:
            _, _, best = min(candidates, key=lambda c: (c[0], c[1]))
            return self._expr(code, best, anchor, out)
        out.reviews.append(ReviewItem(code, "unresolved", (
            f"{code}: prerequisite is one of {self._describe(expr)}, and none of these routes can be planned from "
            f"the loaded data (courses not researched or not in the catalog); confirm with a counselor.")))
        return []


def _append(lst: list[str], value: str) -> None:
    if value not in lst:
        lst.append(value)
