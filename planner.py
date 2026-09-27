"""
/generate-sep for loaded (non-mock) pathways: joins the separate data layers, then hands sep_engine one
concrete problem.

    planner input
    -> canonical institutions / major / academic year        (PathwayStore lookup, done by main.py)
    -> matching articulation agreement                        (its valid ALTERNATIVES; OR stays OR)
    -> completed courses, canonicalized                        (catalog codes, former codes, cross-listings)
    -> community-college AP waivers                            (only what the college's AP chart states)
    -> remaining major preparation + prerequisites             (prerequisite registry, non-guessing resolver)
    -> remaining GE                                            (the college's own Cal-GETC list for that year)
    -> major-prep / GE overlap                                 (GE slots compete with major courses in Dijkstra)
    -> schedule (sep_engine.generate_sep)
    -> structured plan: typed courses, GE slots, remaining_after_transfer, data sources, AP layers, notes

Nothing here invents articulation, prerequisites, GE eligibility, AP equivalencies or data years: whatever the
data can't support is returned as needs_review / not_loaded / unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from articulation.assist import Alternative, _dnf, to_engine
from datastores import DataRegistry, university_system
from datastores.common import academic_year_of, code_key, split_code, year_status
from datastores.ge import (CAL_GETC, UC_SEVEN, CollegeGE, GEItem, SlotSpec, course_item, match_items, parse_ge_areas,
                           pathway_id)
from datastores.prerequisites import PrereqRecord, PrereqResolver, ResolvedPrereqs
from sep_engine import (Course, GroupCategory, RequirementGroup, SEPError, StudentProfile, TransferAgreement,
                        generate_sep, select_pathway)
from sep_engine.pipeline import node_id
from sep_engine.scheduler import parse_term_label

TOP_ALTERNATIVES = 12
PLACEHOLDER_DIFFICULTY = 1.0      # a GE slot wins a units tie against a real course picked only for GE


class PlanError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class AltSpec:
    index: int
    required: list[str]
    groups: list[RequirementGroup]
    remaining: list[str]                  # requirement ids (ASSIST) or labels (legacy)
    path: list[str]
    alt: Alternative | None = None


@dataclass
class SlotReq:
    spec: SlotSpec
    lab_mode: str | None                  # None | 'with_lab' | 'lab_only'

    @property
    def group_id(self) -> str:
        return f"ge-{self.spec.slot_id}" + ("-lab" if self.lab_mode == "with_lab" else "")


@dataclass
class Built:
    catalog: dict[str, Course]
    agreement: TransferAgreement
    completed: list[str]
    placeholders: dict[str, dict]         # placeholder code -> GE slot metadata
    resolved: ResolvedPrereqs
    alt: AltSpec
    variant: list[SlotReq]


@dataclass
class Note:
    kind: str
    message: str
    course: str | None = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, **({"course": self.course} if self.course else {})}


@dataclass
class Planner:
    req: Any
    match: Any
    data: DataRegistry
    notes: list[Note] = field(default_factory=list)

    def note(self, kind: str, message: str, course: str | None = None) -> None:
        if not any(n.message == message for n in self.notes):
            self.notes.append(Note(kind, message, course))

    # =================================================================
    # entry point
    # =================================================================
    def run(self) -> dict:
        p = self.pathway = self.match.pathway
        self.ag = p.assist
        self.college_id, self.university_id = p.college_ref, p.university_ref
        self.wanted = academic_year_of(self.req.start_term)
        self.system = university_system(p.university)
        self.ge_pid = pathway_id(self.req.ge_pathway)
        if self.ge_pid == UC_SEVEN and self.system != "UC":
            raise PlanError(422, f"The UC 7-course pattern is a UC transfer admission requirement, so it doesn't apply "
                                 f"to {p.university}. Choose CAL-GETC for this target.")
        for w in self.match.warnings:
            self.note("articulation", w)
        self.catalog = self.data.catalogs.for_institution(self.college_id, self.wanted)
        self.prereg = self.data.prereqs.get(self.college_id)
        self.ge_ds: CollegeGE | None = (self.data.ge.college_list(self.college_id, CAL_GETC, self.wanted)
                                        if self.ge_pid == CAL_GETC else None)
        self._completed()
        self._ap()
        self._ge_state()
        built = self._choose()
        season, year = parse_term_label(self.req.start_term)
        profile = StudentProfile(completed_courses=built.completed, start_term=season, start_year=year,
                                 include_summer=self.req.include_summer)
        try:
            result = generate_sep(built.catalog, built.agreement, profile)
        except SEPError as e:
            raise PlanError(422, str(e)) from e
        for w in result.pop("warnings"):
            self.note("engine", w)
        for plan in result["plans"]:
            self._annotate(plan, built)
        in_plan = {c["code"] for plan in result["plans"] for s in plan["semesters"] for c in s["courses"]}
        self._prereq_notes(built.resolved, in_plan)
        self._source_notes()
        result["articulation"] = self._articulation_info(built, result["plans"][0] if result["plans"] else None)
        result["remaining_after_transfer"] = self._remaining(built.alt)
        if result["remaining_after_transfer"] and self.ag:   # legacy files already say so in their notes
            items = "; ".join(r["label"] for r in result["remaining_after_transfer"])
            self.note("remaining_after_transfer", (
                f"{len(result['remaining_after_transfer'])} {p.university} requirement(s) have no {p.college} course "
                f"articulated, so they are completed after transfer: {items}."))
        result["data_sources"] = self._data_sources()
        result["ap_evaluations"] = self.ap_evals
        result["completed_courses_normalized"] = self.completed_rows
        result["admission_requirements"] = self._admission_requirements()
        result["review_items"] = [{"course": r.course, "kind": r.kind, "message": r.message}
                                  for r in built.resolved.reviews if r.course in in_plan]
        result["notes"] = [n.to_dict() for n in self.notes]
        result["warnings"] = [n.message for n in self.notes]
        return result

    # =================================================================
    # completed courses
    # =================================================================
    def _course_index(self) -> dict[str, tuple[str, str, float | None]]:
        """Courses the agreement itself describes: key -> (code, title, units)."""
        if self.ag:
            return {c.key: (c.code, c.title, c.units) for c in self.ag.courses.values()}
        return {code_key(c.code): (c.code, c.title, c.units) for c in self.pathway.catalog.values()}

    def _completed(self) -> None:
        index = self._course_index()
        cross = self.ag.cross_listed() if self.ag else {}
        rows, codes = [], []
        for raw in self.req.completed_courses:
            k = code_key(raw)
            if not k:
                continue
            code, by = None, "unknown"
            if k in cross and cross[k] in index:
                code, by = index[cross[k]][0], "cross_listed"
            elif k in index:
                code, by = index[k][0], "agreement"
            elif self.catalog and (hit := self.catalog.resolve(raw)):
                code, by = hit[0].code, hit[1]
            elif self.ge_ds and (g := self.ge_ds.course(raw)):
                code, by = g.code, "ge_list"
            rows.append({"entered": raw, "code": code, "matched_by": by})
            if code and code not in codes:
                codes.append(code)
        unknown = [r["entered"] for r in rows if not r["code"]]
        if unknown:
            where = f"the {self.catalog.title}" if self.catalog else f"the loaded {self.pathway.college} data"
            self.note("completed_courses", f"Not found in {where}, so not counted: {', '.join(unknown)}.")
        self.completed_rows = rows
        self.completed_real = codes              # entered by the student (canonical codes)

    # =================================================================
    # AP: community college effect, GE effect, target campus effect (kept apart)
    # =================================================================
    def _ap(self) -> None:
        ap = self.data.ap
        relevant = set(self._course_index())
        self.ap_evals, self.ap_waived, self.ap_ge_items = [], [], []
        uc = self.data.ge.uc_seven
        for entry in self.req.ap_scores:
            subject, score = entry.subject, entry.score
            exam_id = ap.normalize_exam(subject)
            if exam_id is None:
                self.ap_evals.append({"subject": subject, "score": score, "exam_id": None, "status": "unknown_exam",
                                      "message": "This AP subject isn't in the loaded AP exam list."})
                continue
            cc = ap.community_college_effect(self.college_id, exam_id, score, relevant)
            for c in cc.get("waived_courses") or []:
                if c not in self.ap_waived:
                    self.ap_waived.append(c)
            for cond in cc.get("conditions") or []:
                if cc["status"] == "applied":
                    self.note("ap", f"{ap.exams.get(exam_id, subject)}: {cond}")
            ge = self._ap_ge_effect(exam_id, score, cc, uc)
            target = ap.target_campus_effect(self.university_id, exam_id, score)
            self.ap_evals.append({"subject": subject, "exam_id": exam_id, "exam_name": ap.exams.get(exam_id, subject),
                                  "score": score, "status": "evaluated", "community_college_effect": cc,
                                  "ge_effect": ge, "target_campus_effect": target})
        if any(e.get("target_campus_effect", {}).get("rules") for e in self.ap_evals):
            self.note("ap", f"AP credit at {self.pathway.university} is listed with each exam for reference. The campus "
                            f"decides it (it needs review), so it isn't applied to your {self.pathway.college} plan.")

    def _ap_ge_effect(self, exam_id: str, score: int, cc: dict, uc) -> dict:
        if self.ge_pid == UC_SEVEN:
            if uc is None:
                return {"pathway": UC_SEVEN, "status": "not_loaded", "areas": []}
            areas = uc.ap_exams.get(exam_id)
            if not areas:
                return {"pathway": UC_SEVEN, "status": "not_listed", "areas": []}
            if score < uc.ap_min_score:
                return {"pathway": UC_SEVEN, "status": "below_minimum", "areas": areas}
            reqs = uc.requirements
            options = tuple((r["id"], False) for a in areas for r in reqs if a in (r.get("accepts") or []))
            self.ap_ge_items.append(GEItem("ap_exam", exam_id, f"{self.data.ap.exams.get(exam_id, exam_id)} ({score})",
                                           areas[0], options))
            return {"pathway": UC_SEVEN, "status": "eligible", "areas": areas, "source": uc.source.get("document_title")}
        if self.ge_ds is None:
            return {"pathway": self.ge_pid, "status": "not_loaded", "areas": []}
        text = cc.get("ge_areas_text")
        if cc["status"] in ("not_loaded", "not_listed"):
            return {"pathway": CAL_GETC, "status": cc["status"], "areas": []}
        if cc["status"] == "below_minimum":
            return {"pathway": CAL_GETC, "status": "below_minimum", "areas_text": text, "areas": []}
        if not text:
            return {"pathway": CAL_GETC, "status": "none_listed", "areas": []}
        options = parse_ge_areas(text, self.ge_ds.lab["subarea_id"] if self.ge_ds.lab else None)
        if not options:
            self.note("ap", f"{self.data.ap.exams.get(exam_id)}: the college chart's GE entry ({text}) couldn't be read, "
                            f"so it wasn't applied; ask a counselor.")
            return {"pathway": CAL_GETC, "status": "needs_review", "areas_text": text, "areas": []}
        discipline = (cc.get("listed_courses") or [exam_id])[0].split()[0]
        self.ap_ge_items.append(GEItem("ap_exam", exam_id, f"{self.data.ap.exams.get(exam_id, exam_id)} ({score})",
                                       discipline, options))
        return {"pathway": CAL_GETC, "status": "eligible", "areas_text": text,
                "areas": [a for a, _ in options if a], "choose_one": " or " in text.split(";")[0],
                "lab": any(lab for _, lab in options), "source": cc.get("source")}

    # =================================================================
    # GE: what completed courses and AP already cover; slot variants for the rest
    # =================================================================
    def _ge_state(self) -> None:
        self.ge_states, self.ge_lab_item, self.ge_variants, self.ge_label = [], None, [[]], None
        if self.ge_pid == CAL_GETC:
            ds = self.ge_ds
            if ds is None:
                years = self.data.ge.years(self.college_id, CAL_GETC)
                self.note("ge", (f"GE isn't planned: no Cal-GETC course list for {self.pathway.college} is loaded"
                                 + (f" for {self.wanted} or an earlier year (loaded: {', '.join(years)})." if years else ".")))
                return
            self.ge_label = ds.display_name
            items = [it for c in self.completed_real if c not in self.ap_waived
                     for it in [course_item(ds, c)] if it]
            items += self.ap_ge_items
            self.ge_states, self.ge_lab_item = match_items(ds.slots, bool(ds.lab), items)
            remaining = [s.spec for s in self.ge_states if s.satisfied_by is None]
            lab_needed = bool(ds.lab) and self.ge_lab_item is None
            if not lab_needed:
                self.ge_variants = [[SlotReq(s, None) for s in remaining]]
            else:
                lab_slots = [s for s in remaining if s.lab_area]
                if lab_slots:
                    self.ge_variants = [[SlotReq(s, "with_lab" if s is ls else None) for s in remaining] for ls in lab_slots]
                else:
                    lab = ds.lab
                    spec = SlotSpec(lab["subarea_id"], lab["subarea_id"], lab["area_id"], lab["name"],
                                    lab["name"], lab["eligible"], None, None, True)
                    self.ge_variants = [[SlotReq(s, None) for s in remaining] + [SlotReq(spec, "lab_only")]]
        elif self.ge_pid == UC_SEVEN:
            uc = self.data.ge.uc_seven
            if uc is None:
                self.note("ge", "The UC 7-course pattern isn't loaded, so it isn't planned.")
                return
            self.ge_label = "UC 7-course pattern"
            specs = []
            for r in uc.requirements:
                n = int(r.get("minimum_courses") or 1)
                for i in range(n):
                    specs.append(SlotSpec(r["id"] if n == 1 else f"{r['id']}#{i + 1}", r["id"], r["id"], r["name"],
                                          r.get("area_name") or r["name"], frozenset(), r.get("rule"),
                                          r.get("minimum_semester_units_each"), False,
                                          int(r.get("minimum_distinct_areas") or 0)))
            self.uc_specs = specs
            self.ge_states, _ = match_items(specs, False, self.ap_ge_items)
            self.ge_variants = [[SlotReq(s.spec, None) for s in self.ge_states if s.satisfied_by is None]]
            self.note("ge", ("The UC 7-course pattern file has no college course list, so completed courses and "
                             "major-prep courses can't be checked against it; each remaining course is planned as its "
                             "own slot. Confirm UC-E/UC-M/UC-H/UC-B/UC-S courses on ASSIST."))

    # =================================================================
    # alternatives x GE variants -> the cheapest valid engine problem
    # =================================================================
    def _alternatives(self) -> list[AltSpec]:
        if self.ag is None:
            ag = self.pathway.agreement
            return [AltSpec(0, list(ag.required_courses), [g for g in ag.requirement_groups
                                                            if g.category is GroupCategory.MAJOR],
                            list(self.pathway.not_articulated), [])]
        out = []
        for i, alt in enumerate(self.ag.alternatives):
            required, groups = to_engine(self.ag, alt)
            out.append(AltSpec(i, required, groups, list(alt.remaining), list(alt.path), alt))
        done = {code_key(c) for c in self.completed_real + self.ap_waived}
        index = self._course_index()

        def bound(a: AltSpec) -> tuple:
            units = sum(index[code_key(c)][2] or 0 for c in a.required if code_key(c) not in done)
            units += sum(min((index[code_key(o)][2] or 0) for o in g.options) for g in a.groups
                         if not any(code_key(o) in done for o in g.options))
            return (len(a.remaining), units, a.index)

        return sorted(out, key=bound)[:TOP_ALTERNATIVES]

    def _choose(self) -> Built:
        errors, best = [], None
        resolved_cache: dict[int, ResolvedPrereqs] = {}
        for alt in self._alternatives():
            for variant in self.ge_variants:
                try:
                    built = self._build(alt, variant, resolved_cache)
                    sel = select_pathway(built.catalog, built.agreement, built.completed, "units")
                except SEPError as e:
                    errors.append(str(e))
                    continue
                key = (len(alt.remaining), sel.cost, alt.index)
                if best is None or key < best[0]:
                    best = (key, built)
        if best is None:
            raise PlanError(422, "No valid plan could be built from the loaded data: " + (errors[0] if errors else
                                                                                          "no alternatives"))
        return best[1]

    def _units_of(self, key: str) -> tuple[str, str, float | None] | None:
        index = self._course_index()
        if key in index and index[key][2]:
            return index[key]
        if self.catalog:
            c = self.catalog.get(key)
            if c and c.type == "credit" and c.units:
                return (c.code, c.title, c.units)
        return None

    def _record(self, key: str) -> PrereqRecord | None:
        if self.prereg and key in self.prereg.records:
            return self.prereg.records[key]
        if self.ag and key in self.ag.prereq_records:
            return self.ag.prereq_records[key]
        if self.ag is None:                                   # legacy file: its own prereqs/coreqs lists
            course = next((c for c in self.pathway.catalog.values() if code_key(c.code) == key), None)
            if course is not None:
                items = ([{"type": "COURSE", "course_id": f"x:{code_key(p)}", "timing": "before"} for p in course.prereqs]
                         + [{"type": "COURSE", "course_id": f"x:{code_key(q)}", "timing": "before_or_concurrent"}
                            for q in course.coreqs])
                return PrereqRecord(key, course.code, "documented", {"type": "AND", "items": items} if items
                                    else {"type": "NONE"}, None, (), None, f"agreement:{self.pathway.file}")
        return None

    def _build(self, alt: AltSpec, variant: list[SlotReq], cache: dict[int, ResolvedPrereqs]) -> Built:
        completed_all = [*self.completed_real, *(c for c in self.ap_waived if c not in self.completed_real)]
        seeds = list(dict.fromkeys([*alt.required, *(o for g in alt.groups for o in g.options)]))
        if alt.index not in cache:
            resolver = PrereqResolver(self._record, self._units_of, completed_all,
                                      self.prereg.alias_of if self.prereg else None,
                                      self.prereg.external if self.prereg else None)
            cache[alt.index] = resolver.resolve(seeds, anchor=alt.required)
        resolved = cache[alt.index]
        codes = list(dict.fromkeys([*seeds, *resolved.added,
                                    *(d for deps in (*resolved.prereqs.values(), *resolved.coreqs.values()) for d in deps)]))
        catalog: dict[str, Course] = {}
        for code in codes:
            info = self._units_of(code_key(code))
            if info is None:
                raise SEPError(f"{code} has no units in the loaded data, so it can't be planned.")
            catalog[info[0]] = None                       # placeholder: filled below once every code is known
        for code in list(catalog):
            info = self._units_of(code_key(code))
            catalog[code] = Course(code=code, title=info[1], units=info[2],
                                   prereqs=[p for p in resolved.prereqs.get(code, []) if p in catalog and p != code],
                                   coreqs=[q for q in resolved.coreqs.get(code, []) if q in catalog and q != code])
        completed = [c for c in completed_all if c in catalog]
        placeholders, ge_groups = {}, []
        for slot in variant:
            ph = self._placeholder(slot, catalog)
            if ph is None:
                continue
            course, meta, candidates = ph
            catalog[course.code] = course
            placeholders[course.code] = meta
            ge_groups.append(RequirementGroup(id=slot.group_id, name=meta["label"], category=GroupCategory.GE,
                                              choose=1, options=[*candidates, course.code]))
        p = self.pathway
        agreement = TransferAgreement(
            college=p.college, university=p.university, major=p.major,
            degree=(f"{p.major}, {p.degree}" if p.degree else None), ge_pattern=self.req.ge_pathway,
            required_courses=alt.required, requirement_groups=[*alt.groups, *ge_groups])
        return Built(catalog, agreement, completed, placeholders, resolved, alt, variant)

    def _placeholder(self, slot: SlotReq, catalog: dict[str, Course]):
        spec = slot.spec
        if self.ge_pid == UC_SEVEN:
            units, eligible = spec.minimum_units or 3, frozenset()
        else:
            ds = self.ge_ds
            eligible = spec.eligible & ds.lab["eligible"] if slot.lab_mode == "with_lab" else spec.eligible
            units = ds.requirement_units(eligible)
            if units is None:
                self.note("ge", f"{ds.display_name} {spec.requirement_id}: no eligible course with units is listed, "
                                f"so this requirement isn't planned; check with a counselor.")
                return None
        suffix = {"with_lab": " (with lab)", "lab_only": " (lab)"}.get(slot.lab_mode or "", "")
        label = f"{self.ge_label} {spec.requirement_id}{suffix}: {spec.name}"
        code = f"GE {spec.slot_id}" + ("+LAB" if slot.lab_mode == "with_lab" else "")
        meta = {"pathway": self.ge_pid, "display_name": self.ge_label, "requirement_id": spec.requirement_id,
                "slot_id": spec.slot_id, "area_id": spec.area_id, "name": spec.name, "area_name": spec.area_name,
                "lab_required": slot.lab_mode == "with_lab", "lab_only": slot.lab_mode == "lab_only",
                "rule": spec.rule, "label": label, "institution_id": self.college_id,
                "academic_year": self.ge_ds.academic_year if self.ge_ds else None,
                "options_status": "loaded" if eligible else "not_loaded"}
        candidates = [c for c in catalog if code_key(c) in eligible]
        course = Course(code=code, title=label, units=units, difficulty=PLACEHOLDER_DIFFICULTY)
        return course, meta, candidates

    # =================================================================
    # output
    # =================================================================
    def _req_labels(self) -> dict[str, list[str]]:
        """Course code -> the articulation requirements it can meet (from the agreement's own expressions)."""
        out: dict[str, list[str]] = {}
        if not self.ag:
            return out
        for r in self.ag.requirements.values():
            if not r.expr:
                continue
            for s in _dnf(r.expr):
                for k in s:
                    out.setdefault(self.ag.code(k), [])
                    if r.label not in out[self.ag.code(k)]:
                        out[self.ag.code(k)].append(r.label)
        return out

    def _annotate(self, plan: dict, built: Built) -> None:
        labels = self._req_labels()
        chosen_major = {c["code"] for r in plan["requirements"] if r["category"] == "major" for c in r["courses"]}
        ge_names = {g.id: g.name for g in built.agreement.requirement_groups if g.category is GroupCategory.GE}

        def kind(code: str, is_ge: bool) -> str:
            if code in built.placeholders:
                return "ge_slot"
            if is_ge:
                return "ge_course"
            return "major_prep" if code in chosen_major else "prerequisite"

        for sem in plan["semesters"]:
            for c in sem["courses"]:
                c["type"] = kind(c["code"], c["is_ge"])
                if c["type"] == "ge_slot":
                    c["ge"] = built.placeholders[c["code"]]
                    c["satisfies"] = [c["ge"]["label"]]
                elif c["type"] == "major_prep" and labels.get(c["code"]):
                    extra = [s for s in c["satisfies"] if s in ge_names.values()]
                    c["satisfies"] = labels[c["code"]] + extra
        for n in plan["graph"]["nodes"]:
            d = n["data"]
            d["type"] = kind(d["label"], d["is_ge"])
        plan["ge"] = self._ge_block(plan, built)

    def _ge_block(self, plan: dict, built: Built) -> dict | None:
        if not self.ge_label:
            return None
        chosen = {r["id"]: r["courses"][0] for r in plan["requirements"] if r["category"] == "ge" and r["courses"]}
        group_of = {s.spec.slot_id: s for s in built.variant}

        def item_ref(it: GEItem) -> dict:
            return {"type": it.kind, "ref": it.ref, "label": it.label}

        rows, lab_row = [], None
        for st in self.ge_states:
            spec = st.spec
            row = {"slot_id": spec.slot_id, "requirement_id": spec.requirement_id, "area_id": spec.area_id,
                   "name": spec.name, "area_name": spec.area_name, "rule": spec.rule}
            if st.satisfied_by:
                row.update(status="completed" if st.satisfied_by.kind == "completed_course" else "ap",
                           satisfied_by=item_ref(st.satisfied_by))
            else:
                slot = group_of.get(spec.slot_id)
                pick = chosen.get(slot.group_id) if slot else None
                if pick is None:
                    row.update(status="not_planned")
                elif pick["code"] in built.placeholders:
                    row.update(status="planned", term=pick["term"], units=built.catalog[pick["code"]].units,
                               slot_code=pick["code"], lab_required=slot.lab_mode == "with_lab")
                else:
                    row.update(status="completed" if pick["status"] == "completed" else "major_prep",
                               satisfied_by={"type": "major_prep" if pick["kind"] == "major" else "course",
                                             "ref": pick["code"], "label": f"{pick['code']} {pick['title']}"},
                               term=pick["term"], lab_required=slot.lab_mode == "with_lab")
            rows.append(row)
        extra = [s for s in built.variant if s.lab_mode == "lab_only"]
        for slot in extra:
            pick = chosen.get(slot.group_id)
            rows.append({"slot_id": slot.spec.slot_id, "requirement_id": slot.spec.requirement_id,
                         "area_id": slot.spec.area_id, "name": slot.spec.name, "area_name": slot.spec.area_name,
                         "rule": None, "status": "planned" if pick else "not_planned",
                         "term": pick["term"] if pick else None, "slot_code": pick["code"] if pick else None})
        if self.ge_ds and self.ge_ds.lab:
            with_lab = next((r for r in rows if r.get("lab_required") or r["slot_id"] == self.ge_ds.lab["subarea_id"]), None)
            if self.ge_lab_item:
                lab_row = {"required": True, "status": "completed" if self.ge_lab_item.kind == "completed_course" else "ap",
                           "satisfied_by": item_ref(self.ge_lab_item)}
            elif with_lab:
                lab_row = {"required": True, "status": with_lab["status"], "via_requirement": with_lab["requirement_id"],
                           "term": with_lab.get("term")}
            else:
                lab_row = {"required": True, "status": "not_planned"}
        ds = self.ge_ds
        return {
            "pathway": self.ge_pid, "display_name": self.ge_label, "institution_id": self.college_id,
            "dataset_id": ds.dataset_id if ds else (self.data.ge.uc_seven.dataset_id if self.data.ge.uc_seven else None),
            "academic_year": ds.academic_year if ds else None, "requested_academic_year": self.wanted,
            "year_status": year_status(ds.academic_year, self.wanted) if ds else "unknown",
            "course_list": "loaded" if ds else "not_loaded",
            "requirements": rows, "lab": lab_row,
            "planned_units": sum(built.catalog[c].units for s in plan["semesters"] for c in
                                 (x["code"] for x in s["courses"]) if c in built.placeholders),
            "rules": ["Each course counts toward one area; a 5C lab may also count with its 5A/5B area (as the "
                      "college list states)."] if ds else [],
        }

    def _articulation_info(self, built: Built, plan: dict | None) -> dict:
        p, ag = self.pathway, self.ag
        info = {"source": p.source, "dataset_id": p.dataset_id, "file": p.file, "publisher": p.publisher,
                "url": p.source_url, "retrieved_on": p.retrieved_on if not ag else None,
                "academic_year": p.academic_year, "requested_academic_year": self.match.academic_year,
                "year_status": year_status(p.academic_year, self.match.academic_year),
                "degree": p.degree, "not_articulated": [r["label"] for r in self._remaining(built.alt)],
                "prerequisites_enforced": bool(self.prereg) or p.prerequisites_enforced}
        if not ag:
            return info
        chosen = {}
        if plan:
            for r in plan["requirements"]:
                if r["category"] == "major" and r["id"] != "major-prep":
                    chosen[r["id"]] = [c for c in r["courses"]]
        done = set(built.completed)
        required = {code_key(c) for c in built.alt.required}
        rows = []
        for r in ag.requirements.values():
            row = {**r.to_dict(), "source_expression": ag.expr_text(r.expr)}
            if built.alt.alt and r.id in built.alt.alt.remaining:
                row.update(status="remaining_after_transfer", chosen=[])
            elif r.id in chosen:
                picks = chosen[r.id]
                row.update(status="completed" if all(c["status"] == "completed" for c in picks) else "planned",
                           chosen=[c["code"] for c in picks])
            elif r.expr and (sets := [s for s in _dnf(r.expr) if s <= required]):
                codes = [ag.code(k) for k in sorted(sets[0])]
                row.update(status="completed" if all(c in done for c in codes) else "planned", chosen=codes)
            else:
                row.update(status="not_selected", chosen=[])
            rows.append(row)
        info.update(document_title=ag.source.get("document_title"), date_published=ag.source.get("date_published"),
                    schema_version=ag.schema_version, concentration=ag.concentration, requirement_scope=ag.scope,
                    complete_degree_requirements_in_source=ag.complete_degree, requirements=rows,
                    alternatives_total=len(ag.alternatives), chosen_path=built.alt.path,
                    transfer_policies=ag.policies, recommendations=ag.recommendations)
        if len(ag.alternatives) > 1:
            self.note("articulation", (
                f"This agreement can be met {len(ag.alternatives)} ways; the plan uses the one with the fewest units"
                + (f" ({'; '.join(built.alt.path)})" if built.alt.path else "") + ". The other ways are equally valid."))
        return info

    def _remaining(self, alt: AltSpec) -> list[dict]:
        if not self.ag:
            return [{"type": "remaining_after_transfer", "requirement_id": None, "label": label,
                     "reason": "no_course_articulated", "target_courses": []} for label in alt.remaining]
        out = []
        for rid in alt.remaining:
            r = self.ag.requirements[rid]
            out.append({"type": "remaining_after_transfer", "requirement_id": rid, "label": r.label,
                        "target_courses": [t.__dict__ for t in r.targets],
                        "reason": "no_course_articulated" if r.status == "no_course_articulated" else r.status,
                        "source_note": r.note})
        return out

    def _prereq_notes(self, resolved: ResolvedPrereqs, in_plan: set[str]) -> None:
        """One note per planned course whose prerequisite needs review (all details stay in review_items)."""
        by_course: dict[str, list] = {}
        for r in resolved.reviews:
            if r.course in in_plan:
                by_course.setdefault(r.course, []).append(r)
        for course, items in by_course.items():
            kinds = {r.kind for r in items}
            routes = list(dict.fromkeys(r.detail for r in items if r.kind == "condition_route" and r.detail))
            if "policy" in kinds:
                rec = self._record(code_key(course))
                msg = f"{course}: {rec.summary if rec and rec.summary else 'depends on placement or college policy'}"
                if routes or "condition" in kinds:
                    msg += " No prerequisite course was added; confirm placement with a counselor."
            elif routes:
                msg = (f"{course} prerequisite: {' and '.join(routes)}. This depends on placement or approval, so no "
                       f"prerequisite course was added; confirm with a counselor.")
            else:
                msg = " ".join(dict.fromkeys(r.message for r in items
                                             if r.kind not in ("clarification", "same_term", "condition_route")))
            if "clarification" in kinds:
                msg = (msg or f"{course}:") + (" (Prerequisite grouping from a user-supplied clarification, not "
                                               "independently verified.)")
            extra = [r.message for r in items if r.kind == "same_term"]
            text = " ".join(x for x in [msg.strip(), *dict.fromkeys(extra)] if x)
            if text:
                self.note("prerequisite", text, course)
        unconfirmed = sorted(set(resolved.unconfirmed_year) & in_plan)
        if unconfirmed:
            self.note("prerequisite", (
                f"Prerequisites for {', '.join(unconfirmed)} come from cited college sources but aren't confirmed "
                f"against the current catalog year."))
        added = sorted(c for c in resolved.added if c in in_plan)
        if added:
            self.note("prerequisite", "Added because a planned course lists it as a prerequisite: "
                      + ", ".join(f"{c} (for {resolved.added[c]})" for c in added) + ". Already met some other "
                      "way (placement or other coursework)? Confirm with a counselor: the loaded prerequisite data "
                      "names only these courses.")

    def _source_notes(self) -> None:
        if self.catalog and year_status(self.catalog.academic_year, self.wanted) == "older_year":
            self.note("data_year", f"Course titles and units come from the {self.catalog.title}; a {self.wanted} "
                                   f"catalog isn't loaded.")
        if self.ge_ds and year_status(self.ge_ds.academic_year, self.wanted) == "older_year":
            self.note("data_year", f"GE courses come from {self.ge_ds.institution_name}'s {self.ge_ds.academic_year} "
                                   f"{self.ge_ds.display_name} list; the {self.wanted} list isn't loaded, so check "
                                   f"ASSIST for changes.")

    def _data_sources(self) -> dict:
        p, cat, ds = self.pathway, self.catalog, self.ge_ds
        college_ap = self.data.ap.college.get(self.college_id)
        campus_ap = self.data.ap.campuses.get(self.university_id)
        return {
            "articulation": {"status": "loaded", "file": p.file, "dataset_id": p.dataset_id,
                             "academic_year": p.academic_year, "requested_academic_year": self.match.academic_year,
                             "year_status": year_status(p.academic_year, self.match.academic_year)},
            "catalog": ({"status": "loaded", "file": cat.file, "academic_year": cat.academic_year,
                         "year_status": year_status(cat.academic_year, self.wanted)} if cat else {"status": "not_loaded"}),
            "prerequisites": ({"status": "loaded", "file": self.prereg.file, "reviewed_on": self.prereg.reviewed_on}
                              if self.prereg else {"status": "not_loaded"}),
            "ge": ({"status": "loaded", "pathway": self.ge_pid, "file": ds.file, "dataset_id": ds.dataset_id,
                    "academic_year": ds.academic_year, "year_status": year_status(ds.academic_year, self.wanted)}
                   if ds else {"status": "loaded" if self.ge_pid == UC_SEVEN and self.data.ge.uc_seven else "not_loaded",
                               "pathway": self.ge_pid}),
            "ap": {"college_chart": ({"status": "loaded", "file": college_ap.file, "academic_year": college_ap.academic_year}
                                     if college_ap else {"status": "not_loaded"}),
                   "target_campus_chart": ({"status": "loaded", "campus": campus_ap.name,
                                            "source_period": campus_ap.source_period}
                                           if campus_ap else {"status": "not_loaded"})},
        }

    def _admission_requirements(self) -> dict | None:
        """UC 7-course pattern: UC targets only (a UC admission requirement, not a GE certification)."""
        if self.system != "UC":
            return None
        uc = self.data.ge.uc_seven
        if uc is None:
            return {"uc_seven_course": {"status": "not_loaded"}}
        specs = []
        for r in uc.requirements:
            n = int(r.get("minimum_courses") or 1)
            specs += [SlotSpec(f"{r['id']}#{i + 1}", r["id"], r["id"], r["name"], r["name"], frozenset(), r.get("rule"),
                               r.get("minimum_semester_units_each"), False, int(r.get("minimum_distinct_areas") or 0))
                      for i in range(n)]
        ap_items = [GEItem("ap_exam", e["exam_id"], e["exam_name"], (uc.ap_exams.get(e["exam_id"]) or ["?"])[0],
                           tuple((r["id"], False) for a in uc.ap_exams.get(e["exam_id"], [])
                                 for r in uc.requirements if a in (r.get("accepts") or [])))
                    for e in self.ap_evals if e.get("exam_id") and e["score"] >= uc.ap_min_score]
        states, _ = match_items(specs, False, ap_items)
        rows = []
        for r in uc.requirements:
            mine = [s for s in states if s.spec.requirement_id == r["id"]]
            by_ap = [s.satisfied_by.label for s in mine if s.satisfied_by]
            rows.append({"id": r["id"], "name": r["name"], "minimum_courses": len(mine), "rule": r.get("rule"),
                         "satisfied_by_ap": by_ap, "remaining_courses": len(mine) - len(by_ap),
                         "status": "met_by_ap" if len(by_ap) == len(mine) else "unknown_course_list"})
        return {"uc_seven_course": {
            "status": "evaluated", "type": "uc_transfer_admission_requirement", "applies_to": "UC targets",
            "dataset_id": uc.dataset_id, "requirements": rows, "course_list": "not_loaded",
            "general_rules": uc.general_rules,
            "message": ("A UC admission requirement (not GE). AP exams are counted from the loaded chart; which "
                        f"{self.pathway.college} courses count isn't in the loaded data, so check ASSIST.")}}


def plan_request(req, match, data: DataRegistry) -> dict:
    return Planner(req, match, data).run()


def ge_options(data: DataRegistry, institution: str, pathway: str, requirement_id: str, academic_year: str | None,
               lab: bool = False) -> dict:
    """GE detail panel: the courses that can fill one requirement, from the college's GE list + catalog."""
    pid = pathway_id(pathway) or pathway
    inst = data.resolve(institution)
    if pid == UC_SEVEN:
        return {"status": "not_loaded", "pathway": pid, "requirement_id": requirement_id, "options": [],
                "message": "No college course list for the UC 7-course pattern is loaded; ASSIST marks eligible "
                           "courses UC-E, UC-M, UC-H, UC-B and UC-S."}
    ds = data.ge.college_list(inst, pid, academic_year)
    if ds is None:
        return {"status": "not_loaded", "pathway": pid, "requirement_id": requirement_id, "options": [],
                "message": "No GE course list for this college is loaded for that academic year."}
    found = ds.subarea(requirement_id)
    lab_info = ds.lab
    if found is None:
        area = next((a for a in ds.areas if str(a.get("id")) == requirement_id), None)
        if area is None:
            return {"status": "unknown_requirement", "pathway": pid, "requirement_id": requirement_id, "options": [],
                    "message": f"{requirement_id} isn't a requirement in {ds.display_name}."}
        found = (area, {"id": requirement_id, "name": area.get("name"),
                        "eligible_course_codes": [c for s in area.get("subareas") or [] for c in s.get("eligible_course_codes") or []]})
    area, sub = found
    keys = [code_key(c) for c in sub.get("eligible_course_codes") or []]
    if lab and lab_info and sub.get("id") != lab_info["subarea_id"]:
        keys = [k for k in keys if k in lab_info["eligible"]]
    catalog = data.catalogs.for_institution(inst, academic_year)
    options = []
    for k in dict.fromkeys(keys):
        g = ds.courses.get(k)
        c = catalog.get(k) if catalog else None
        code = (g.code if g else c.code if c else split_code(k))
        other = [a for a in (g.areas if g else ()) if a != sub.get("id")]
        options.append({"code": code, "title": (g.title if g else c.title if c else ""),
                        "units": g.units if g and g.units else (c.units if c else None),
                        "also_counts_for": other, "same_as": list(g.same_as) if g else [],
                        "prerequisite": (g.prerequisite if g and g.prerequisite else (c.prerequisite_text if c else None)),
                        "in_catalog": c is not None,
                        "includes_lab": bool(lab_info and k in lab_info["eligible"])})
    options.sort(key=lambda o: (o["code"].split()[0], _num(o["code"])))
    return {"status": "loaded", "pathway": pid, "display_name": ds.display_name, "institution_id": inst,
            "institution": ds.institution_name, "requirement_id": sub.get("id"), "name": sub.get("name"),
            "area_id": str(area.get("id")), "area_name": area.get("name"), "rule": area.get("rule"),
            "lab_required": bool(lab), "note": sub.get("note"),
            "minimum_disciplines": area.get("minimum_disciplines"),
            "dataset_id": ds.dataset_id, "academic_year": ds.academic_year, "requested_academic_year": academic_year,
            "year_status": year_status(ds.academic_year, academic_year), "options": options}


def _num(code: str) -> tuple:
    import re
    m = re.search(r"(\d+)", code)
    return (int(m[1]) if m else 0, code)


__all__ = ["PlanError", "ge_options", "node_id", "plan_request"]
