"""
The pathway store: every articulation dataset in data/articulation/, plus mock pathways injected for demos.

    load_directory(folder)   read every *.json: valid + plannable -> available; valid but not plannable ->
                             unsupported (with the reason); unreadable / invalid -> skipped (with the reason)
    find(college, university, major, ge, start_term)
                             by canonical institution ids, major (degree separate) and the start term's
                             academic year: that year's agreement, else the latest earlier one (with a
                             warning), else none. Loaded data wins over mock data.
    listing()                GET /pathways

Mock pathways (source "mock") only exist after an explicit add_mock() and survive reloads.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sep_engine.models import Course, TransferAgreement, index_catalog
from tag_engine.selectors import degree_key, norm, split_degree

from .institutions import InstitutionRegistry
from .assist import UnsupportedAssist, build_assist_pathway, is_assist_dataset, parse_assist
from .loader import NotArticulationData, UnsupportedAgreement, build_pathway, read_dataset
from .models import Pathway, SkippedFile, UnsupportedPathway

log = logging.getLogger("uvicorn.error")
ARTICULATION_DIR = Path(__file__).resolve().parent.parent / "data" / "articulation"


def academic_year_of(term: str | None) -> str | None:
    """The academic year a term belongs to: 'Fall 2026' -> '2026-2027'; 'Spring 2027', 'Summer 2027' -> '2026-2027'."""
    m = re.fullmatch(r"\s*(fall|spring|summer)\s+(\d{4})\s*", term or "", re.IGNORECASE)
    if not m:
        return None
    start = int(m[2]) if m[1].lower() == "fall" else int(m[2]) - 1
    return f"{start}-{start + 1}"


@dataclass
class Match:
    pathway: Pathway
    academic_year: str | None                 # the start term's academic year
    warnings: list[str] = field(default_factory=list)


@dataclass
class Lookup:
    match: Match | None = None
    unsupported: UnsupportedPathway | None = None
    reason: str | None = None                 # why nothing matched, when there is more to say than "no data"


class PathwayStore:
    def __init__(self) -> None:
        self.registry = InstitutionRegistry()
        self.pathways: list[Pathway] = []
        self.unsupported: list[UnsupportedPathway] = []
        self.skipped: list[SkippedFile] = []
        self.folder: Path | None = None

    # --- loading ---------------------------------------------------------------------------------
    def load_directory(self, folder: Path | str = ARTICULATION_DIR) -> None:
        """(Re)load every articulation file in `folder`. Mock pathways are kept."""
        self.folder = Path(folder)
        registry, loaded, unsupported, skipped, seen = InstitutionRegistry(), [], [], [], {}
        files = sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []
        for path in files:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                skipped.append(SkippedFile(path.name, f"not readable JSON ({e})"))
                continue
            if is_assist_dataset(raw):
                self._load_assist(raw, path, registry, loaded, unsupported, skipped, seen)
                continue
            try:
                ds = read_dataset(path, raw)
            except NotArticulationData as e:
                skipped.append(SkippedFile(path.name, str(e)))
                continue
            if ds.dataset_id in seen:
                skipped.append(SkippedFile(path.name, f"same dataset_id as {seen[ds.dataset_id]} "
                                                      f"({ds.dataset_id}), which is already loaded"))
                continue
            seen[ds.dataset_id] = path.name
            for inst in ds.institutions:
                registry.register(inst.id, inst.name, inst.aliases)
            try:
                loaded.append(build_pathway(ds, path.name))
            except UnsupportedAgreement as e:
                ag, names = ds.agreement, {i.id: i.name for i in ds.institutions}
                unsupported.append(UnsupportedPathway(
                    ag.from_institution_id, ag.to_institution_id, names[ag.from_institution_id],
                    names[ag.to_institution_id], ag.major, ag.degree, ag.academic_year, ds.dataset_id, path.name, str(e)))
        self.registry, self.unsupported, self.skipped = registry, unsupported, skipped
        self.pathways = [p for p in self.pathways if p.source == "mock"] + loaded
        log.log(logging.WARNING if unsupported or skipped else logging.INFO,
                "Articulation data (%s): %d pathway(s) available, %d unsupported, %d file(s) skipped. "
                "Details: GET /pathways.", self.folder, len(loaded), len(unsupported), len(skipped))
        for s in skipped:
            log.info("Articulation file %s skipped: %s", s.file, s.reason)
        for u in unsupported:
            log.info("%s", u.message)

    @staticmethod
    def _load_assist(raw, path, registry, loaded, unsupported, skipped, seen) -> None:
        """An ASSIST-extracted agreement (requirement tree): available, unsupported (with the reason) or skipped."""
        ag_raw = raw["agreement"]
        dataset_id = raw.get("dataset_id") or path.stem
        if dataset_id in seen:
            skipped.append(SkippedFile(path.name, f"same dataset_id as {seen[dataset_id]} ({dataset_id}), "
                                                  f"which is already loaded"))
            return
        seen[dataset_id] = path.name
        names = {i["id"]: i.get("name") or i["id"] for i in raw.get("institutions") or [] if i.get("id")}
        for inst_id, name in names.items():
            registry.register(inst_id, name)
        try:
            loaded.append(build_assist_pathway(parse_assist(raw, path.name)))
        except (UnsupportedAssist, UnsupportedAgreement, KeyError, TypeError, ValueError) as e:
            frm, to = ag_raw.get("from_institution_id", "?"), ag_raw.get("to_institution_id", "?")
            unsupported.append(UnsupportedPathway(
                frm, to, names.get(frm, frm), names.get(to, to), ag_raw.get("major", "?"), ag_raw.get("degree"),
                ag_raw.get("academic_year", "?"), dataset_id, path.name, str(e) or type(e).__name__))

    def reload(self) -> None:
        self.load_directory(self.folder or ARTICULATION_DIR)

    def add_mock(self, catalog: list[Course], agreements: Iterable[TransferAgreement]) -> None:
        """Register ready-made engine data (the dev mock injector). Names resolve through the registry."""
        self.remove_mock()
        for ag in agreements:
            major, degree = split_degree(ag.degree or ag.major)
            self.pathways.append(Pathway(
                college=ag.college, university=ag.university, college_ref=ag.college, university_ref=ag.university,
                major=ag.major, degree=degree, academic_year=None, ge_pattern=ag.ge_pattern, agreement=ag,
                catalog=index_catalog(catalog), source="mock"))

    def remove_mock(self) -> None:
        self.pathways = [p for p in self.pathways if p.source != "mock"]

    # --- lookup ----------------------------------------------------------------------------------
    def _same(self, p: Pathway | UnsupportedPathway, college: str, university: str, major: str) -> bool:
        return (self.registry.same(p.college_ref, college) and self.registry.same(p.university_ref, university)
                and norm(split_degree(p.major)[0]) == norm(major))

    def lookup(self, college: str, university: str, major: str, ge: str, start_term: str | None = None) -> Lookup:
        name, degree = split_degree(major)
        wanted = academic_year_of(start_term)
        found = [p for p in self.pathways
                 if self._same(p, college, university, name)
                 and (degree is None or p.degree is None or degree_key(p.degree) == degree_key(degree))
                 and (p.ge_pattern is None or norm(p.ge_pattern) == norm(ge))]
        unsupported = next((u for u in self.unsupported if self._same(u, college, university, name)
                            and (degree is None or u.degree is None or degree_key(u.degree) == degree_key(degree))), None)
        real = [p for p in found if p.source != "mock"]
        if real:
            usable = [p for p in real if not wanted or not p.academic_year or p.academic_year <= wanted]
            if not usable:
                years = ", ".join(sorted({p.academic_year for p in real}))
                return Lookup(unsupported=unsupported, reason=(
                    f"Articulation data for this pathway is loaded only for {years}; none covers {wanted} "
                    f"(the year {start_term} falls in) or an earlier year."))
            found = usable
        if not found:
            return Lookup(unsupported=unsupported)
        # Loaded data before mock; the requested year, else the latest earlier one; then by degree
        best = min(found, key=lambda p: (p.source == "mock", p.academic_year != wanted, _year_rank(p.academic_year),
                                         p.degree or ""))
        warnings = []
        if wanted and best.academic_year and best.academic_year != wanted:
            warnings.append(f"No {wanted} articulation agreement (the year {start_term} falls in) is loaded for this "
                            f"pathway, so the {best.academic_year} agreement is used. Articulation can change from "
                            f"year to year: check the current agreement on ASSIST.org.")
        others = sorted({p.degree for p in found if p.degree and p.degree != best.degree
                         and p.academic_year == best.academic_year and p.source == best.source})
        if others:
            warnings.append(f"Planning the {best.degree} degree; also available: {', '.join(others)}. Add the degree to "
                            f"the major (e.g. \"{name}, {others[0]}\") to choose it.")
        warnings += best.notes          # GE is decided by the planner (planner.py), from the GE datasets
        return Lookup(match=Match(best, wanted, warnings))

    def find(self, college: str, university: str, major: str, ge: str, start_term: str | None = None) -> Match | None:
        return self.lookup(college, university, major, ge, start_term).match

    def catalog_for(self, college: str) -> dict[str, Course] | None:
        """Every course the pathways know for a college (first definition of a code wins)."""
        merged: dict[str, Course] = {}
        for p in sorted(self.pathways, key=lambda p: p.source == "mock"):
            if self.registry.same(p.college_ref, college):
                for code, course in p.catalog.items():
                    merged.setdefault(code, course)
        return merged or None

    def listing(self) -> dict[str, list[dict]]:
        """GET /pathways: what can be planned, what was found but can't be, and what couldn't be read."""
        resolve = self.registry.resolve
        available = [{
            "source": p.source, "dataset_id": p.dataset_id, "file": p.file,
            "college": p.college, "college_id": resolve(p.college_ref), "university": p.university,
            "university_id": resolve(p.university_ref), "major": p.major, "degree": p.degree,
            "academic_year": p.academic_year, "ge_pattern": p.ge_pattern,
            "required_courses": list(p.agreement.required_courses),
            "choice_groups": [{"id": g.id, "name": g.name, "choose": g.choose, "options": list(g.options)}
                              for g in p.agreement.requirement_groups if g.category.value == "major"],
            "not_articulated": list(p.not_articulated), "prerequisites_enforced": p.prerequisites_enforced,
            "publisher": p.publisher, "source_url": p.source_url, "retrieved_on": p.retrieved_on,
        } for p in self.pathways]
        unsupported = [{
            "dataset_id": u.dataset_id, "file": u.file, "college": u.college, "college_id": u.college_ref,
            "university": u.university, "university_id": u.university_ref, "major": u.major, "degree": u.degree,
            "academic_year": u.academic_year, "reason": u.reason,
        } for u in self.unsupported]
        skipped = [{"file": s.file, "reason": s.reason} for s in self.skipped]
        return {"folder": str(self.folder) if self.folder else None,
                "available": available, "unsupported": unsupported, "skipped": skipped}


def _year_rank(year: str | None) -> int:
    """Later years first (the latest earlier agreement wins); no year last."""
    return -int(year[:4]) if year else 0
