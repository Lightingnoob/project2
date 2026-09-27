"""
generate_sep(): Dijkstra (pick courses) -> topological sort (order them) -> CSP (place them in terms).

Plans are chosen from the workload (see choose_plans):
  Total Required Units = major prep + GE still to take (the minimum-units Dijkstra selection).
  Average over 2 years = total / (4 Fall/Spring terms, + Summers at half weight if enabled).
    <= 15 units/term -> normal workload: ONE balanced plan (the shortest plan at <= 15 units/term)
    >  15 units/term -> heavy workload: Plan A Fast Track (2 years, ~16-18 units/term) and
                        Plan B Balanced Track (3 years, ~12-14 units/term)
Every plan is load-balanced by the CSP (target = total / terms, soft cap target + 2, hard cap 18).
Returns a JSON-serializable dict in the /generate-sep response shape (Pydantic models in main.py).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import Course, PlanConfig, StudentProfile, Term, TransferAgreement, index_catalog
from .pathway import PathwaySelection, select_pathway
from .scheduler import regular_term_count, schedule_courses, spring_horizon, term_sequence, weighted_terms
from .toposort import chain_metrics, critical_path, topological_sort

NORMAL_LOAD_MAX = 15.0          # units per Fall/Spring term that still count as a normal load
MAX_UNITS = 18                  # hard cap per Fall/Spring term
MAX_SUMMER_UNITS = 9            # hard cap per Summer term
TWO_YEARS = 4                   # Fall/Spring terms
THREE_YEARS = 6

RECOMMENDED_PLAN = PlanConfig(id="A", name="Your Plan", label="Balanced Plan", kind="recommended", weight="units",
                              max_units=MAX_UNITS, max_summer_units=MAX_SUMMER_UNITS, max_avg_load=NORMAL_LOAD_MAX)
FAST_TRACK = PlanConfig(id="A", name="Plan A", label="Fast Track", kind="fast", weight="units",
                        max_units=MAX_UNITS, max_summer_units=MAX_SUMMER_UNITS, min_regular_terms=TWO_YEARS)
BALANCED_TRACK = PlanConfig(id="B", name="Plan B", label="Balanced Track", kind="balanced", weight="units",
                            max_units=MAX_UNITS, max_summer_units=MAX_SUMMER_UNITS, min_regular_terms=THREE_YEARS)


def analyze_workload(total_units: float, profile: StudentProfile) -> dict[str, Any]:
    """Classify the workload by the average load needed to finish in 2 years (4 Fall/Spring terms)."""
    slots = spring_horizon(profile, TWO_YEARS)
    n_regular = regular_term_count(slots)
    average = total_units / weighted_terms(slots, MAX_SUMMER_UNITS / MAX_UNITS) if total_units else 0.0
    heavy = average > NORMAL_LOAD_MAX + 1e-9
    summers = len(slots) - n_regular
    span = f"{n_regular} semesters" + (f" + {summers} summer{'s' if summers != 1 else ''}" if summers else "")
    return {
        "total_required_units": _num(total_units),
        "two_year_semesters": n_regular,
        "two_year_average_units": round(average, 1),
        "normal_load_max": _num(NORMAL_LOAD_MAX),
        "classification": "heavy" if heavy else "normal",
        "plan_count": 2 if heavy else 1,
        "explanation": (
            f"{_num(total_units)} required units is about {average:.1f} units per semester over {span} "
            + (f"(more than {NORMAL_LOAD_MAX:g}): a heavy workload, so there are two plans: a 2-year Fast Track "
               f"and a 3-year Balanced Track." if heavy else
               f"(at most {NORMAL_LOAD_MAX:g}): a normal workload, so there is one balanced plan.")),
        "notes": [],
    }


def choose_plans(workload: dict[str, Any]) -> list[PlanConfig]:
    return [FAST_TRACK, BALANCED_TRACK] if workload["classification"] == "heavy" else [RECOMMENDED_PLAN]


def _num(x: float) -> float | int:
    return int(x) if float(x).is_integer() else round(x, 2)


def node_id(code: str) -> str:
    """Cytoscape-safe id: 'MATH 1' -> 'MATH1'."""
    return re.sub(r"[^A-Za-z0-9_]", "", code)


def generate_sep(
    catalog: Mapping[str, Course] | Iterable[Course],
    agreement: TransferAgreement,
    profile: StudentProfile | None = None,
    plans: Sequence[PlanConfig] | None = None,
) -> dict[str, Any]:
    """
    Build the plans for one College + University + Major agreement: one balanced plan for a normal
    workload, or a Fast Track + Balanced Track for a heavy one (pass `plans` to override).

    Raises a SEPError subclass (CycleError, UnknownCourseError,
    InfeasibleRequirementError, SchedulingError) when the data or the
    constraints make a plan impossible.
    """
    if not isinstance(catalog, Mapping):
        catalog = index_catalog(list(catalog))
    profile = profile or StudentProfile()
    completed = frozenset(profile.completed_courses)
    first = term_sequence(profile.start_term, profile.start_year, 1, profile.include_summer)[0]

    warnings = [f"Completed course '{c}' is not in this college's catalog and was ignored."
                for c in sorted(completed - catalog.keys())]

    # Workload first: Total Required Units from the minimum-units selection (Dijkstra)
    selections: dict[str, PathwaySelection] = {}

    def selection_for(metric: str) -> PathwaySelection:
        if metric not in selections:
            selections[metric] = select_pathway(catalog, agreement, completed, metric)
        return selections[metric]

    total_units = sum(catalog[c].units for c in selection_for("units").courses)
    workload = analyze_workload(total_units, profile)

    built: list[dict[str, Any]] = []
    for cfg in (plans or choose_plans(workload)):
        if cfg.kind == "balanced" and built and built[0]["_regular_terms"] >= cfg.min_regular_terms:
            workload["notes"].append(
                f"The Fast Track already needs {built[0]['_regular_terms']} semesters, so a separate "
                f"{cfg.min_regular_terms}-semester Balanced Track would be the same plan and was skipped.")
            continue
        built.append(_build_plan(catalog, agreement, profile, completed, cfg, selection_for(cfg.weight)))
    if workload["classification"] == "heavy" and built and plans is None:
        fast_terms = built[0]["_regular_terms"]
        if fast_terms > workload["two_year_semesters"]:
            workload["notes"].insert(0, (
                f"Finishing in {workload['two_year_semesters']} semesters isn't possible within "
                f"{MAX_UNITS} units per semester and the prerequisite/term constraints, so the "
                f"Fast Track takes {fast_terms} semesters."))
        if len(built) == 1:
            built[0]["name"] = "Your Plan"          # no Plan B, so don't call it "Plan A"
            workload["explanation"] = (
                f"{workload['total_required_units']} required units is about "
                f"{workload['two_year_average_units']} units per semester over 2 years (more than "
                f"{NORMAL_LOAD_MAX:g}): a heavy workload that doesn't fit in 2 years, so there is one "
                f"plan over {fast_terms} semesters.")
    for plan in built:
        plan.pop("_regular_terms")
    workload["plan_count"] = len(built)

    return {
        "pathway": {
            "college": agreement.college,
            "university": agreement.university,
            "major": agreement.major,
            "degree": agreement.degree or agreement.major,
            "ge_pathway": agreement.ge_pattern,
            "start_term": first.label,
            "include_summer": profile.include_summer,
            "completed_courses": sorted(completed),
        },
        "warnings": warnings,
        "workload": workload,
        "plans": built,
    }


def _describe(cfg: PlanConfig, n_regular: int, n_summer: int, target: float) -> str:
    years = f"{n_regular / 2:g} year{'s' if n_regular != 2 else ''}"
    summers = f" plus {n_summer} summer{'s' if n_summer != 1 else ''}" if n_summer else ""
    pace = f"about {target:.1f} units per semester (never more than {cfg.max_units:g})"
    if cfg.kind == "fast":
        return f"Transfer as early as possible: {years} ({n_regular} semesters{summers}) at {pace}."
    if cfg.kind == "balanced":
        return f"A lighter pace over {years} ({n_regular} semesters{summers}) at {pace}."
    return f"Evenly spread over {n_regular} semesters{summers} at {pace}."


def _build_plan(catalog: Mapping[str, Course], agreement: TransferAgreement, profile: StudentProfile,
                completed: frozenset[str], cfg: PlanConfig, selection: PathwaySelection) -> dict[str, Any]:
    # 1. Dijkstra selection (computed once per metric by generate_sep)
    major = {c for c in selection.courses if selection.kinds.get(c) == "major"}

    # 2. Topological sort (first pass validates/detects cycles, second puts long chains first)
    order = topological_sort(catalog, selection.courses, completed)
    _, height = chain_metrics(order, catalog)
    order = topological_sort(catalog, selection.courses, completed, key=lambda c: (-height[c], c))

    # 3. CSP backtracking: place courses into real terms, ending in a Spring, load-balanced
    schedule = schedule_courses(catalog, order, profile, cfg.max_units, cfg.max_term_difficulty,
                                max_summer_units=cfg.max_summer_units, major_courses=major,
                                balance=cfg.balance, balance_margin=cfg.balance_margin,
                                min_regular_terms=cfg.min_regular_terms, max_avg_load=cfg.max_avg_load)
    targets = schedule.targets or [0.0] * len(schedule.slots)

    term_of = {c: schedule.slots[t].label for c, t in schedule.assignment.items()}
    position = {c: i for i, c in enumerate(order)}
    is_ge = lambda code: selection.kinds.get(code) == "ge"   # noqa: E731

    # --- semesters -----------------------------------------------------------
    semesters = []
    terms = schedule.terms()
    for n, (slot, codes) in enumerate(terms):
        courses = [catalog[c] for c in sorted(codes, key=position.__getitem__)]
        semesters.append({
            "index": slot.index + 1,
            "term": slot.label,
            "season": slot.term.value,
            "year": slot.year,
            "units": _num(sum(c.units for c in courses)),
            "max_units": _num(schedule.caps[slot.index]),
            "target_units": round(targets[slot.index], 1),
            "is_padding": not courses and n == len(terms) - 1,
            "courses": [{"id": node_id(c.code), "code": c.code, "title": c.title,
                         "units": _num(c.units), "is_ge": is_ge(c.code),
                         "satisfies": selection.reasons.get(c.code, [])} for c in courses],
        })

    # --- graph (planned courses + completed courses that matter to this plan) --
    chosen = set(agreement.required_courses) | {c for picks in selection.choices.values() for c in picks}
    shown_completed = {c for c in chosen if c in completed}
    for c in order:
        shown_completed |= {r for r in (*catalog[c].prereqs, *catalog[c].coreqs) if r in completed}
    graph_codes = [*order, *sorted(shown_completed)]
    in_graph = set(graph_codes)
    nodes = [{"data": {
        "id": node_id(code), "label": code, "title": catalog[code].title, "units": _num(catalog[code].units),
        "is_ge": is_ge(code), "status": "completed" if code in completed else "planned",
        "term": term_of.get(code), "satisfies": selection.reasons.get(code, []),
    }} for code in graph_codes]
    edges: dict[str, dict] = {}
    for code in graph_codes:
        for relation, reqs in (("prereq", catalog[code].prereqs), ("coreq", catalog[code].coreqs)):
            for r in reqs:
                if r in in_graph:
                    eid = f"e_{node_id(r)}_{node_id(code)}"
                    edges.setdefault(eid, {"data": {"id": eid, "source": node_id(r), "target": node_id(code),
                                                    "relation": relation}})
    cp = [node_id(c) for c in critical_path(order, catalog)]

    # --- requirements ----------------------------------------------------------
    def entry(code: str) -> dict[str, Any]:
        return {"id": node_id(code), "code": code, "title": catalog[code].title,
                "kind": selection.kinds.get(code, "major"),
                "status": "completed" if code in completed else "planned", "term": term_of.get(code)}

    requirements = [{
        "id": "major-prep", "name": "Major preparation (required)", "category": "major",
        "choose": len(agreement.required_courses),
        "courses": [entry(c) for c in agreement.required_courses],
    }] + [{
        "id": g.id, "name": g.name, "category": g.category.value, "choose": g.choose,
        "courses": [entry(c) for c in selection.choices[g.id]],
    } for g in agreement.requirement_groups]

    total_units = sum(catalog[c].units for c in order)
    last = schedule.slots[-1] if schedule.slots else None
    regular = [s for s in semesters if s["season"] != "Summer"]
    n_regular = len(regular)
    target = next((t for t, s in zip(targets, schedule.slots) if not s.is_summer), 0.0)
    return {
        "_regular_terms": n_regular,          # used by generate_sep, removed before returning
        "id": cfg.id,
        "name": cfg.name,
        "label": cfg.label,
        "kind": cfg.kind,
        "description": cfg.description or _describe(cfg, n_regular, len(semesters) - n_regular, target),
        "selection_metric": cfg.weight,
        "max_units_regular": _num(cfg.max_units),
        "max_units_summer": _num(min(cfg.max_units, cfg.max_summer_units)),
        "summary": {
            "terms_needed": len(semesters),
            "min_terms_required": schedule.lower_bound,
            "prereq_chain_length": len(cp),
            "unit_load_terms": math.ceil(total_units / cfg.max_units) if total_units else 0,
            "total_units": _num(total_units),
            "target_units_per_term": round(target, 1),
            "min_units_regular_term": _num(min((s["units"] for s in regular), default=0)),
            "max_units_regular_term": _num(max((s["units"] for s in regular), default=0)),
            "transfer_admission_term": f"Fall {last.year}" if last and last.term is Term.SPRING else None,
            "schedule_proven_minimal": schedule.proven_minimal,
            "major_prep_in_summer": schedule.major_in_summer,
        },
        "graph": {"nodes": nodes, "edges": list(edges.values())},
        "critical_path": cp,
        "critical_path_edges": [f"e_{a}_{b}" for a, b in zip(cp, cp[1:])],
        "requirements": requirements,
        "semesters": semesters,
    }
