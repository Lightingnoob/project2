"""
GE data (data/ge/*.json), two separate kinds:

  college GE lists   Cal-GETC as approved at ONE college for ONE academic year (Chabot, Porterville ...):
                     areas -> subareas -> eligible course codes, with each course's units and areas.
                     Looked up by institution + pathway + academic year; another college's list is never used.
  UC 7-course        a UC transfer ADMISSION requirement (not a GE certification): English x2, math x1,
                     breadth x4 from >= 2 areas. The file has no college course list, so which courses count
                     is unknown; it only applies to UC targets.

GE planning works on "slots": one per course a requirement still needs (1A, 1B, 1C, 2, 3A, 3B, 4 #1, 4 #2,
5A, 5B, 6 for Cal-GETC). Completed courses and AP exams are matched to slots first (each course counts in one
area; a 5C lab may also count with its 5A/5B area, as the list states). The remaining slots go to the
scheduler as placeholder courses that compete with major-prep courses on the same list, so a major-prep course
that is on the list fills the slot instead of adding a duplicate GE course.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import DATA_DIR, LoadReport, code_key, pick_year, read_json

log = logging.getLogger("uvicorn.error")
GE_DIR = DATA_DIR / "ge"

CAL_GETC = "cal_getc"
UC_SEVEN = "uc_seven_course"
PATHWAY_ALIASES = {"calgetc": CAL_GETC, "cal_getc": CAL_GETC, "cal getc": CAL_GETC,
                   "7coursepattern": UC_SEVEN, "uc7coursepattern": UC_SEVEN, "sevencoursepattern": UC_SEVEN,
                   "ucsevencourse": UC_SEVEN, "ucsevencoursepattern": UC_SEVEN}


def pathway_id(name: str | None) -> str | None:
    """'CAL-GETC' / 'Cal-GETC' / 'cal_getc' -> 'cal_getc'; '7-Course Pattern' -> 'uc_seven_course'."""
    return PATHWAY_ALIASES.get(re.sub(r"[^a-z0-9]", "", (name or "").lower()))


# =====================================================================
# Datasets
# =====================================================================
@dataclass(frozen=True)
class GECourse:
    code: str
    title: str
    units: float | None
    areas: tuple[str, ...]
    same_as: tuple[str, ...]
    former_codes: tuple[str, ...]
    prerequisite: str | None


@dataclass(frozen=True)
class SlotSpec:
    """One course a GE requirement needs."""
    slot_id: str               # '1A', '4#2'
    requirement_id: str        # '1A', '4'
    area_id: str               # '1', '4'
    name: str                  # 'English Composition'
    area_name: str
    eligible: frozenset[str]   # code keys
    rule: str | None
    minimum_units: float | None
    lab_area: bool             # a course here may also carry the area's lab requirement
    discipline_area: int = 0   # >0: slots of this area need that many distinct disciplines


@dataclass
class CollegeGE:
    dataset_id: str
    file: str
    institution_id: str
    institution_name: str
    pathway: str
    display_name: str
    academic_year: str
    source: dict
    minimum_total_units: float | None
    areas: list[dict]
    courses: dict[str, GECourse] = field(default_factory=dict)     # code key -> course
    slots: list[SlotSpec] = field(default_factory=list)
    lab: dict[str, Any] | None = None                              # {area_id, subarea_id, eligible, name}
    notes: list[str] = field(default_factory=list)

    def course(self, code: str) -> GECourse | None:
        return self.courses.get(code_key(code))

    def requirement_units(self, eligible: frozenset[str]) -> float | None:
        units = [self.courses[k].units for k in eligible if k in self.courses and self.courses[k].units]
        return min(units) if units else None

    def subarea(self, requirement_id: str) -> tuple[dict, dict] | None:
        for area in self.areas:
            for sub in area.get("subareas") or []:
                if sub.get("id") == requirement_id:
                    return area, sub
        return None


@dataclass
class UCSevenCourse:
    dataset_id: str
    file: str
    source: dict
    requirements: list[dict]
    area_codes: dict[str, str]
    ap_min_score: int
    ap_exams: dict[str, list[str]]           # exam id -> UC area codes
    general_rules: dict
    notes: list[str]
    academic_year: str | None


def _build_slots(areas: list[dict]) -> tuple[list[SlotSpec], dict | None]:
    slots: list[SlotSpec] = []
    lab = None
    for area in areas:
        aid, aname = str(area["id"]), area.get("name") or f"Area {area['id']}"
        subs = area.get("subareas") or []
        lab_id = area.get("laboratory_subarea") if area.get("laboratory_required") else None
        if lab_id:
            lab_sub = next((s for s in subs if s.get("id") == lab_id), None)
            lab = {"area_id": aid, "subarea_id": lab_id, "name": (lab_sub or {}).get("name") or "Laboratory",
                   "eligible": frozenset(code_key(c) for c in (lab_sub or {}).get("eligible_course_codes") or [])}
        course_subs = [s for s in subs if s.get("id") != lab_id]
        disciplines = int(area.get("minimum_disciplines") or 0)
        counted = 0
        for sub in course_subs:
            n = int(sub.get("minimum_courses") or (area.get("minimum_courses") if len(course_subs) == 1 else 1) or 1)
            eligible = frozenset(code_key(c) for c in sub.get("eligible_course_codes") or [])
            for i in range(n):
                slots.append(SlotSpec(
                    slot_id=sub["id"] if n == 1 else f"{sub['id']}#{i + 1}", requirement_id=str(sub["id"]),
                    area_id=aid, name=sub.get("name") or aname, area_name=aname, eligible=eligible,
                    rule=area.get("rule"), minimum_units=sub.get("minimum_semester_units"),
                    lab_area=bool(lab_id), discipline_area=disciplines))
            counted += n
        extra = int(area.get("minimum_courses") or 0) - counted       # "3 courses, at least one from each ..."
        union = frozenset(k for s in course_subs for k in (code_key(c) for c in s.get("eligible_course_codes") or []))
        for i in range(max(0, extra)):
            slots.append(SlotSpec(f"{aid}#x{i + 1}", aid, aid, aname, aname, union, area.get("rule"), None,
                                  bool(lab_id), disciplines))
    return slots, lab


class GEStore:
    def __init__(self) -> None:
        self.college: dict[tuple[str, str], list[CollegeGE]] = {}      # (institution, pathway) -> by year desc
        self.uc_seven: UCSevenCourse | None = None
        self.report = LoadReport()

    def load_directory(self, folder: Path = GE_DIR) -> None:
        self.college, self.uc_seven, self.report = {}, None, LoadReport()
        for path in sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []:
            try:
                raw = read_json(path)
            except (OSError, ValueError) as e:
                self.report.skip(path, f"not readable JSON ({e})")
                continue
            if not isinstance(raw, dict):
                self.report.skip(path, "not a GE dataset")
                continue
            scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
            if scope.get("type") == "uc_transfer_admission_requirement":
                self._load_uc_seven(path, raw)
            elif (raw.get("institution") or {}).get("id") and (raw.get("ge_pathway") or {}).get("requirements") \
                    and isinstance(raw.get("courses"), list):
                self._load_college(path, raw)
            else:
                self.report.skip(path, "not a GE dataset this app understands (college GE list or UC 7-course)")
        for sets in self.college.values():
            sets.sort(key=lambda d: d.academic_year, reverse=True)

    def _load_college(self, path: Path, raw: dict) -> None:
        gp, inst, src = raw["ge_pathway"], raw["institution"], raw.get("source") or {}
        pid = pathway_id(gp.get("id")) or gp.get("id")
        year = src.get("academic_year")
        if not year:
            self.report.skip(path, "GE list without source.academic_year: its year can't be verified")
            return
        ds = CollegeGE(dataset_id=raw.get("dataset_id") or path.stem, file=path.name, institution_id=inst["id"],
                       institution_name=inst.get("name") or inst["id"], pathway=pid,
                       display_name=gp.get("display_name") or gp.get("name") or pid, academic_year=year,
                       source=src, minimum_total_units=gp.get("minimum_total_semester_units"),
                       areas=gp["requirements"], notes=list(raw.get("notes") or []))
        for c in raw["courses"]:
            code = c.get("code")
            if not code:
                continue
            ds.courses[code_key(code)] = GECourse(
                code=code, title=c.get("title") or "", units=c.get("semester_units"),
                areas=tuple(c.get("cal_getc_areas") or ()), same_as=tuple(c.get("same_as") or ()),
                former_codes=tuple(c.get("former_codes") or ()), prerequisite=c.get("prerequisite"))
        ds.slots, ds.lab = _build_slots(ds.areas)
        self.college.setdefault((ds.institution_id, pid), []).append(ds)
        self.report.loaded.append({"file": path.name, "kind": "college_ge_list", "institution_id": ds.institution_id,
                                   "pathway": pid, "academic_year": year, "courses": len(ds.courses)})

    def _load_uc_seven(self, path: Path, raw: dict) -> None:
        gp, ap = raw.get("ge_pathway") or {}, raw.get("ap_rules") or {}
        m = re.search(r"(\d{4}-\d{4})", str((raw.get("source") or {}).get("document_title", "")))
        self.uc_seven = UCSevenCourse(
            dataset_id=raw.get("dataset_id") or path.stem, file=path.name, source=raw.get("source") or {},
            requirements=gp.get("requirements") or [], area_codes=gp.get("course_area_codes") or {},
            ap_min_score=int(ap.get("minimum_score") or 3),
            ap_exams={e["exam_id"]: list(e.get("areas") or []) for e in ap.get("exams") or []},
            general_rules=raw.get("general_rules") or {}, notes=list(raw.get("notes") or []),
            academic_year=m[1] if m else None)
        self.report.loaded.append({"file": path.name, "kind": "uc_admission_requirement", "pathway": UC_SEVEN,
                                   "requirements": len(self.uc_seven.requirements),
                                   "college_course_list_included": False})

    # --- lookup ------------------------------------------------------------------------------------
    def college_list(self, inst_id: str, pathway: str, academic_year: str | None) -> CollegeGE | None:
        """That year's list, else the latest earlier one; never a later year's or another college's list."""
        sets = self.college.get((inst_id, pathway)) or []
        year = pick_year([d.academic_year for d in sets], academic_year)
        return next((d for d in sets if d.academic_year == year), None)

    def years(self, inst_id: str, pathway: str) -> list[str]:
        return [d.academic_year for d in self.college.get((inst_id, pathway)) or []]

    def status(self) -> list[dict]:
        rows = [{"institution_id": d.institution_id, "institution": d.institution_name, "pathway": d.pathway,
                 "name": f"{d.institution_name} {d.display_name}", "academic_year": d.academic_year, "file": d.file,
                 "courses": len(d.courses), "status": "loaded"}
                for sets in self.college.values() for d in sets]
        if self.uc_seven:
            rows.append({"institution_id": None, "pathway": UC_SEVEN, "name": "UC 7-course pattern",
                         "academic_year": self.uc_seven.academic_year, "file": self.uc_seven.file,
                         "status": "loaded", "applies_to": "UC targets only (admission requirement)",
                         "college_course_list": "not_loaded"})
        return rows


# =====================================================================
# Matching completed courses / AP exams to slots
# =====================================================================
@dataclass(frozen=True)
class GEItem:
    """Something that can fill GE slots: a completed course or an AP exam."""
    kind: str                                   # completed_course | ap_exam
    ref: str                                    # course code or exam id
    label: str
    discipline: str
    options: tuple[tuple[str | None, bool], ...]  # (requirement id or None, also satisfies the lab)


@dataclass
class SlotState:
    spec: SlotSpec
    satisfied_by: GEItem | None = None
    lab_by_this: bool = False


def course_item(ds: CollegeGE, code: str, title: str | None = None) -> GEItem | None:
    key = code_key(code)
    reqs = [s.requirement_id for s in ds.slots if key in s.eligible]
    lab = bool(ds.lab and key in ds.lab["eligible"])
    reqs = list(dict.fromkeys(reqs))
    if not reqs and not lab:
        return None
    options = tuple((r, lab) for r in reqs) + (((None, True),) if lab else ())
    course = ds.course(code)
    return GEItem("completed_course", code, f"{code}" + (f" {course.title}" if course else (f" {title}" if title else "")),
                  re.sub(r"[^A-Z]", "", code.split()[0].upper()) if code.split() else code, options)


def parse_ge_areas(text: str | None, lab_id: str | None = "5C") -> tuple[tuple[str | None, bool], ...]:
    """A published AP GE-area statement -> slot options: '5B and 5C' -> ((5B, lab),); '3A or 3B' -> ((3A,), (3B,));
    '4; US-2' -> ((4,),) (only the first clause is Cal-GETC). Unknown shapes -> ()."""
    if not text:
        return ()
    first = text.split(";")[0].strip()
    if not first:
        return ()
    if " or " in first:
        return tuple((a.strip(), False) for a in first.split(" or ") if a.strip())
    parts = [a.strip() for a in re.split(r"\s+and\s+|\+", first) if a.strip()]
    lab = lab_id in parts if lab_id else False
    areas = [a for a in parts if a != lab_id]
    if len(areas) == 1:
        return ((areas[0], lab),)
    if not areas and lab:
        return ((None, True),)
    return ()


def match_items(slots: list[SlotSpec], lab_required: bool, items: list[GEItem]) -> tuple[list[SlotState], GEItem | None]:
    """Assign items to slots so the most slots are filled (then the lab), each item used once."""
    items = [i for i in items if i.options][:24]
    best: dict = {"score": (-1, -1), "assign": {}, "lab": None}
    by_req: dict[str, list[int]] = {}
    for n, s in enumerate(slots):
        by_req.setdefault(s.requirement_id, []).append(n)

    def ok_discipline(assign: dict[int, GEItem], slot_n: int, item: GEItem) -> bool:
        spec = slots[slot_n]
        if not spec.discipline_area:
            return True
        same_area = [n for n in range(len(slots)) if slots[n].area_id == spec.area_id]
        used = {assign[n].discipline for n in same_area if n in assign}
        distinct_after = len(used | {item.discipline})
        remaining_after = sum(1 for n in same_area if n not in assign and n != slot_n)
        return distinct_after + remaining_after >= min(spec.discipline_area, len(same_area))

    seen: set = set()

    def dfs(i: int, assign: dict[int, GEItem], lab: GEItem | None) -> None:
        key = (i, frozenset((n, it.ref) for n, it in assign.items()), lab.ref if lab else None)
        if key in seen:
            return
        seen.add(key)
        score = (len(assign), 1 if lab or not lab_required else 0)
        if score > best["score"]:
            best.update(score=score, assign=dict(assign), lab=lab)
        if i == len(items):
            return
        item = items[i]
        for req, gives_lab in item.options:
            new_lab = lab or (item if gives_lab and lab_required else None)
            if req is None:
                if gives_lab and not lab:
                    dfs(i + 1, assign, new_lab)
                continue
            for n in by_req.get(req, []):
                if n not in assign and ok_discipline(assign, n, item):
                    assign[n] = item
                    dfs(i + 1, assign, new_lab)
                    del assign[n]
                    break                                # identical slots: trying the first free one is enough
        dfs(i + 1, assign, lab)                          # item not used

    dfs(0, {}, None)
    states = [SlotState(s, best["assign"].get(n)) for n, s in enumerate(slots)]
    return states, best["lab"]
