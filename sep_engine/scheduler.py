"""
Algorithm 3 — Semester scheduling as a Constraint Satisfaction Problem.

Calendar: terms run Spring -> (Summer) -> Fall -> Spring ... from the student's
start term, with real labels ("Fall 2026", "Summer 2027"). Summer only appears
when the student opts in.

Variables : each course still to take.
Domains   : term indices 0..H-1 in which the course is offered and fits the
            term's unit cap, narrowed by its prerequisite chain (it can't
            start before `depth` terms, and must leave room for its longest
            dependent chain).
Constraints:
  * prerequisite  — every in-plan prereq is in a strictly earlier term
  * co-requisite  — every in-plan coreq is in the same term or earlier
  * unit caps     — Fall/Spring <= max_units; Summer <= max_summer_units (half-length term)
  * difficulty cap (optional) — sum of difficulty scores per term <= cap
  * time conflict — no two courses in one term have overlapping meetings
  * term availability — Fall-only / Spring-only / no-summer courses stay put
  * transfer timeline — the plan's LAST term is always a Spring (transfer for
    Fall admission). If the courses would finish in a Fall, the plan is
    padded to end with the following Spring.

Summer bias: GE courses try Summer terms first; major prep tries Fall/Spring
first and, for each horizon, is only allowed into Summer at all if no
schedule of that length exists without it (a strict pass runs first with
major prep removed from Summer domains, then a relaxed pass).

Search: depth-first backtracking. The next variable is the one with the
fewest remaining values (MRV), ties broken by topological order. When a
course doesn't fit, the search moves it to the next candidate term, and if
nothing works it backtracks and moves an earlier course instead. After every
placement, forward checking removes now-impossible terms from the domains of
unplaced courses and abandons the branch as soon as any domain empties or the
remaining units can't fit.

Horizon: Spring-ending horizons H are tried shortest first under the HARD caps,
so the first feasible one is the earliest transfer (proven unless a horizon hit
the node budget). A plan can also demand a minimum number of Fall/Spring terms
(e.g. a 3-year track) or a maximum average load (e.g. <= 15 units/term).

Load balancing (instead of packing early terms to the cap):
  Target_Units_Per_Term = total units / weighted term count, where a Summer
  counts as summer_cap/max_units of a term (9/18 = 0.5) and gets that share of
  the target. Within the chosen horizon the CSP is re-solved with a SOFT cap of
  target + 2 units per term (loosened to +3, +5 if needed; the hard cap always
  wins). In this mode major prep is placed first along its prerequisite chain,
  then GE courses (biggest first) go into the term furthest below its target.
  Finally a hill-climb moves single GE courses between terms while that lowers
  sum((load - target)^2), so terms end up numerically similar (15/14/16/15, not
  18/18/17/7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .errors import SchedulingError
from .models import Course, StudentProfile, Term
from .toposort import chain_metrics

_EPS = 1e-9
DEFAULT_SUMMER_MAX_UNITS = 9


@dataclass(frozen=True)
class TermSlot:
    index: int
    term: Term
    year: int

    @property
    def label(self) -> str:
        return f"{self.term.value} {self.year}"

    @property
    def is_summer(self) -> bool:
        return self.term is Term.SUMMER


def parse_term_label(label: str) -> tuple[Term, int]:
    """'Fall 2026' -> (Term.FALL, 2026)."""
    m = re.fullmatch(r"\s*(fall|spring|summer)\s+(\d{4})\s*", label, re.IGNORECASE)
    if not m:
        raise ValueError(f"term must look like 'Fall 2026', got {label!r}")
    return Term(m.group(1).capitalize()), int(m.group(2))


def term_sequence(start_term: Term, start_year: int, count: int, include_summer: bool) -> list[TermSlot]:
    """The next `count` terms starting at start_term/start_year, in calendar order."""
    cycle = [Term.SPRING, Term.SUMMER, Term.FALL] if include_summer else [Term.SPRING, Term.FALL]
    if start_term not in cycle:
        raise SchedulingError("Starting in Summer requires include_summer=True.")
    i, year, out = cycle.index(start_term), start_year, []
    for n in range(count):
        out.append(TermSlot(n, cycle[i], year))
        i += 1
        if i == len(cycle):     # wrapped past Fall -> next calendar year
            i, year = 0, year + 1
    return out


@dataclass
class Schedule:
    slots: list[TermSlot]
    assignment: dict[str, int]           # course -> term index
    caps: list[float]                    # hard unit cap of each slot
    proven_minimal: bool                 # False if a shorter horizon ran out of search budget
    nodes_explored: int
    lower_bound: int                     # terms needed ignoring availability/conflicts/Spring-end
    major_in_summer: list[str] = field(default_factory=list)   # major prep that had to go in Summer
    targets: list[float] = field(default_factory=list)         # load-balancing target per slot (units)
    soft_caps: list[float] = field(default_factory=list)       # per-slot cap the balanced solution respects

    def terms(self) -> list[tuple[TermSlot, list[str]]]:
        return [(slot, [c for c, t in self.assignment.items() if t == slot.index]) for slot in self.slots]


class _BudgetExceeded(Exception):
    pass


def regular_term_count(slots: list[TermSlot]) -> int:
    return sum(not s.is_summer for s in slots)


def weighted_terms(slots: list[TermSlot], summer_weight: float) -> float:
    """Capacity-weighted term count: a Summer counts as `summer_weight` of a Fall/Spring (9/18 = 0.5)."""
    n_regular = regular_term_count(slots)
    return n_regular + summer_weight * (len(slots) - n_regular)


def spring_horizon(profile: StudentProfile, min_regular_terms: int) -> list[TermSlot]:
    """Shortest Spring-ending run of terms from the start term with at least `min_regular_terms` Fall/Spring terms."""
    for h in range(1, 40):
        slots = term_sequence(profile.start_term, profile.start_year, h, profile.include_summer)
        if slots[-1].term is Term.SPRING and regular_term_count(slots) >= min_regular_terms:
            return slots
    raise SchedulingError(f"No Spring-ending horizon with {min_regular_terms} regular terms.")


class _Backtracker:
    """
    DFS backtracking + forward checking over term indices.

    Two modes:
      * feasibility (targets=None): MRV variable order; GE Summer-first, major prep Fall/Spring-first,
        then earliest term. Decides the shortest valid horizon, exactly as before.
      * load-balanced (targets given): major prep is placed first (MRV among majors, earliest term within
        the soft caps, i.e. along the prerequisite chain), then GE courses are placed biggest-first into
        the term furthest below its target ("padding" the light terms).
    """

    def __init__(self, catalog: Mapping[str, Course], order: list[str], slots: list[TermSlot],
                 caps: list[float], max_difficulty: float | None, major: set[str],
                 node_budget: int, targets: list[float] | None = None) -> None:
        in_plan = set(order)
        self.course = {c: catalog[c] for c in order}
        self.rank = {c: i for i, c in enumerate(order)}
        self.prereqs = {c: {p for p in catalog[c].prereqs if p in in_plan} for c in order}
        self.coreqs = {c: {q for q in catalog[c].coreqs if q in in_plan} for c in order}
        self.dependents: dict[str, set[str]] = {c: set() for c in order}
        self.coreq_of: dict[str, set[str]] = {c: set() for c in order}
        for c in order:
            for p in self.prereqs[c]:
                self.dependents[p].add(c)
            for q in self.coreqs[c]:
                self.coreq_of[q].add(c)

        self.slots = slots
        self.caps = caps
        self.max_difficulty = max_difficulty
        self.major = major
        self.targets = targets
        self.load = [0.0] * len(slots)
        self.difficulty = [0.0] * len(slots)
        self.members: list[list[str]] = [[] for _ in slots]
        self.assignment: dict[str, int] = {}
        self.nodes = 0
        self.node_budget = node_budget

    def initial_domains(self, depth: dict[str, int], height: dict[str, int],
                        major_out_of_summer: bool) -> dict[str, list[int]]:
        H = len(self.slots)
        domains = {}
        for c, course in self.course.items():
            banned_summer = major_out_of_summer and c in self.major
            domains[c] = [s for s in range(depth[c], H - height[c] + 1)
                          if self.slots[s].term in course.terms_offered
                          and course.units <= self.caps[s] + _EPS
                          and not (banned_summer and self.slots[s].is_summer)]
        return domains

    def solve(self, domains: dict[str, list[int]]) -> dict[str, int] | None:
        if any(not d for d in domains.values()):
            return None
        return dict(self.assignment) if self._search(domains) else None

    # --- constraint checks -------------------------------------------------
    def _fits(self, c: str, s: int) -> bool:
        """Unit cap, difficulty cap and time conflicts against what's already in term s."""
        course = self.course[c]
        if self.load[s] + course.units > self.caps[s] + _EPS:
            return False
        if self.max_difficulty is not None and self.difficulty[s] + course.difficulty > self.max_difficulty + _EPS:
            return False
        return not any(course.conflicts_with(self.course[o]) for o in self.members[s])

    def _consistent(self, c: str, s: int) -> bool:
        a = self.assignment
        return (self._fits(c, s)
                and all(a[p] < s for p in self.prereqs[c] if p in a)
                and all(a[d] > s for d in self.dependents[c] if d in a)
                and all(a[q] <= s for q in self.coreqs[c] if q in a)
                and all(a[d] >= s for d in self.coreq_of[c] if d in a))

    def _place(self, c: str, s: int) -> None:
        self.assignment[c] = s
        self.load[s] += self.course[c].units
        self.difficulty[s] += self.course[c].difficulty
        self.members[s].append(c)

    def _unplace(self, c: str, s: int) -> None:
        del self.assignment[c]
        self.load[s] -= self.course[c].units
        self.difficulty[s] -= self.course[c].difficulty
        self.members[s].remove(c)

    # --- search --------------------------------------------------------------
    def _value_order(self, c: str, domain: list[int]) -> list[int]:
        is_major = c in self.major
        if self.targets is None:
            # Feasibility: GE Summer-first, major prep Fall/Spring-first, then earliest.
            return sorted(domain, key=lambda s: (0 if self.slots[s].is_summer != is_major else 1, s))
        if is_major:
            # Major prep: earliest Fall/Spring term that stays within the soft cap (follows the prereq chain).
            return sorted(domain, key=lambda s: (1 if self.slots[s].is_summer else 0, s))
        # GE: an under-target Summer first (GE goes to Summer), then the term furthest below its target.
        t = self.targets
        return sorted(domain, key=lambda s: (0 if self.slots[s].is_summer and self.load[s] < t[s] else 1,
                                             self.load[s] - t[s], s))

    def _pick_variable(self, domains: dict[str, list[int]]) -> str:
        if self.targets is None:
            return min(domains, key=lambda x: (len(domains[x]), self.rank[x]))            # MRV
        # Major prep first (MRV), then GE biggest-first so the fine-grained courses fill the gaps last.
        return min(domains, key=lambda x: (x not in self.major, len(domains[x]),
                                           0 if x in self.major else -self.course[x].units, self.rank[x]))

    def rebalance(self, domains: dict[str, list[int]], targets: list[float], max_moves: int = 500) -> int:
        """
        Hill-climb on a complete schedule: repeatedly make the single-course move that most reduces
        sum((term load - target)^2), keeping every constraint (caps, offerings, prereqs, coreqs, time
        conflicts, major prep out of Summer when required). GE courses are the padding: a major-prep
        course only moves when no GE move helps. Returns the number of moves made.
        """
        ge = [c for c in self.assignment if c not in self.major]
        majors = [c for c in self.assignment if c in self.major]
        moves = 0
        while moves < max_moves:
            best = self._best_move(ge, domains, targets) or self._best_move(majors, domains, targets)
            if best is None:
                return moves
            _, c, s, t = best
            self._unplace(c, s)
            self._place(c, t)
            moves += 1
        return moves

    def _best_move(self, courses: list[str], domains: dict[str, list[int]], targets: list[float]):
        best = None
        for c in courses:
            s, u = self.assignment[c], self.course[c].units
            for t in domains[c]:
                if t == s:
                    continue
                delta = ((self.load[s] - u - targets[s]) ** 2 + (self.load[t] + u - targets[t]) ** 2
                         - (self.load[s] - targets[s]) ** 2 - (self.load[t] - targets[t]) ** 2)
                if delta > -_EPS or (best is not None and delta >= best[0]):
                    continue
                self._unplace(c, s)
                ok = self._consistent(c, t)
                self._place(c, s)
                if ok:
                    best = (delta, c, s, t)
        return best

    def _search(self, domains: dict[str, list[int]]) -> bool:
        if not domains:
            return True
        self.nodes += 1
        if self.nodes > self.node_budget:
            raise _BudgetExceeded
        c = self._pick_variable(domains)
        rest = {x: d for x, d in domains.items() if x != c}
        for s in self._value_order(c, domains[c]):
            if not self._consistent(c, s):
                continue
            self._place(c, s)
            pruned = self._forward_check(c, s, rest)
            if pruned is not None and self._search(pruned):
                return True
            self._unplace(c, s)                                           # backtrack
        return False

    def _forward_check(self, c: str, s: int, domains: dict[str, list[int]]) -> dict[str, list[int]] | None:
        """Prune values made impossible by placing c in term s; None means a dead end."""
        pre, dep, co, co_of = self.prereqs[c], self.dependents[c], self.coreqs[c], self.coreq_of[c]
        pruned: dict[str, list[int]] = {}
        for x, dom in domains.items():
            keep = [v for v in dom
                    if not (x in pre and v >= s)
                    and not (x in dep and v <= s)
                    and not (x in co and v > s)
                    and not (x in co_of and v < s)
                    and (v != s or self._fits(x, s))]
            if not keep:
                return None
            pruned[x] = keep
        remaining = sum(self.course[x].units for x in pruned)
        capacity = sum(cap - load for cap, load in zip(self.caps, self.load))
        return pruned if remaining <= capacity + _EPS else None


def schedule_courses(
    catalog: Mapping[str, Course],
    order: list[str],
    profile: StudentProfile,
    max_units: float = 18,
    max_term_difficulty: float | None = None,
    *,
    max_summer_units: float = DEFAULT_SUMMER_MAX_UNITS,
    major_courses: Iterable[str] = (),
    balance: bool = True,
    balance_margin: float = 2.0,
    min_regular_terms: int = 0,
    max_avg_load: float | None = None,
    end_in_spring: bool = True,
    node_budget: int = 50_000,
) -> Schedule:
    """
    Assign topologically ordered courses to real terms.

    Horizon: the shortest Spring-ending run of terms that is feasible under the HARD caps and also has
    at least `min_regular_terms` Fall/Spring terms and (if given) an average load of at most
    `max_avg_load` units per Fall/Spring term (a Summer counts as summer_cap/max_units of a term).

    Load balancing (balance=True), within that horizon:
      Target_Units_Per_Term = total units / weighted term count  (Summer target scaled the same way)
      1. re-solve with each term capped at min(hard cap, target + balance_margin) as a soft cap,
         loosening the margin (+1, +3) if that's infeasible, else keeping the hard-cap solution;
      2. hill-climb GE moves toward the targets (see _Backtracker.rebalance).
    The hard caps (e.g. 18 units, 9 in Summer) are never exceeded.
    """
    summer_cap = min(max_units, max_summer_units)
    cap_for = {Term.FALL: max_units, Term.SPRING: max_units, Term.SUMMER: summer_cap}
    cycle_terms = {Term.FALL, Term.SPRING} | ({Term.SUMMER} if profile.include_summer else set())
    major = set(major_courses) & set(order)

    for c in order:
        course = catalog[c]
        if course.units > max_units:
            raise SchedulingError(f"{c} is {course.units} units, above the {max_units}-unit term cap.")
        if max_term_difficulty is not None and course.difficulty > max_term_difficulty:
            raise SchedulingError(f"{c} alone exceeds the per-term difficulty cap of {max_term_difficulty}.")
        usable = [t for t in course.terms_offered & cycle_terms if course.units <= cap_for[t] + _EPS]
        if not usable:
            offered = ", ".join(sorted(t.value for t in course.terms_offered))
            raise SchedulingError(f"{c} is only offered in {offered}, and no such term in this plan can "
                                  f"hold its {course.units} units (Summer cap {summer_cap}, "
                                  f"summer {'on' if profile.include_summer else 'off'}).")

    if not order:
        return Schedule([], {}, [], True, 0, 0)

    def slots_for(h: int) -> list[TermSlot]:
        return term_sequence(profile.start_term, profile.start_year, h, profile.include_summer)

    depth, height = chain_metrics(order, catalog)
    total_units = sum(catalog[c].units for c in order)
    lower = max(height.values())
    while lower < profile.max_terms and sum(cap_for[s.term] for s in slots_for(lower)) < total_units - _EPS:
        lower += 1

    summer_weight = summer_cap / max_units

    def summer_room(strict: bool) -> float:
        """Units that could go into Summer at all (offered in Summer, fit its cap; major prep only if allowed)."""
        return sum(catalog[c].units for c in order
                   if Term.SUMMER in catalog[c].terms_offered and catalog[c].units <= summer_cap + _EPS
                   and not (strict and c in major))

    proven, explored = True, 0
    for horizon in range(lower, profile.max_terms + 1):
        slots = slots_for(horizon)
        if end_in_spring and slots[-1].term is not Term.SPRING:
            continue                         # transfers happen after a Spring: only Spring-ending plans
        if regular_term_count(slots) < min_regular_terms:
            continue
        weight = weighted_terms(slots, summer_weight if summer_room(strict=True) > 0 else 0.0)
        if max_avg_load is not None and total_units / weight > max_avg_load + _EPS:
            continue                         # would need more than the allowed average load per term
        caps = [cap_for[s.term] for s in slots]
        has_summer = any(s.is_summer for s in slots)
        # Strict pass keeps major prep out of Summer; relax only if that finds nothing.
        for strict in ((True, False) if has_summer and major else (False,)):
            solver = _Backtracker(catalog, order, slots, caps, max_term_difficulty, major, node_budget)
            try:
                result = solver.solve(solver.initial_domains(depth, height, major_out_of_summer=strict))
            except _BudgetExceeded:
                result, proven = None, False
            explored += solver.nodes
            if result is None:
                continue
            targets, soft_caps = [], list(caps)
            if balance:
                targets = load_targets(slots, total_units, summer_weight, summer_room(strict))
                solver, soft_caps, n = _balance(catalog, order, slots, caps, targets, max_term_difficulty, major,
                                                depth, height, strict, balance_margin, node_budget, solver)
                explored += n
            assignment = dict(solver.assignment)
            in_summer = sorted(c for c in major if slots[assignment[c]].is_summer)
            return Schedule(slots, assignment, caps, proven, explored, lower, in_summer, targets, soft_caps)

    raise SchedulingError(
        f"No valid schedule ending in a Spring term fits within {profile.max_terms} terms under a "
        f"{max_units}-unit cap" + (f" and a difficulty cap of {max_term_difficulty}" if max_term_difficulty else "")
        + (f" with at least {min_regular_terms} Fall/Spring terms" if min_regular_terms else "")
        + (f" at no more than {max_avg_load:g} units per term on average" if max_avg_load else "")
        + ". Try allowing more terms, enabling summer, or relaxing the caps.")


def load_targets(slots: list[TermSlot], total_units: float, summer_weight: float, summer_room: float) -> list[float]:
    """
    Target_Units_Per_Term = total / weighted term count; a Summer's target is summer_weight of that.
    Summers only share the load if some course can go there, and never more than those courses' units.
    """
    n_regular = regular_term_count(slots)
    n_summer = len(slots) - n_regular
    w = summer_weight if summer_room > 0 else 0.0
    per_term = total_units / (n_regular + w * n_summer)
    summer_target = per_term * w
    if n_summer and summer_target * n_summer > summer_room:
        summer_target = summer_room / n_summer
        per_term = (total_units - summer_room) / n_regular
    return [summer_target if s.is_summer else per_term for s in slots]


def _balance(catalog, order, slots, caps, targets, max_difficulty, major, depth, height, strict,
             margin, node_budget, fallback: _Backtracker) -> tuple[_Backtracker, list[float], int]:
    """Soft-cap re-solve (tightest margin first), then GE hill-climbing. Returns (solver, soft caps, nodes)."""
    explored = 0
    solver, soft_caps = fallback, list(caps)
    for m in (margin, margin + 1, margin + 3):
        trial_caps = [min(cap, target + m) for cap, target in zip(caps, targets)]
        trial = _Backtracker(catalog, order, slots, trial_caps, max_difficulty, major,
                             min(node_budget, 20_000), targets=targets)
        try:
            found = trial.solve(trial.initial_domains(depth, height, major_out_of_summer=strict))
        except _BudgetExceeded:
            found = None
        explored += trial.nodes
        if found is not None:
            solver, soft_caps = trial, trial_caps
            break
    solver.rebalance(solver.initial_domains(depth, height, major_out_of_summer=strict), targets)
    return solver, soft_caps, explored
