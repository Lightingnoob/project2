"""Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import itertools
import json
import random
import unittest

from sep_engine import (Course, CycleError, GroupCategory, InfeasibleRequirementError, MeetingTime,
                        RequirementGroup, SchedulingError, StudentProfile, Term, TransferAgreement,
                        generate_sep, schedule_courses, select_pathway, topological_sort)
from sep_engine.mock_data import MOCK_AGREEMENT, MOCK_CATALOG, MOCK_PROFILE
from sep_engine.models import index_catalog
from sep_engine.pathway import WEIGHT_FUNCTIONS, requisite_closure
from sep_engine.scheduler import load_targets, parse_term_label, term_sequence

F, S, SU = Term.FALL, Term.SPRING, Term.SUMMER


def cat(*courses: Course) -> dict[str, Course]:
    return index_catalog(list(courses))


def course(code, units=3, difficulty=3, prereqs=(), coreqs=(), terms=(F, S), meetings=()):
    return Course(code=code, title=code, units=units, difficulty=difficulty, prereqs=list(prereqs),
                  coreqs=list(coreqs), terms_offered=frozenset(terms),
                  meetings=[MeetingTime(days=d, start=s, end=e) for d, s, e in meetings])


def assert_valid_schedule(tc: unittest.TestCase, catalog, courses, completed, schedule, max_units,
                          max_difficulty=None):
    a = schedule.assignment
    tc.assertEqual(set(a), set(courses) - set(completed))
    for c, t in a.items():
        crs = catalog[c]
        tc.assertIn(schedule.slots[t].term, crs.terms_offered, f"{c} placed in a term it isn't offered")
        for p in crs.prereqs:
            tc.assertTrue(p in completed or a[p] < t, f"{c} scheduled before prereq {p}")
        for q in crs.coreqs:
            tc.assertTrue(q in completed or a[q] <= t, f"{c} scheduled before coreq {q}")
    if schedule.slots:
        tc.assertEqual(schedule.slots[-1].term, S, "plan must end in a Spring term")
    for slot, codes in schedule.terms():
        tc.assertLessEqual(sum(catalog[c].units for c in codes), min(max_units, schedule.caps[slot.index]) + 1e-9)
        if max_difficulty is not None:
            tc.assertLessEqual(sum(catalog[c].difficulty for c in codes), max_difficulty + 1e-9)
        for x, y in itertools.combinations(codes, 2):
            tc.assertFalse(catalog[x].conflicts_with(catalog[y]), f"{x} and {y} overlap in {slot.label}")


class TopologicalSortTests(unittest.TestCase):
    def test_prereqs_come_first(self):
        c = cat(course("A"), course("B", prereqs=["A"]), course("C", prereqs=["A", "B"]), course("D"))
        order = topological_sort(c, c.keys())
        for code in order:
            for p in c[code].prereqs:
                self.assertLess(order.index(p), order.index(code))

    def test_completed_courses_are_dropped(self):
        c = cat(course("A"), course("B", prereqs=["A"]))
        self.assertEqual(topological_sort(c, ["A", "B"], completed=["A"]), ["B"])

    def test_cycle_is_reported_with_path(self):
        c = cat(course("A", prereqs=["C"]), course("B", prereqs=["A"]), course("C", prereqs=["B"]), course("D"))
        with self.assertRaises(CycleError) as ctx:
            topological_sort(c, c.keys())
        cycle = ctx.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"A", "B", "C"})
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_two_course_cycle(self):
        c = cat(course("A", prereqs=["B"]), course("B", prereqs=["A"]))
        with self.assertRaises(CycleError):
            topological_sort(c, c.keys())


class DijkstraTests(unittest.TestCase):
    def test_picks_cheapest_options_and_counts_prereqs(self):
        # X is cheap on its own but drags in a 5-unit prereq; Y is the true minimum.
        c = cat(course("P", units=5), course("X", units=2, prereqs=["P"]), course("Y", units=3), course("Z", units=4))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["X", "Y", "Z"])])
        sel = select_pathway(c, ag, metric="units")
        self.assertEqual(sel.choices["g"], ["Y"])
        self.assertEqual(sel.courses, {"Y"})

    def test_double_count_major_into_ge_but_not_ge_into_ge(self):
        c = cat(course("M", units=5), course("G1", units=3), course("G2", units=3))
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=["M"], requirement_groups=[
            RequirementGroup(id="a", name="A", category=GroupCategory.GE, options=["M", "G1"]),
            RequirementGroup(id="b", name="B", category=GroupCategory.GE, options=["M", "G2"])])
        sel = select_pathway(c, ag)
        # M is free for one GE group, but may not fill both -> M (5) + one 3-unit GE course
        self.assertEqual((sel.choices["a"] + sel.choices["b"]).count("M"), 1)
        self.assertEqual(sel.cost[0], 8)

    def test_completed_course_prerequisites_are_not_added(self):
        # CSCI 20 <- CSCI 15 <- CSCI 14: a student who completed CSCI 15 never needs CSCI 14 again
        c = cat(course("C14", units=4), course("C15", units=4, prereqs=["C14"]), course("C20", units=4, prereqs=["C15"]),
                course("G1"), course("G2", prereqs=["C14"]))
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=["C20"],
                               requirement_groups=[RequirementGroup(id="g", name="G", category=GroupCategory.MAJOR,
                                                                    options=["C15", "G1"])])
        sel = select_pathway(c, ag, completed=["C15"])
        self.assertEqual(sel.courses, {"C20"})                     # C15 done and chosen for free; no C14
        self.assertEqual(requisite_closure(c, "C20", done={"C15"}), [])

    def test_completed_course_is_free(self):
        c = cat(course("A", units=3, difficulty=1), course("B", units=4, difficulty=5))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["A", "B"])])
        sel = select_pathway(c, ag, completed=["B"])
        self.assertEqual(sel.choices["g"], ["B"])
        self.assertEqual(sel.courses, set())

    def test_metrics_disagree(self):
        c = cat(course("SHORT", units=2, difficulty=4), course("EASY", units=3, difficulty=1))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["SHORT", "EASY"])])
        self.assertEqual(select_pathway(c, ag, metric="units").choices["g"], ["SHORT"])
        self.assertEqual(select_pathway(c, ag, metric="difficulty").choices["g"], ["EASY"])

    def test_infeasible_group(self):
        c = cat(course("A"), course("B"))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g1", name="First", category=GroupCategory.GE, choose=2, options=["A", "B"]),
            RequirementGroup(id="g2", name="Second", category=GroupCategory.GE, options=["A"])])
        with self.assertRaises(InfeasibleRequirementError) as ctx:
            select_pathway(c, ag)
        self.assertIn("Second", str(ctx.exception))

    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(7)
        for trial in range(150):
            n = rng.randint(4, 9)
            courses = [course(f"C{i}", units=rng.randint(1, 5), difficulty=rng.randint(1, 5),
                              prereqs=rng.sample([f"C{j}" for j in range(i)], k=min(i, rng.choice([0, 0, 1, 2]))))
                       for i in range(n)]
            c = cat(*courses)
            codes = list(c)
            groups = []
            for gi in range(rng.randint(1, 4)):
                opts = rng.sample(codes, k=rng.randint(1, min(5, n)))
                groups.append(RequirementGroup(id=f"g{gi}", name=f"G{gi}", choose=rng.randint(1, len(opts)),
                                               category=rng.choice([GroupCategory.GE, GroupCategory.MAJOR]),
                                               options=opts))
            required = rng.sample(codes, k=rng.randint(0, 2))
            completed = rng.sample(codes, k=rng.randint(0, 2))
            ag = TransferAgreement(college="c", university="u", major="m",
                                   required_courses=required, requirement_groups=groups)
            for metric in ("units", "difficulty"):
                expected = brute_force_cost(c, ag, completed, metric)
                if expected is None:
                    with self.assertRaises(InfeasibleRequirementError):
                        select_pathway(c, ag, completed, metric)
                else:
                    sel = select_pathway(c, ag, completed, metric)
                    self.assertEqual(sel.cost, expected, f"trial {trial} metric {metric}")


def brute_force_cost(catalog, agreement, completed, metric):
    w = WEIGHT_FUNCTIONS[metric]
    done = set(completed)
    per_group = [itertools.combinations(g.options, g.choose) for g in agreement.requirement_groups]
    best = None
    for combo in itertools.product(*per_group):
        used = set()
        ok = True
        for g, picks in zip(agreement.requirement_groups, combo):
            for p in picks:
                if (g.category, p) in used:
                    ok = False
                used.add((g.category, p))
        if not ok:
            continue
        chosen = set(agreement.required_courses) | {p for picks in combo for p in picks}
        full = set()
        for x in chosen:     # a completed course's own prerequisites are already behind the student
            full |= {x} if x in done else {x, *requisite_closure(catalog, x, done)}
        cost = (0.0, 0.0)
        for x in full - done:
            cost = (cost[0] + w(catalog[x])[0], cost[1] + w(catalog[x])[1])
        best = cost if best is None or cost < best else best
    return best


class SchedulerTests(unittest.TestCase):
    def test_term_sequence(self):
        labels = [s.label for s in term_sequence(F, 2026, 4, include_summer=False)]
        self.assertEqual(labels, ["Fall 2026", "Spring 2027", "Fall 2027", "Spring 2028"])
        labels = [s.label for s in term_sequence(S, 2027, 4, include_summer=True)]
        self.assertEqual(labels, ["Spring 2027", "Summer 2027", "Fall 2027", "Spring 2028"])
        labels = [s.label for s in term_sequence(SU, 2027, 3, include_summer=True)]
        self.assertEqual(labels, ["Summer 2027", "Fall 2027", "Spring 2028"])
        with self.assertRaises(SchedulingError):
            term_sequence(SU, 2027, 3, include_summer=False)

    def test_parse_term_label(self):
        self.assertEqual(parse_term_label("Fall 2026"), (F, 2026))
        self.assertEqual(parse_term_label(" summer 2027 "), (SU, 2027))
        with self.assertRaises(ValueError):
            parse_term_label("Autumn 2026")

    def test_fall_only_course_is_pushed(self):
        # B needs A; B is Fall-only, so starting in Fall: A (Fall), gap (Spring), B (Fall), then end on Spring
        c = cat(course("A"), course("B", prereqs=["A"], terms=[F]))
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=F))
        self.assertEqual(sched.assignment, {"A": 0, "B": 2})
        self.assertEqual([s.label for s in sched.slots], ["Fall 2026", "Spring 2027", "Fall 2027", "Spring 2028"])

    def test_ends_in_spring_with_padding(self):
        # Everything fits in Fall 2026, but transfers happen after a Spring -> pad to Spring 2027
        c = cat(course("A"), course("B"))
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=F, start_year=2026), balance=False)
        self.assertEqual([s.label for s in sched.slots], ["Fall 2026", "Spring 2027"])
        self.assertEqual(set(sched.assignment.values()), {0})
        # With load balancing the final Spring is used instead of left empty
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=F, start_year=2026))
        self.assertEqual(sorted(sched.assignment.values()), [0, 1])
        # Starting in Spring, a one-term plan already ends in Spring
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=S, start_year=2027))
        self.assertEqual([s.label for s in sched.slots], ["Spring 2027"])

    def test_unit_cap_forces_extra_term(self):
        c = cat(*(course(f"X{i}", units=5) for i in range(4)))
        sched = schedule_courses(c, list(c), StudentProfile(), max_units=12)
        self.assertEqual(len(sched.slots), 2)
        assert_valid_schedule(self, c, c.keys(), [], sched, 12)

    def test_time_conflict_separates_courses(self):
        c = cat(course("A", meetings=[("MW", "09:00", "10:15")]), course("B", meetings=[("W", "10:00", "11:00")]))
        sched = schedule_courses(c, ["A", "B"], StudentProfile())
        self.assertNotEqual(sched.assignment["A"], sched.assignment["B"])

    def test_coreqs_can_share_a_term(self):
        c = cat(course("LEC"), course("LAB", units=1, coreqs=["LEC"]))
        sched = schedule_courses(c, ["LEC", "LAB"], StudentProfile(start_term=S))
        self.assertEqual(len(sched.slots), 1)
        self.assertEqual(sched.assignment["LEC"], sched.assignment["LAB"])

    def test_summer_only_when_enabled_and_capped(self):
        c = cat(*(course(f"G{i}", units=3, terms=(F, S, SU)) for i in range(6)))
        off = schedule_courses(c, list(c), StudentProfile(start_term=S), max_units=18)
        self.assertFalse(any(s.is_summer for s in off.slots))
        on = schedule_courses(c, list(c), StudentProfile(start_term=S, include_summer=True), max_units=18)
        for slot, codes in on.terms():
            if slot.is_summer:
                self.assertLessEqual(sum(c[x].units for x in codes), 9)
        # a course bigger than the summer cap never lands in summer
        big = cat(course("BIG", units=10, terms=(F, S, SU)), course("G", units=3, terms=(F, S, SU)))
        sched = schedule_courses(big, ["BIG", "G"], StudentProfile(start_term=SU, include_summer=True))
        self.assertFalse(sched.slots[sched.assignment["BIG"]].is_summer)

    def test_ge_goes_to_summer_major_stays_out(self):
        # Start in Spring with summer: GE (offered any term) fills the summer; major prep stays in Fall/Spring
        major = [course("M1", units=4, terms=(F, S, SU)), course("M2", units=4, prereqs=["M1"], terms=(F, S, SU))]
        ge = [course(f"G{i}", units=3, terms=(F, S, SU)) for i in range(3)]
        c = cat(*major, *ge)
        order = topological_sort(c, c.keys())
        profile = StudentProfile(start_term=S, include_summer=True)
        # Packing mode: every GE goes to the Summer
        sched = schedule_courses(c, order, profile, major_courses={"M1", "M2"}, balance=False)
        summer = [codes for slot, codes in sched.terms() if slot.is_summer]
        self.assertTrue(summer and set(summer[0]) == {"G0", "G1", "G2"}, sched.terms())
        # Balanced mode: Summer still gets GE (only GE), but only its share of the load
        sched = schedule_courses(c, order, profile, major_courses={"M1", "M2"})
        summer = [codes for slot, codes in sched.terms() if slot.is_summer]
        self.assertTrue(summer and summer[0] and set(summer[0]) <= {"G0", "G1", "G2"}, sched.terms())
        self.assertEqual(sched.major_in_summer, [])
        assert_valid_schedule(self, c, c.keys(), [], sched, 18)

    def test_major_in_summer_only_when_necessary(self):
        # A 5-course chain from Fall 2026 can only end by Spring 2028 if one link runs in Summer 2027
        chain = [course(f"M{i}", units=4, prereqs=[f"M{i-1}"] if i else [], terms=(F, S, SU)) for i in range(5)]
        c = cat(*chain)
        order = [f"M{i}" for i in range(5)]
        sched = schedule_courses(c, order, StudentProfile(start_term=F, include_summer=True), major_courses=order)
        self.assertEqual(sched.slots[-1].label, "Spring 2028")
        self.assertEqual(sched.major_in_summer, ["M2"])

    def test_backtracking_needed(self):
        # D and E are Fall-only and clash, C is Spring-only after A, and the cap fits two courses:
        # the search has to undo early placements to find the minimum.
        c = cat(course("A", units=6), course("B", units=6), course("C", units=6, prereqs=["A"], terms=[S]),
                course("D", units=6, terms=[F], meetings=[("MW", "09:00", "10:00")]),
                course("E", units=6, terms=[F], meetings=[("MW", "09:30", "10:30")]))
        order = topological_sort(c, c.keys())
        sched = schedule_courses(c, order, StudentProfile(start_term=F), max_units=12)
        assert_valid_schedule(self, c, c.keys(), [], sched, 12)
        self.assertEqual(len(sched.slots), brute_force_min_terms(c, order, 12, F))

    def test_minimal_terms_match_brute_force(self):
        rng = random.Random(11)
        for trial in range(80):
            summer = trial % 2 == 1
            n = rng.randint(2, 5 if summer else 6)
            offered = [(F, S, SU), (F, S, SU), (F, S), (F,), (S,)] if summer else [(F, S), (F, S), (F,), (S,)]
            courses = []
            for i in range(n):
                prereqs = rng.sample([f"C{j}" for j in range(i)], k=min(i, rng.choice([0, 0, 1])))
                meetings = [rng.choice([("MW", "09:00", "10:15"), ("TR", "09:00", "10:15"), ("MW", "10:00", "11:00")])] \
                    if rng.random() < 0.4 else []
                courses.append(course(f"C{i}", units=rng.randint(2, 6), prereqs=prereqs,
                                      terms=rng.choice(offered), meetings=meetings))
            c = cat(*courses)
            order = topological_sort(c, c.keys())
            start = rng.choice([F, S])
            major = set(rng.sample(order, k=rng.randint(0, len(order))))
            profile = StudentProfile(start_term=start, include_summer=summer, max_terms=8)
            expected = brute_force_min_terms(c, order, 9, start, limit=8, include_summer=summer, summer_cap=5)
            for balance in (False, True):           # balancing never changes which horizon is minimal
                try:
                    sched = schedule_courses(c, order, profile, max_units=9, max_summer_units=5,
                                             major_courses=major, balance=balance)
                except SchedulingError:
                    self.assertIsNone(expected, f"trial {trial}")
                    continue
                assert_valid_schedule(self, c, c.keys(), [], sched, 9)
                self.assertEqual(len(sched.slots), expected, f"trial {trial} balance={balance}")

    def test_unit_cap_violation_raises(self):
        c = cat(course("BIG", units=20))
        with self.assertRaises(SchedulingError):
            schedule_courses(c, ["BIG"], StudentProfile(), max_units=18)


class LoadBalancingTests(unittest.TestCase):
    def loads(self, sched):
        return [sum(self.c[c].units for c in codes) for _, codes in sched.terms()]

    def test_even_spread_instead_of_packing(self):
        self.c = cat(*(course(f"G{i}", units=3) for i in range(12)))       # 36 units, 4 terms
        packed = schedule_courses(self.c, list(self.c), StudentProfile(), min_regular_terms=4, balance=False)
        balanced = schedule_courses(self.c, list(self.c), StudentProfile(), min_regular_terms=4)
        self.assertEqual(self.loads(packed), [18, 18, 0, 0])
        self.assertEqual(self.loads(balanced), [9, 9, 9, 9])
        self.assertEqual(balanced.targets, [9.0] * 4)

    def test_every_term_within_margin_of_target(self):
        # a 4-course major chain (4u) + 12 GE courses of mixed sizes, over 4 terms
        chain = [course(f"M{i}", units=4, prereqs=[f"M{i-1}"] if i else []) for i in range(4)]
        ge = [course(f"G{i}", units=u) for i, u in enumerate([3, 3, 3, 3, 4, 4, 5, 3, 3, 2, 3, 4])]
        self.c = cat(*chain, *ge)
        sched = schedule_courses(self.c, topological_sort(self.c, self.c.keys()), StudentProfile(),
                                 major_courses={m.code for m in chain}, min_regular_terms=4)
        assert_valid_schedule(self, self.c, self.c.keys(), [], sched, 18)
        target = sched.targets[0]
        for load in self.loads(sched):
            self.assertLessEqual(abs(load - target), 2 + 1e-9, (self.loads(sched), target))

    def test_major_prep_moves_only_when_no_ge_can_help(self):
        # all major prep, no GE: balancing still spreads it (the last term isn't left light)
        self.c = cat(*(course(f"M{i}", units=4) for i in range(9)))       # 36 units over 4 terms
        sched = schedule_courses(self.c, list(self.c), StudentProfile(), major_courses=set(self.c),
                                 min_regular_terms=4)
        self.assertEqual(max(self.loads(sched)) - min(self.loads(sched)), 4)   # 12/8/8/8 at worst: 4u courses

    def test_max_avg_load_extends_horizon(self):
        self.c = cat(*(course(f"G{i}", units=3) for i in range(20)))       # 60 units
        light = schedule_courses(self.c, list(self.c), StudentProfile(), max_avg_load=15)
        self.assertEqual(len(light.slots), 4)                               # 60 / 4 = 15
        lighter = schedule_courses(self.c, list(self.c), StudentProfile(), max_avg_load=12)
        self.assertEqual(len(lighter.slots), 6)                             # 60 / 4 > 12 -> next Spring-ending horizon

    def test_summer_share_only_when_courses_can_go_there(self):
        slots = term_sequence(F, 2026, 5, include_summer=True)              # F, Sp, Su, F, Sp
        self.assertEqual(load_targets(slots, 45, 0.5, summer_room=30), [10, 10, 5, 10, 10])
        self.assertEqual(load_targets(slots, 45, 0.5, summer_room=0), [11.25, 11.25, 0, 11.25, 11.25])
        self.assertEqual(load_targets(slots, 45, 0.5, summer_room=3), [10.5, 10.5, 3, 10.5, 10.5])


def brute_force_min_terms(catalog, order, max_units, start, limit=8, include_summer=False, summer_cap=9):
    """Fewest terms of any valid, Spring-ending schedule (exhaustive search)."""
    for H in range(1, limit + 1):
        slots = term_sequence(start, 2026, H, include_summer=include_summer)
        if slots[-1].term is not S:
            continue
        caps = [min(max_units, summer_cap) if s.is_summer else max_units for s in slots]
        for combo in itertools.product(range(H), repeat=len(order)):
            a = dict(zip(order, combo))
            if all(slots[t].term in catalog[c].terms_offered
                   and all(a[p] < t for p in catalog[c].prereqs if p in a)
                   and all(a[q] <= t for q in catalog[c].coreqs if q in a)
                   for c, t in a.items()) \
               and all(sum(catalog[c].units for c in a if a[c] == t) <= caps[t] for t in range(H)) \
               and not any(a[x] == a[y] and catalog[x].conflicts_with(catalog[y])
                           for x, y in itertools.combinations(order, 2)):
                return H
    return None


class PipelineTests(unittest.TestCase):
    def check_plans(self, result, profile):
        json.dumps(result)   # must be JSON-serializable
        catalog = index_catalog(MOCK_CATALOG)
        completed = set(profile.completed_courses)
        calendar = [s.label for s in term_sequence(profile.start_term, profile.start_year, 30, profile.include_summer)]
        for plan in result["plans"]:
            sems = plan["semesters"]
            self.assertEqual([s["term"] for s in sems], calendar[:len(sems)])
            self.assertEqual(sems[-1]["season"], "Spring")
            self.assertEqual(plan["summary"]["transfer_admission_term"], f"Fall {sems[-1]['year']}")
            scheduled = [c["code"] for s in sems for c in s["courses"]]
            self.assertEqual(len(scheduled), len(set(scheduled)))
            self.assertFalse(completed & set(scheduled))
            for s in sems:
                cap = plan["max_units_summer"] if s["season"] == "Summer" else plan["max_units_regular"]
                self.assertEqual(s["max_units"], cap)
                self.assertLessEqual(s["units"], cap)
            for req in plan["requirements"]:
                self.assertEqual(len(req["courses"]), req["choose"])
            term_of = {c["code"]: s["index"] for s in sems for c in s["courses"]}
            for code, t in term_of.items():
                for p in catalog[code].prereqs:
                    self.assertTrue(p in completed or term_of[p] < t)

    def test_mock_pipeline(self):
        # 53 units / 4 semesters = 13.25 <= 15 -> normal workload -> ONE balanced plan
        result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, MOCK_PROFILE)
        self.assertEqual((result["workload"]["classification"], result["workload"]["plan_count"]), ("normal", 1))
        self.assertEqual([(p["label"], p["kind"]) for p in result["plans"]], [("Balanced Plan", "recommended")])
        self.check_plans(result, MOCK_PROFILE)

    def test_mock_pipeline_all_start_terms_and_summer(self):
        for summer in (False, True):
            for term, year in [(F, 2026), (S, 2027), (SU, 2027)]:
                if term is SU and not summer:
                    continue
                profile = MOCK_PROFILE.model_copy(update={"start_term": term, "start_year": year, "include_summer": summer})
                result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, profile)
                self.check_plans(result, profile)
                for plan in result["plans"]:
                    for s in plan["semesters"]:
                        if s["season"] == "Summer":
                            self.assertTrue(all(c["is_ge"] for c in s["courses"]),
                                            f"major prep in {s['term']}: {s['courses']}")
                        else:                    # balanced: every Fall/Spring term within target ± 2
                            self.assertLessEqual(abs(s["units"] - s["target_units"]), 2 + 1e-9,
                                                 f"{s['term']}: {s['units']} vs target {s['target_units']}")

    @staticmethod
    def synthetic(n_ge: int, n_extra_major: int = 0):
        chain = [course(f"M{i}", units=4, prereqs=[f"M{i-1}"] if i else []) for i in range(4)]
        extra = [course(f"X{i}", units=4) for i in range(n_extra_major)]
        ge = [course(f"G{i:02d}", units=3, terms=(F, S, SU)) for i in range(n_ge)]
        ag = TransferAgreement(college="c", university="u", major="m",
                               required_courses=[c.code for c in chain + extra],
                               requirement_groups=[RequirementGroup(id=f"g{i}", name=f"GE {i}", category=GroupCategory.GE,
                                                                    options=[g.code]) for i, g in enumerate(ge)])
        return chain + extra + ge, ag

    def test_heavy_workload_gets_fast_and_balanced_tracks(self):
        catalog, ag = self.synthetic(14, 2)        # 16 + 8 + 42 = 66 units -> 16.5 per semester over 2 years
        result = generate_sep(catalog, ag, StudentProfile())
        self.assertEqual((result["workload"]["classification"], result["workload"]["plan_count"]), ("heavy", 2))
        fast, balanced = result["plans"]
        self.assertEqual([(p["label"], p["kind"]) for p in result["plans"]],
                         [("Fast Track", "fast"), ("Balanced Track", "balanced")])
        regular = lambda p: [s for s in p["semesters"] if s["season"] != "Summer"]   # noqa: E731
        self.assertEqual((len(regular(fast)), len(regular(balanced))), (4, 6))
        self.assertGreater(fast["summary"]["target_units_per_term"], 15)
        for p in (fast, balanced):
            for s in regular(p):
                self.assertLessEqual(s["units"], 18)
                self.assertLessEqual(abs(s["units"] - s["target_units"]), 2 + 1e-9, (p["label"], s))

    def test_heavy_workload_that_cannot_fit_two_years(self):
        catalog, ag = self.synthetic(18)            # 70 units: 4 x (4 + 3k) never reaches 70 within 18/term
        result = generate_sep(catalog, ag, StudentProfile())
        self.assertEqual((result["workload"]["classification"], result["workload"]["plan_count"]), ("heavy", 1))
        (plan,) = result["plans"]
        self.assertEqual((plan["name"], plan["kind"]), ("Your Plan", "fast"))
        self.assertIn("doesn't fit in 2 years", result["workload"]["explanation"])
        self.assertEqual(len(result["workload"]["notes"]), 2)

    def test_boundary_60_units_is_normal(self):
        catalog, ag = self.synthetic(12, 2)         # 16 + 8 + 36 = 60 units -> exactly 15.0 per semester -> normal
        result = generate_sep(catalog, ag, StudentProfile())
        self.assertEqual((result["workload"]["total_required_units"], result["workload"]["classification"]), (60, "normal"))

    def test_cycle_in_data_surfaces_as_cycle_error(self):
        c = cat(course("A", prereqs=["B"]), course("B", prereqs=["A"]))
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=["A"])
        with self.assertRaises(CycleError):
            generate_sep(c, ag, StudentProfile())


if __name__ == "__main__":
    unittest.main()
