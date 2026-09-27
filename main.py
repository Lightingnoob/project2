"""
TransferPath SEP Planner — API + static frontend.

Run:   uvicorn main:app --reload
App:   http://127.0.0.1:8000/          (index.html, served by this app)
Docs:  http://127.0.0.1:8000/docs

DATA (loaded once at startup, never per request):
  data/articulation/   ASSIST agreements -> PathwayStore (articulation package); GET /pathways
  data/catalogs/       course catalogs    -> CatalogStore (GET /courses/search, /courses/resolve)
  data/prerequisites/  prerequisite rules -> PrerequisiteStore (scheduling order)
  data/ge/             Cal-GETC lists, UC 7-course -> GEStore (GET /ge/options)
  data/ap/             college AP waivers, campus AP charts -> APStore (GET /ap/exams, POST /ap/evaluate)
  data/reference/      audit only, never used for planning
GET /data-status shows what loaded. Real pathways are planned by planner.py; any other combination returns
404 ("N/A - ... not uploaded yet"). For demos, POST /dev/mock-data adds one mock Golden Pathway
(Chabot College -> UC Berkeley -> Computer Science, CAL-GETC and 7-Course Pattern), marked source "mock".

Test:  POST /generate-sep
       {"college": "Chabot College", "university": "Cal State East Bay", "major": "Computer Science",
        "ge_pathway": "CAL-GETC", "completed_courses": ["MTH 1"], "ap_scores": [{"subject": "AP Calculus BC",
        "score": 5}], "start_term": "Fall 2026", "include_summer": false}

The response models below validate every payload's internal consistency (graph
references, critical path chain, unit totals and caps, real consecutive terms,
plans ending in Spring), so a bad payload fails loudly with a 500 instead of
breaking Cytoscape in the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from articulation import ARTICULATION_DIR, PathwayStore
from datastores import DataRegistry, university_system
from datastores.common import academic_year_of, code_key
from planner import PlanError, ge_options, plan_request
from sep_engine import SEPError, StudentProfile, generate_sep
from sep_engine import mock_data
from sep_engine.scheduler import parse_term_label, term_sequence
from tag_engine import (
    EntryTerm, TagDatasetRef, TagEvaluation, TagGeStatus, TagStudentProfile, TermRef, campus_directory, evaluate_tag,
    load_affiliations, load_registry,
)

GEPathway = Literal["CAL-GETC", "7-Course Pattern"]
CourseType = Literal["major_prep", "prerequisite", "ge_slot", "ge_course"]
Season = Literal["Fall", "Spring", "Summer"]
NOT_UPLOADED = "N/A - Transfer data for this specific pathway has not been uploaded yet."


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =====================================================================
# REQUEST
# =====================================================================
class APScore(StrictModel):
    subject: str = Field(examples=["AP Calculus BC"])
    score: int = Field(ge=1, le=5, examples=[5])


class GenerateSEPRequest(StrictModel):
    college: str = Field(examples=["Chabot College"])
    university: str = Field(examples=["UC Berkeley"])
    major: str = Field(examples=["Computer Science"])
    ge_pathway: GEPathway = "CAL-GETC"
    completed_courses: list[str] = Field(default_factory=list, examples=[["CS 1", "ENGL 1A"]])
    ap_scores: list[APScore] = Field(
        default_factory=list,
        description="AP exams. The college's AP chart decides course waivers and GE areas (applied to the plan); the "
                    "target campus's AP chart is reported separately and never applied (mock data ignores AP).")
    tag_university: str | None = Field(
        default=None, description="Optional TAG (Transfer Admission Guarantee) campus: an id from GET /tag/campuses "
                                  "('riverside') or its name ('UC Riverside'). Independent of `university`.",
        examples=["riverside"])
    start_term: str = Field(default="Fall 2026", pattern=r"^(Fall|Spring|Summer) \d{4}$",
                            description="First term of the plan.", examples=["Fall 2026"])
    include_summer: bool = Field(default=False, description="Allow Summer terms (max 9 units, GE first).")

    @model_validator(mode="after")
    def _summer_start_needs_summer(self) -> GenerateSEPRequest:
        if self.start_term.startswith("Summer") and not self.include_summer:
            raise ValueError("a Summer start_term requires include_summer=true")
        return self

    @model_validator(mode="after")
    def _blank_tag_is_none(self) -> GenerateSEPRequest:
        if self.tag_university is not None and self.tag_university.strip() in ("", "None"):
            self.tag_university = None
        return self


# =====================================================================
# RESPONSE — graph (Cytoscape.js `elements` format: { nodes: [...], edges: [...] })
# =====================================================================
class NodeData(StrictModel):
    id: str = Field(description="Cytoscape id; course code without spaces, e.g. 'MATH1'.",
                    pattern=r"^[A-Za-z0-9_]+$", examples=["MATH1"])
    label: str = Field(description="Course code shown on the node.", examples=["MATH 1"])
    title: str = Field(examples=["Calculus I"])
    units: float = Field(gt=0)
    is_ge: bool = Field(description="True for GE courses, False for major prep (and its prerequisites).")
    status: Literal["completed", "planned"]
    term: str | None = Field(description="Planned term, e.g. 'Spring 2027'; null when completed.")
    satisfies: list[str] = Field(default_factory=list, description="Requirements this course counts toward.")
    type: CourseType | None = Field(default=None, description="What the node is (see PlannedCourse.type).")


class CyNode(StrictModel):
    data: NodeData


class EdgeData(StrictModel):
    id: str = Field(description="'e_<source>_<target>'", examples=["e_MATH1_MATH2"])
    source: str = Field(description="Prerequisite / co-requisite course id.")
    target: str = Field(description="Course that requires `source`.")
    relation: Literal["prereq", "coreq"] = Field(
        default="prereq", description="prereq: source in an earlier term. coreq: same term or earlier.")


class CyEdge(StrictModel):
    data: EdgeData


class GraphElements(StrictModel):
    nodes: list[CyNode]
    edges: list[CyEdge]


# =====================================================================
# RESPONSE — term timeline (accordion cards) + requirements
# =====================================================================
class PlannedCourse(StrictModel):
    id: str = Field(description="Matches a graph node id.")
    code: str
    title: str
    units: float = Field(gt=0)
    is_ge: bool
    satisfies: list[str] = Field(default_factory=list)
    type: CourseType = Field(default="major_prep", description=(
        "major_prep: meets an articulation requirement; prerequisite: needed only because a planned course "
        "requires it; ge_slot: one GE requirement (pick any listed course, see `ge`); ge_course: a real course "
        "planned only for GE."))
    ge: dict | None = Field(default=None, description="ge_slot only: {pathway, requirement_id, slot_id, name, "
                                                      "area_id, lab_required, institution_id, academic_year, ...}.")


class Semester(StrictModel):
    index: int = Field(ge=1, description="1-based position in the plan.")
    term: str = Field(description="Real term name.", examples=["Fall 2026"])
    season: Season
    year: int
    units: float = Field(ge=0)
    max_units: float = Field(gt=0, description="Hard unit cap for this term (lower in Summer).")
    target_units: float = Field(ge=0, description="Load-balancing target for this term (0 when not balanced).")
    is_padding: bool = Field(description="Empty final Spring added so the plan ends in Spring.")
    courses: list[PlannedCourse]

    @model_validator(mode="after")
    def _consistent(self) -> Semester:
        if self.term != f"{self.season} {self.year}":
            raise ValueError(f"term {self.term!r} does not match season/year")
        total = sum(c.units for c in self.courses)
        if abs(total - self.units) > 1e-6:
            raise ValueError(f"{self.term}: units={self.units} but courses sum to {total}")
        if self.units > self.max_units + 1e-6:
            raise ValueError(f"{self.term}: {self.units} units exceeds the {self.max_units}-unit cap")
        if self.is_padding and self.courses:
            raise ValueError(f"{self.term}: a padding term cannot contain courses")
        return self


class RequirementCourse(StrictModel):
    id: str
    code: str
    title: str
    kind: Literal["major", "ge"] = Field(description="'major' when the course is (also) major prep.")
    status: Literal["completed", "planned"]
    term: str | None


class Requirement(StrictModel):
    id: str
    name: str
    category: Literal["major", "ge"]
    choose: int = Field(ge=0)
    courses: list[RequirementCourse]

    @model_validator(mode="after")
    def _complete(self) -> Requirement:
        if len(self.courses) != self.choose:
            raise ValueError(f"requirement {self.id!r} lists {len(self.courses)} courses but needs {self.choose}")
        return self


class PlanSummary(StrictModel):
    terms_needed: int = Field(ge=0)
    min_terms_required: int = Field(ge=0, description="Lower bound from prereq chain and unit capacity.")
    prereq_chain_length: int = Field(ge=0)
    unit_load_terms: int = Field(ge=0, description="ceil(total units / regular-term cap).")
    total_units: float = Field(ge=0)
    transfer_admission_term: str | None = Field(examples=["Fall 2028"])
    schedule_proven_minimal: bool
    major_prep_in_summer: list[str] = Field(description="Major prep that could only fit in Summer.")
    target_units_per_term: float = Field(ge=0, description="Target_Units_Per_Term for Fall/Spring terms.")
    min_units_regular_term: float = Field(ge=0, description="Lightest Fall/Spring term in this plan.")
    max_units_regular_term: float = Field(ge=0, description="Heaviest Fall/Spring term in this plan.")


class SemesterPlan(StrictModel):
    id: str = Field(examples=["A"])
    name: str = Field(examples=["Plan A"])
    label: str = Field(examples=["Fastest Route"])
    description: str
    kind: Literal["recommended", "fast", "balanced"] = Field(
        description="recommended = the single plan for a normal workload; fast / balanced = 2- and 3-year tracks.")
    selection_metric: Literal["units", "difficulty"]
    max_units_regular: float = Field(gt=0)
    max_units_summer: float = Field(gt=0)
    summary: PlanSummary
    graph: GraphElements
    critical_path: list[str] = Field(description="Node ids in chain order; highlight these in red.")
    critical_path_edges: list[str] = Field(description="Edge ids joining consecutive critical_path nodes.")
    requirements: list[Requirement]
    semesters: list[Semester]
    ge: dict | None = Field(default=None, description="GE status for this plan: every requirement of the chosen "
                                                      "GE pattern and what satisfies it (null when GE isn't planned).")

    @model_validator(mode="after")
    def _consistent(self) -> SemesterPlan:
        where = f"Plan {self.id}"
        nodes = {n.data.id: n.data for n in self.graph.nodes}
        edges = {e.data.id: e.data for e in self.graph.edges}
        if len(nodes) != len(self.graph.nodes) or len(edges) != len(self.graph.edges):
            raise ValueError(f"{where}: duplicate node or edge ids")
        for e in edges.values():
            if e.source not in nodes or e.target not in nodes:
                raise ValueError(f"{where}: edge {e.id} references a missing node")

        if any(n not in nodes for n in self.critical_path):
            raise ValueError(f"{where}: critical_path references missing nodes")
        expected = [f"e_{a}_{b}" for a, b in zip(self.critical_path, self.critical_path[1:])]
        if self.critical_path_edges != expected or any(e not in edges for e in expected):
            raise ValueError(f"{where}: critical_path must be a connected chain; edges should be {expected}")

        if self.summary.terms_needed != len(self.semesters):
            raise ValueError(f"{where}: terms_needed does not match semesters")
        if [s.index for s in self.semesters] != list(range(1, len(self.semesters) + 1)):
            raise ValueError(f"{where}: semester indexes must be 1..n in order")
        if abs(sum(s.units for s in self.semesters) - self.summary.total_units) > 1e-6:
            raise ValueError(f"{where}: total_units does not match the semesters")
        for s in self.semesters:
            cap = self.max_units_summer if s.season == "Summer" else self.max_units_regular
            if s.max_units != cap:
                raise ValueError(f"{where}: {s.term} cap should be {cap}")
        if self.semesters:
            last = self.semesters[-1]
            if last.season != "Spring":
                raise ValueError(f"{where}: plans must end in a Spring term (ends {last.term})")
            if self.summary.transfer_admission_term != f"Fall {last.year}":
                raise ValueError(f"{where}: transfer_admission_term should be Fall {last.year}")

        planned = {i for i, d in nodes.items() if d.status == "planned"}
        term_index = {c.id: s.index for s in self.semesters for c in s.courses}
        scheduled = [c.id for s in self.semesters for c in s.courses]
        if len(scheduled) != len(set(scheduled)) or set(scheduled) != planned:
            raise ValueError(f"{where}: every planned node must be scheduled exactly once")
        term_label = {c.id: s.term for s in self.semesters for c in s.courses}
        for i, d in nodes.items():
            if d.term != term_label.get(i):
                raise ValueError(f"{where}: node {i} term {d.term!r} does not match the timeline")
        for e in edges.values():
            if e.source in term_index and e.target in term_index:
                a, b = term_index[e.source], term_index[e.target]
                if (e.relation == "prereq" and not a < b) or (e.relation == "coreq" and not a <= b):
                    raise ValueError(f"{where}: violates {e.relation} {e.source} -> {e.target}")
        return self


# =====================================================================
# RESPONSE — top level
# =====================================================================
class PathwayInfo(StrictModel):
    college: str
    university: str
    major: str
    degree: str
    ge_pathway: GEPathway
    start_term: str
    include_summer: bool
    completed_courses: list[str]


class TagPlanEvaluation(StrictModel):
    """TAG for the plans that transfer in one term (the entry term decides which rules apply)."""
    plan_ids: list[str] = Field(min_length=1, examples=[["A"]])
    evaluation: TagEvaluation


class Workload(StrictModel):
    """Why the response has one plan or two."""
    total_required_units: float = Field(ge=0, description="Major prep + GE still to take.")
    two_year_semesters: int = Field(ge=1, description="Fall/Spring terms in a 2-year, Spring-ending plan.")
    two_year_average_units: float = Field(ge=0, description="Average units per semester to finish in 2 years.")
    normal_load_max: float = Field(description="Averages above this count as a heavy workload.", examples=[15])
    classification: Literal["normal", "heavy"]
    plan_count: int = Field(ge=1, le=2)
    explanation: str
    notes: list[str] = Field(default_factory=list)


class ArticulationInfo(StrictModel):
    """Where the plan's major preparation comes from (the articulation agreement, or demo mock data)."""
    model_config = ConfigDict(extra="allow")        # ASSIST agreements add document/requirement details
    source: Literal["assist", "mock"] = Field(description="assist: a loaded data/articulation file; mock: demo data.")
    dataset_id: str | None = None
    file: str | None = Field(default=None, examples=["chabot_csueb_computer_science_2026_2027.json"])
    publisher: str | None = Field(default=None, examples=["ASSIST"])
    url: str | None = None
    retrieved_on: str | None = None
    academic_year: str | None = Field(default=None, description="Year of the agreement used.", examples=["2026-2027"])
    requested_academic_year: str | None = Field(default=None, description="Year the start term falls in.")
    degree: str | None = Field(default=None, examples=["B.S."])
    not_articulated: list[str] = Field(default_factory=list,
                                       description="University requirements with no course to take at the college.")
    prerequisites_enforced: bool = Field(description="False when the data lists no prerequisites.")


class SEPResponse(StrictModel):
    pathway: PathwayInfo
    articulation: ArticulationInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    workload: Workload
    notes: list[dict] = Field(default_factory=list, description="Structured plan notes {kind, message}; `warnings` "
                                                                "holds the same messages as plain text.")
    remaining_after_transfer: list[dict] = Field(
        default_factory=list, description="University requirements with no college course articulated: completed "
                                          "after transfer, never scheduled at the college.")
    data_sources: dict | None = Field(default=None, description="Datasets and academic years this plan used.")
    ap_evaluations: list[dict] = Field(default_factory=list, description=(
        "Per AP exam: community_college_effect, ge_effect and target_campus_effect, evaluated separately."))
    completed_courses_normalized: list[dict] = Field(default_factory=list)
    review_items: list[dict] = Field(default_factory=list, description="Rules that need a counselor's review.")
    admission_requirements: dict | None = Field(default=None, description="UC targets only: the UC 7-course "
                                                                          "pattern (an admission requirement).")
    tag_evaluations: list[TagPlanEvaluation] | None = Field(
        default=None, description="TAG (tag_engine), one entry per distinct transfer term among the plans. "
                                  "null when no TAG campus was selected.")
    plans: list[SemesterPlan]

    @model_validator(mode="after")
    def _tag_plans_exist(self) -> SEPResponse:
        tagged = [pid for t in self.tag_evaluations or () for pid in t.plan_ids]
        if len(tagged) != len(set(tagged)) or not set(tagged) <= {p.id for p in self.plans}:
            raise ValueError(f"tag_evaluations plan_ids {tagged} must be distinct ids of returned plans")
        return self

    @model_validator(mode="after")
    def _real_calendar(self) -> SEPResponse:
        season, year = parse_term_label(self.pathway.start_term)
        for plan in self.plans:
            expected = [s.label for s in term_sequence(season, year, len(plan.semesters), self.pathway.include_summer)]
            actual = [s.term for s in plan.semesters]
            if actual != expected:
                raise ValueError(f"Plan {plan.id}: terms {actual} are not the consecutive terms "
                                 f"from {self.pathway.start_term} (summer {'on' if self.pathway.include_summer else 'off'})")
        if self.workload.plan_count != len(self.plans):
            raise ValueError(f"workload.plan_count={self.workload.plan_count} but {len(self.plans)} plans returned")
        if len({p.id for p in self.plans}) != len(self.plans):
            raise ValueError("plan ids must be unique")
        return self


class CatalogEntry(StrictModel):
    title: str
    units: float


class MockDataStatus(StrictModel):
    loaded: bool
    pathway: dict[str, str] | None = None
    suggested_completed: list[str] = Field(default_factory=list)


class TagCampusInfo(StrictModel):
    id: str = Field(examples=["riverside"])
    name: str = Field(examples=["UC Riverside"])
    tag_url: str | None = None
    entry_terms: list[TermRef] = Field(description="Entry terms the loaded TAG data has rules for.")


class TagCampusesResponse(StrictModel):
    """The TAG University options, from the TAG datasets (not hardcoded)."""
    campuses: list[TagCampusInfo]
    datasets: list[TagDatasetRef]


class TagEvaluateRequest(StrictModel):
    campus: str | None = Field(description="Campus id or name; null / 'None' = no TAG campus.", examples=["riverside"])
    entry_term: str = Field(description="Entry term: 'fall_2027' or 'Fall 2027'.", examples=["fall_2027"])
    student: TagStudentProfile = Field(default_factory=TagStudentProfile)

    @field_validator("entry_term")
    @classmethod
    def _valid_term(cls, v: str) -> str:
        EntryTerm.parse(v)               # ValueError -> 422
        return v


# =====================================================================
# DATA STORE — the articulation files in data/articulation/ (articulation package), loaded at startup.
# Mock data is only added by the dev injector (POST /dev/mock-data).
# =====================================================================
STORE = PathwayStore()
STORE.load_directory(ARTICULATION_DIR)
# Catalog, prerequisites, GE and AP: loaded and indexed once (datastores package)
DATA = DataRegistry().load(STORE)

# TAG rules: every data/uc_tag_requirements_*.json (tag_engine). A broken file is logged and skipped.
TAG_REGISTRY = load_registry()
# Which school/college offers each major at a TAG campus (data/uc_major_affiliations.json), for the TAG rules
# set by school or college. A broken file is logged; those rules then need review.
MAJOR_AFFILIATIONS = load_affiliations()

# =====================================================================
# APP
# =====================================================================
app = FastAPI(title="TransferPath SEP Planner API", version="0.2.0")

# Hackathon setting: allow any origin so the frontend also works from file:// or another dev server.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_FILES = ("/", "/api.js", "/ui.js", "/focus_card.js", "/plan_graph.js")


@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """Without a Cache-Control header, browsers may reuse a cached index.html / ui.js / CSS for hours, so
    frontend edits don't show up. no-cache = check with the server every load (cheap 304 when unchanged)."""
    response = await call_next(request)
    if request.url.path in FRONTEND_FILES or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
def health() -> dict:
    """Liveness check: if http://127.0.0.1:8000/health answers, the server is running."""
    return {"status": "ok"}


ROOT = Path(__file__).parent


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/api.js", include_in_schema=False)
def api_js() -> FileResponse:
    return FileResponse(ROOT / "api.js", media_type="text/javascript")


@app.get("/ui.js", include_in_schema=False)
def ui_js() -> FileResponse:
    return FileResponse(ROOT / "ui.js", media_type="text/javascript")


@app.get("/focus_card.js", include_in_schema=False)
def focus_card_js() -> FileResponse:
    return FileResponse(ROOT / "focus_card.js", media_type="text/javascript")


@app.get("/plan_graph.js", include_in_schema=False)
def plan_graph_js() -> FileResponse:
    return FileResponse(ROOT / "plan_graph.js", media_type="text/javascript")


# Prebuilt Tailwind CSS + self-hosted fonts and icons
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


@app.get("/institutions")
def institutions() -> dict:
    """Dropdown options from the loaded data (no hardcoded lists): colleges any dataset covers, universities any
    articulation or AP dataset names, and the majors that have articulation data."""
    listing = STORE.listing()["available"]
    real = [p for p in listing if p["source"] != "mock"]
    colleges = [{"id": i, "name": n, "has_articulation": any(p["college_id"] == i for p in real),
                 "has_catalog": bool(DATA.catalogs.catalogs.get(i)),
                 "has_ge": any(k[0] == i for k in DATA.ge.college)}
                for i, n in sorted(DATA.colleges.items(), key=lambda x: x[1])]
    universities = [{"id": i, "name": n, "system": university_system(n),
                     "has_articulation": any(p["university_id"] == i for p in real)}
                    for i, n in sorted(DATA.universities.items(), key=lambda x: (university_system(x[1]) or "~", x[1]))]
    known = {u["id"] for u in universities}
    for p in listing:                                    # mock pathways (dev injector) are listed too
        if p["source"] == "mock" and DATA.resolve(p["university"]) not in known:
            known.add(DATA.resolve(p["university"]))
            universities.append({"id": p["university"], "name": p["university"],
                                 "system": university_system(p["university"]), "has_articulation": True, "mock": True})
    return {"colleges": colleges, "universities": universities, "majors": sorted({p["major"] for p in listing})}


@app.get("/majors")
def majors(college: str, university: str) -> dict:
    """Majors with articulation data for this college + university (mock pathways marked)."""
    rows = []
    for p in STORE.pathways:
        if STORE.registry.same(p.college_ref, college) and STORE.registry.same(p.university_ref, university):
            rows.append({"major": p.major, "degree": p.degree, "academic_year": p.academic_year,
                         "label": f"{p.major}, {p.degree}" if p.degree else p.major, "source": p.source,
                         "concentration": p.assist.concentration if p.assist else None, "file": p.file})
    rows = list({(r["label"], r["academic_year"], r["source"]): r for r in rows}.values())   # mock: one per GE pattern
    rows.sort(key=lambda r: (r["major"], r["degree"] or "", r["academic_year"] or ""))
    return {"college": college, "university": university, "majors": rows,
            "unsupported": [{"major": u.major, "degree": u.degree, "reason": u.reason} for u in STORE.unsupported
                            if STORE.registry.same(u.college_ref, college)
                            and STORE.registry.same(u.university_ref, university)]}


# --- Course catalog ------------------------------------------------------------
def _catalog_or_404(institution: str, academic_year: str | None = None):
    cat = DATA.catalogs.for_institution(DATA.resolve(institution), academic_year)
    if cat is None:
        raise HTTPException(404, f"No course catalog is loaded for {institution}.")
    return cat


@app.get("/courses/search", tags=["catalog"])
def courses_search(institution: str, q: str = "", limit: int = 20, academic_year: str | None = None) -> dict:
    """Completed-courses autocomplete: compact {code, title, units} matches from the college catalog."""
    cat = _catalog_or_404(institution, academic_year)
    return {"institution_id": cat.institution_id, "catalog": cat.title, "academic_year": cat.academic_year,
            "results": cat.search(q, limit=max(1, min(limit, 50)))}


@app.get("/courses/resolve", tags=["catalog"])
def courses_resolve(institution: str, code: str) -> dict:
    """Canonical catalog code for any spelling ('mth1' -> 'MTH 1', former 'ENGL 1' -> 'ENGL C1000'); 404 if unknown."""
    cat = _catalog_or_404(institution)
    hit = cat.resolve(code)
    if hit is None:
        raise HTTPException(404, f"{code} isn't in the {cat.title}.")
    return {**hit[0].compact(), "matched_by": hit[1], "catalog": cat.title}


@app.get("/courses/detail", tags=["catalog"])
def courses_detail(institution: str, code: str) -> dict:
    """Catalog description and requisite text, plus the structured prerequisite record when the registry has one."""
    cat = _catalog_or_404(institution)
    hit = cat.resolve(code)
    if hit is None:
        raise HTTPException(404, f"{code} isn't in the {cat.title}.")
    reg = DATA.prereqs.get(cat.institution_id)
    rec = reg.records.get(code_key(hit[0].code)) if reg else None
    return {**hit[0].detail(), "catalog": cat.title, "academic_year": cat.academic_year,
            "prerequisite_record": ({"status": rec.status, "summary": rec.summary, "requirement": rec.requirement,
                                     "advisory": list(rec.advisory), "current_year_confirmed": rec.current_year_confirmed}
                                    if rec else {"status": "not_loaded"})}


# --- GE -------------------------------------------------------------------------
@app.get("/ge/options", tags=["ge"])
def ge_requirement_options(institution: str, requirement_id: str, pathway: str = "cal_getc",
                           academic_year: str | None = None, start_term: str | None = None, lab: bool = False) -> dict:
    """The courses that can fill one GE requirement (a plan's GE slot), from the college's own GE list for that
    academic year + its catalog. `lab=true` keeps only courses that also carry the lab requirement."""
    return ge_options(DATA, institution, pathway, requirement_id, academic_year or academic_year_of(start_term), lab)


# --- AP --------------------------------------------------------------------------
class APEvaluateRequest(StrictModel):
    college: str = Field(examples=["Chabot College"])
    university: str | None = Field(default=None, examples=["Cal State East Bay"])
    ap_scores: list[APScore]


@app.get("/ap/exams", tags=["ap"])
def ap_exams() -> dict:
    """AP subjects from the loaded AP data (for the AP subject dropdown)."""
    return {"exams": DATA.ap.exam_list()}


@app.post("/ap/evaluate", tags=["ap"])
def ap_evaluate(request: APEvaluateRequest) -> dict:
    """Each exam's college effect (course waiver + GE areas) and target-campus effect, kept separate. The plan
    decides which choose-one course applies; here a choose-one waiver lists its options."""
    college = DATA.resolve(request.college)
    uni = DATA.resolve(request.university) if request.university else None
    out = []
    for a in request.ap_scores:
        exam = DATA.ap.normalize_exam(a.subject)
        if exam is None:
            out.append({"subject": a.subject, "score": a.score, "exam_id": None, "status": "unknown_exam"})
            continue
        cc = DATA.ap.community_college_effect(college, exam, a.score, relevant=None)
        listed = cc.get("ge_areas_text") and cc["status"] not in ("below_minimum", "not_listed", "not_loaded")
        out.append({"subject": a.subject, "score": a.score, "exam_id": exam, "exam_name": DATA.ap.exams.get(exam),
                    "status": "evaluated", "community_college_effect": cc,
                    "ge_effect": {"status": "listed" if listed else "none_listed", "areas_text": cc.get("ge_areas_text")},
                    "target_campus_effect": (DATA.ap.target_campus_effect(uni, exam, a.score) if uni
                                             else {"status": "not_applicable", "rules": []})})
    return {"college_id": college, "university_id": uni, "evaluations": out}


# --- Diagnostics -------------------------------------------------------------------
@app.get("/data-status", tags=["pathways"])
def data_status() -> dict:
    """What the application actually loaded (datasets, academic years, what was skipped and why)."""
    return DATA.status(STORE, TAG_REGISTRY, MAJOR_AFFILIATIONS)


@app.get("/colleges/{college}/catalog", response_model=dict[str, CatalogEntry],
         responses={404: {"description": "No catalog uploaded for this college."}})
def college_catalog(college: str) -> dict:
    catalog = STORE.catalog_for(college)
    if catalog is None:
        raise HTTPException(404, f"No course catalog has been uploaded for {college}.")
    return {code: {"title": c.title, "units": c.units} for code, c in catalog.items()}


@app.post("/generate-sep", response_model=SEPResponse,
          responses={404: {"description": NOT_UPLOADED},
                     422: {"description": "Invalid request, infeasible plan, or articulation data the planner "
                                          "can't use (the detail says why)."}})
def generate(request: GenerateSEPRequest) -> dict:
    lookup = STORE.lookup(request.college, request.university, request.major, request.ge_pathway, request.start_term)
    if lookup.match is None:
        if lookup.unsupported is not None:
            raise HTTPException(422, lookup.unsupported.message)
        raise HTTPException(404, lookup.reason or NOT_UPLOADED)
    pathway = lookup.match.pathway
    if pathway.source == "mock":
        result = mock_plan(request, lookup.match)
    else:
        try:
            result = plan_request(request, lookup.match, DATA)          # planner.py: every data layer joined
        except PlanError as e:
            raise HTTPException(e.status, str(e)) from e
    result["tag_evaluations"] = tag_evaluations(request, result["plans"])
    return result


def mock_plan(request: GenerateSEPRequest, match) -> dict:
    """The dev mock pathway: its own illustrative catalog and GE groups, planned as before (no real data)."""
    pathway = match.pathway
    season, year = parse_term_label(request.start_term)
    profile = StudentProfile(completed_courses=request.completed_courses, start_term=season,
                             start_year=year, include_summer=request.include_summer)
    try:
        result = generate_sep(pathway.catalog, pathway.agreement_for(request.ge_pathway), profile)
    except SEPError as e:
        raise HTTPException(422, str(e)) from e
    result["warnings"] = match.warnings + result["warnings"] + [
        "Demo mock data: illustrative courses, not real articulation, GE or prerequisite data."]
    result["notes"] = [{"kind": "mock", "message": w} for w in result["warnings"]]
    for plan in result["plans"]:
        for s in plan["semesters"]:
            for c in s["courses"]:
                c["type"] = "ge_course" if c["is_ge"] else "major_prep"
        for n in plan["graph"]["nodes"]:
            n["data"]["type"] = "ge_course" if n["data"]["is_ge"] else "major_prep"
    result["articulation"] = {
        "source": pathway.source, "dataset_id": pathway.dataset_id, "file": pathway.file,
        "publisher": pathway.publisher, "url": pathway.source_url, "retrieved_on": pathway.retrieved_on,
        "academic_year": pathway.academic_year, "requested_academic_year": match.academic_year,
        "degree": pathway.degree, "not_articulated": pathway.not_articulated,
        "prerequisites_enforced": pathway.prerequisites_enforced}
    return result


# --- Articulation data diagnostics -----------------------------------------
@app.get("/pathways", tags=["pathways"])
def pathways() -> dict:
    """What the planner can use: available pathways, loaded agreements it can't plan (with the reason), and
    files in data/articulation/ it couldn't read (with the reason)."""
    return STORE.listing()


@app.post("/pathways/reload", tags=["pathways"])
def reload_pathways() -> dict:
    """Re-read data/articulation/ (after adding or editing a file) without restarting. Mock data is kept."""
    STORE.reload()
    DATA.load(STORE)
    return STORE.listing()


# --- TAG: a separate layer that reads the plans; it never changes them -----
def tag_profile(request: GenerateSEPRequest, plan: dict) -> TagStudentProfile:
    """What the planner knows for TAG: the major, completed courses and GE progress. GPA, units, UC-E/UC-M
    designations and the disqualifying conditions aren't collected yet, so they stay unknown."""
    ge = [c for r in plan["requirements"] if r["category"] == "ge" for c in r["courses"]]
    planned = [c["term"] for c in ge if c["status"] == "planned" and c["term"]]
    block = plan.get("ge")
    if block:                                   # real GE data: every requirement is accounted for
        pending = [r for r in block["requirements"] if r["status"] in ("planned", "major_prep")]
        status = "planned" if pending or planned else "completed"
        if any(r["status"] == "not_planned" for r in block["requirements"]):
            status = None                       # a requirement couldn't be planned: GE completion is unknown
    else:
        status = None if not ge else "completed" if not planned else "planned"
    last = max(planned, key=lambda t: EntryTerm.parse(t)) if planned else None
    return TagStudentProfile(intended_major=request.major, completed_courses=request.completed_courses,
                             ge=TagGeStatus(pattern=request.ge_pathway, status=status, completion_term=last))


def tag_evaluations(request: GenerateSEPRequest, plans: list[dict]) -> list[dict] | None:
    """One TAG evaluation per distinct transfer term (each plan's term is its TAG entry term). None when
    no TAG campus was chosen: TAG is optional and doesn't run."""
    if request.tag_university is None:
        return None
    by_term: dict[str | None, list[dict]] = {}
    for plan in plans:
        by_term.setdefault(plan["summary"]["transfer_admission_term"], []).append(plan)
    return [{"plan_ids": [p["id"] for p in group],
             "evaluation": evaluate_tag(request.tag_university, term, tag_profile(request, group[0]),
                                        registry=TAG_REGISTRY, affiliations=MAJOR_AFFILIATIONS).model_dump()}
            for term, group in by_term.items()]


@app.get("/tag/campuses", response_model=TagCampusesResponse, tags=["tag"])
def tag_campuses() -> dict:
    """TAG University options and the datasets they come from."""
    return {"campuses": campus_directory(TAG_REGISTRY),
            "datasets": [TagDatasetRef.of(d).model_dump() for d in TAG_REGISTRY.datasets]}


@app.post("/tag/evaluate", response_model=TagEvaluation, tags=["tag"])
def tag_evaluate(request: TagEvaluateRequest) -> TagEvaluation:
    """Evaluate TAG for one campus + entry term + student profile, independent of SEP generation."""
    return evaluate_tag(request.campus, request.entry_term, request.student, registry=TAG_REGISTRY,
                        affiliations=MAJOR_AFFILIATIONS)


# --- Dev-only: hackathon data injector ---------------------------------
def _mock_status() -> dict:
    ag = mock_data.MOCK_AGREEMENT
    loaded = any(p.source == "mock" for p in STORE.pathways)
    return {"loaded": loaded,
            "pathway": {"college": ag.college, "university": ag.university, "major": ag.major} if loaded else None,
            "suggested_completed": list(mock_data.MOCK_PROFILE.completed_courses) if loaded else []}


@app.get("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def mock_data_status() -> dict:
    return _mock_status()


@app.post("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def inject_mock_data() -> dict:
    STORE.add_mock(mock_data.MOCK_CATALOG, mock_data.MOCK_AGREEMENTS)
    return _mock_status()


@app.delete("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def clear_mock_data() -> dict:
    """Remove the mock pathway only; loaded articulation data stays."""
    STORE.remove_mock()
    return _mock_status()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
