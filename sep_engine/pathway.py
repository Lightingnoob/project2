"""
Algorithm 2 — Optimal pathway selection via Dijkstra's algorithm.

Picking courses for multiple-choice requirement groups ("choose 2 of these 10
Area 4 courses") is modelled as a shortest-path problem over decision states:

    state = (group g, picks made in g, next option index i, courses added so far, courses used per category)

From each state there are two edges:
    skip option i  -> (g, k, i+1, ...)            weight 0
    pick option i  -> (g, k+1, i+1, ... + new)   weight = w(option) + w(its missing prereqs/coreqs)
and once group g has `choose` picks it moves to (g+1, 0, 0, ...) at weight 0.
The goal is any state past the last group. All weights are non-negative, so
the first goal state Dijkstra pops is the minimum-total-weight combination.

Weights are tuples compared lexicographically (a primary metric, then a
tie-breaker): 'units' = (units, difficulty) for the shortest route,
'difficulty' = (difficulty, units) for the easiest route. Courses that are
already completed or already in the plan cost 0, which is what makes
double-counting (e.g. MATH 1 filling both major prep and GE Area 2) and
shared prerequisites come out right.

To keep the state space small, the "added"/"used" sets are trimmed to the
courses that still matter for the remaining groups; two states that agree on
everything relevant to the future are merged.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .errors import InfeasibleRequirementError
from .models import Course, GroupCategory, TransferAgreement
from .toposort import get_course

Weight = tuple[float, float]
ZERO: Weight = (0.0, 0.0)

WEIGHT_FUNCTIONS: dict[str, Callable[[Course], Weight]] = {
    "units": lambda c: (c.units, c.difficulty),        # shortest path, ties -> easier
    "difficulty": lambda c: (c.difficulty, c.units),   # easiest path, ties -> fewer units
}


def _add(a: Weight, b: Weight) -> Weight:
    return (a[0] + b[0], a[1] + b[1])


def requisite_closure(catalog: Mapping[str, Course], code: str, done: Iterable[str] = ()) -> list[str]:
    """Every course still needed before/with `code` (transitive prereqs + coreqs), excluding `code`.
    The walk stops at completed courses: a completed course's own prerequisites are already behind the student."""
    done = frozenset(done)
    seen: set[str] = {code}
    out: list[str] = []
    stack = [code]
    while stack:
        course = get_course(catalog, stack.pop())
        for r in (*course.prereqs, *course.coreqs):
            if r not in seen:
                get_course(catalog, r, context=course.code)
                seen.add(r)
                if r in done:
                    continue
                out.append(r)
                stack.append(r)
    return out


@dataclass
class PathwaySelection:
    courses: set[str]                                   # everything still to take (never completed ones)
    choices: dict[str, list[str]]                       # group id -> courses counted toward it
    reasons: dict[str, list[str]] = field(default_factory=dict)   # course -> why it is in the plan
    kinds: dict[str, str] = field(default_factory=dict)          # course -> "major" | "ge" (major wins)
    cost: Weight = ZERO
    states_explored: int = 0


@dataclass(frozen=True)
class _Pick:
    group_index: int
    course: str
    added: tuple[str, ...]   # courses this pick newly adds to the plan (itself and/or requisites)


State = tuple[int, int, int, frozenset[str], frozenset[tuple[GroupCategory, str]]]


def select_pathway(
    catalog: Mapping[str, Course],
    agreement: TransferAgreement,
    completed: Iterable[str] = (),
    metric: str = "units",
) -> PathwaySelection:
    """Choose courses for every requirement group so the plan's total weight is minimal."""
    if metric not in WEIGHT_FUNCTIONS:
        raise ValueError(f"metric must be one of {sorted(WEIGHT_FUNCTIONS)}, got {metric!r}")
    weight_of = WEIGHT_FUNCTIONS[metric]
    done = frozenset(completed)
    reasons: dict[str, list[str]] = {}

    kinds: dict[str, str] = {}

    def note(code: str, why: str, kind: str | None = None) -> None:
        reasons.setdefault(code, [])
        if why not in reasons[code]:
            reasons[code].append(why)
        if kind and kinds.get(code) != "major":
            kinds[code] = kind

    # Mandatory major prep (and everything it needs) is in every plan.
    base: set[str] = set()
    for code in agreement.required_courses:
        get_course(catalog, code, context="required major prep")
        note(code, "Major prep (required)", "major")
        if code in done:
            continue
        for r in requisite_closure(catalog, code, done):
            note(r, f"Needed for {code}", "major")
            base.add(r)
        base.add(code)

    groups = agreement.requirement_groups
    n = len(groups)
    closure: dict[str, list[str]] = {}
    for g in groups:
        for opt in g.options:
            get_course(catalog, opt, context=f"requirement group '{g.id}'")
            closure.setdefault(opt, [] if opt in done else requisite_closure(catalog, opt, done))

    # relevant[g]: courses whose presence can still change a cost from group g on.
    # used_keys[g]: (category, course) pairs that groups g.. could still try to reuse.
    relevant: list[frozenset[str]] = [frozenset()] * (n + 1)
    used_keys: list[frozenset[tuple[GroupCategory, str]]] = [frozenset()] * (n + 1)
    for g in reversed(range(n)):
        grp = groups[g]
        relevant[g] = relevant[g + 1].union(*([o, *closure[o]] for o in grp.options))
        used_keys[g] = used_keys[g + 1] | {(grp.category, o) for o in grp.options}

    def canon(g: int, k: int, i: int, sel: frozenset, used: frozenset) -> State:
        return (g, k, i, sel & relevant[g], used & used_keys[g])

    start: State = (0, 0, 0, frozenset(), frozenset())
    dist: dict[State, Weight] = {start: ZERO}
    parent: dict[State, tuple[State, _Pick | None] | None] = {start: None}
    tie = itertools.count()
    heap: list[tuple[Weight, int, State]] = [(ZERO, next(tie), start)]
    deepest = 0

    def relax(prev: State, nxt: State, d: Weight, pick: _Pick | None) -> None:
        if nxt not in dist or d < dist[nxt]:
            dist[nxt] = d
            parent[nxt] = (prev, pick)
            heapq.heappush(heap, (d, next(tie), nxt))

    while heap:
        d, _, st = heapq.heappop(heap)
        if d > dist[st]:
            continue                                   # stale heap entry
        g, k, i, sel, used = st
        if g == n:
            return _build_selection(catalog, groups, base, parent, st, reasons, kinds, note, weight_of, len(dist))
        deepest = max(deepest, g)
        grp = groups[g]

        if k == grp.choose:                            # group satisfied -> next group
            relax(st, canon(g + 1, 0, 0, sel, used), d, None)
            continue
        if len(grp.options) - i < grp.choose - k:      # not enough options left
            continue

        relax(st, (g, k, i + 1, sel, used), d, None)   # skip option i
        opt = grp.options[i]
        key = (grp.category, opt)
        if key in used:                                # already counted toward another group of this category
            continue
        added = tuple(c for c in (opt, *closure[opt]) if c not in done and c not in base and c not in sel)
        cost = d
        for c in added:
            cost = _add(cost, weight_of(catalog[c]))
        relax(st, canon(g, k + 1, i + 1, sel | frozenset(added), used | {key}),
              cost, _Pick(g, opt, added))

    grp = groups[deepest]
    raise InfeasibleRequirementError(
        f"Requirement '{grp.name}' cannot be satisfied: it needs {grp.choose} distinct course(s), "
        f"but too few of its options remain once courses already counted toward other "
        f"{grp.category.value} requirements are excluded.")


def _build_selection(catalog, groups, base, parent, goal, reasons, kinds, note, weight_of, explored) -> PathwaySelection:
    picks: list[_Pick] = []
    st = goal
    while parent[st] is not None:
        st, pick = parent[st]
        if pick:
            picks.append(pick)
    picks.reverse()

    courses = set(base)
    choices: dict[str, list[str]] = {g.id: [] for g in groups}
    for p in picks:
        grp = groups[p.group_index]
        kind = "major" if grp.category is GroupCategory.MAJOR else "ge"
        choices[grp.id].append(p.course)
        note(p.course, grp.name, kind)
        for c in p.added:
            courses.add(c)
            if c != p.course:
                note(c, f"Needed for {p.course}", kind)

    cost = ZERO
    for c in courses:
        cost = _add(cost, weight_of(catalog[c]))
    return PathwaySelection(courses=courses, choices=choices, reasons=reasons, kinds=kinds, cost=cost,
                            states_explored=explored)
