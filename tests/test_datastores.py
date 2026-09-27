"""Data stores (datastores/): catalog, prerequisite registry + resolver, GE lists + slot matching, AP layers.
Run with: python -m unittest discover -s tests -v

Real files in data/ are used where a test checks what the app loads; synthetic records test the rules."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from datastores import DataRegistry
from datastores.ap import APStore
from datastores.catalog import CatalogStore
from datastores.common import DATA_DIR, pick_year, split_code, year_status
from datastores.ge import CAL_GETC, GEItem, GEStore, course_item, match_items, parse_ge_areas, pathway_id
from datastores.prerequisites import PrereqRecord, PrereqResolver, PrerequisiteStore

DATA = DataRegistry().load()


def course(cid, timing="before"):
    return {"type": "COURSE", "course_id": f"x:{cid}", "timing": timing}


def rec(key, requirement, status="documented"):
    return PrereqRecord(key, split_code(key), status, requirement, None, (), True, "test")


def resolver(records: dict, units: dict, completed=(), aliases=None):
    return PrereqResolver(lambda k: records.get(k), lambda k: (split_code(k), k, units[k]) if k in units else None,
                          completed, aliases)


# =====================================================================
class CatalogTests(unittest.TestCase):
    def test_real_catalog_loads_by_content(self):
        cat = DATA.catalogs.for_institution("chabot", "2026-2027")
        self.assertEqual((cat.institution_id, cat.academic_year, len(cat.courses)), ("chabot", "2025-2026", 1341))
        self.assertIsNone(DATA.catalogs.for_institution("porterville"))            # no Porterville catalog loaded

    def test_search_is_compact_and_ranked(self):
        cat = DATA.catalogs.for_institution("chabot")
        results = cat.search("calc", limit=10)
        self.assertIn({"code": "MTH 1", "title": "Calculus I", "units": 5.0}, results)
        self.assertEqual(cat.search("mth 1", limit=1)[0]["code"], "MTH 1")          # exact code first
        self.assertTrue(all(set(r) <= {"code", "title", "units", "units_max", "type"} for r in results))

    def test_resolve_canonical_codes(self):
        cat = DATA.catalogs.for_institution("chabot")
        self.assertEqual(cat.resolve("mth1")[0].code, "MTH 1")
        self.assertEqual((cat.resolve("ENGL 1")[0].code, cat.resolve("ENGL 1")[1]), ("ENGL C1000", "formerly"))
        self.assertIsNone(cat.resolve("ZZZ 99"))

    def test_non_catalog_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
            store = CatalogStore()
            store.load_directory(Path(d))
            self.assertEqual(store.catalogs, {})
            self.assertIn("not a course catalog", store.report.skipped[0]["reason"])


# =====================================================================
class PrerequisiteStoreTests(unittest.TestCase):
    def test_registry_loads_with_semantics_preserved(self):
        reg = DATA.prereqs.get("chabot")
        self.assertEqual(len(reg.records), 44)
        mth1 = reg.records["MTH1"]
        self.assertEqual(mth1.status, "policy_dependent")
        self.assertEqual(mth1.requirement["type"], "AND")                          # kept as published, not flattened
        self.assertEqual(reg.records["PHYS4A"].requirement["items"][1]["timing"], "before_or_concurrent")
        self.assertEqual(reg.alias_of["MTH25"], frozenset({"ENGR25", "MTH25", "PHYS25"}))
        self.assertEqual(reg.external["MTH20"]["status"], "not_researched")

    def test_non_registry_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.json").write_text("[]", encoding="utf-8")
            store = PrerequisiteStore()
            store.load_directory(Path(d))
            self.assertEqual(store.institutions, {})
            self.assertTrue(store.report.skipped)


class ResolverTests(unittest.TestCase):
    UNITS = {"A": 4, "B": 3, "C": 5, "D": 3, "E": 4}

    def test_timings(self):
        r = resolver({"A": rec("A", {"type": "AND", "items": [course("B"), course("C", "before_or_concurrent"),
                                                              course("D", "same_term")]})}, self.UNITS)
        out = r.resolve(["A"], anchor=["A", "B", "C", "D"])
        self.assertEqual((out.prereqs["A"], sorted(out.coreqs["A"])), (["B"], ["C", "D"]))
        self.assertTrue(any(i.kind == "same_term" for i in out.reviews))

    def test_or_never_requires_every_alternative(self):
        rule = {"type": "OR", "items": [course("B"), course("C")]}
        none = {"type": "NONE"}
        r = resolver({"A": rec("A", rule), "B": rec("B", none), "C": rec("C", none)}, self.UNITS)
        out = r.resolve(["A"], anchor=["A", "C"])                    # C is already in the plan: use that branch
        self.assertEqual(out.prereqs["A"], ["C"])
        out = r.resolve(["A"], anchor=["A"])                         # neither: add the cheaper one, never both
        self.assertEqual((out.prereqs["A"], list(out.added)), (["B"], ["B"]))
        out = resolver({"A": rec("A", rule)}, self.UNITS, completed=["C"]).resolve(["A"], ["A"])
        self.assertEqual((out.prereqs["A"], out.added), ([], {}))    # satisfied by a completed course

    def test_condition_routes_are_never_guessed(self):
        rule = {"type": "OR", "items": [course("B"), {"type": "CONDITION", "description": "Placement",
                                                      "requires_external_confirmation": True}]}
        out = resolver({"A": rec("A", rule)}, self.UNITS).resolve(["A"], ["A"])
        self.assertEqual((out.prereqs["A"], out.added), ([], {}))    # no course added: placement may apply
        self.assertEqual([i.kind for i in out.reviews], ["condition_route"])
        out = resolver({"A": rec("A", {"type": "CONDITION", "description": "Approval"})}, self.UNITS).resolve(["A"], ["A"])
        self.assertEqual(out.reviews[0].kind, "condition")

    def test_unresolved_and_missing_rules_need_review(self):
        out = resolver({"A": rec("A", None)}, self.UNITS).resolve(["A", "B"], ["A", "B"])
        kinds = {i.course: i.kind for i in out.reviews}
        self.assertEqual(kinds, {"A": "unresolved", "B": "not_loaded"})

    def test_unresearched_or_unschedulable_courses_are_not_added(self):
        r = resolver({"A": rec("A", {"type": "OR", "items": [course("E"), course("Z")]}),
                      "E": rec("E", None, status="not_researched")}, self.UNITS)
        out = r.resolve(["A"], ["A"])
        self.assertEqual(out.added, {})
        self.assertTrue(any(i.kind == "unresolved" for i in out.reviews))
        out = resolver({"A": rec("A", course("Z"))}, self.UNITS).resolve(["A"], ["A"])
        self.assertEqual(out.reviews[0].kind, "unschedulable")        # Z has no units: dropped, reported

    def test_supporting_prerequisites_are_added_transitively(self):
        records = {"A": rec("A", course("B")), "B": rec("B", course("C")), "C": rec("C", {"type": "NONE"})}
        out = resolver(records, self.UNITS).resolve(["A"], ["A"])
        self.assertEqual((out.added, out.prereqs["B"]), ({"B": "A", "C": "B"}, ["C"]))
        out = resolver(records, self.UNITS, completed=["B"]).resolve(["A"], ["A"])
        self.assertEqual(out.added, {})                               # completed prerequisite: nothing to add

    def test_aliases_count_as_completed(self):
        out = resolver({"A": rec("A", course("B"))}, self.UNITS, completed=["X"],
                       aliases={"B": frozenset({"B", "X"})}).resolve(["A"], ["A"])
        self.assertEqual(out.prereqs["A"], [])


# =====================================================================
class GETests(unittest.TestCase):
    def setUp(self):
        self.chabot = DATA.ge.college_list("chabot", CAL_GETC, "2026-2027")

    def test_college_lists_by_institution_and_year(self):
        self.assertEqual((self.chabot.dataset_id, self.chabot.academic_year), ("chabot_cal_getc_2025_2026", "2025-2026"))
        self.assertIsNone(DATA.ge.college_list("chabot", CAL_GETC, "2024-2025"))    # never a later year's list
        porterville = DATA.ge.college_list("porterville", CAL_GETC, "2025-2026")
        self.assertNotEqual(porterville.courses.keys(), self.chabot.courses.keys())  # each college's own list
        self.assertIsNone(DATA.ge.college_list("laney", CAL_GETC, "2025-2026"))
        self.assertEqual((pathway_id("CAL-GETC"), pathway_id("7-Course Pattern")), ("cal_getc", "uc_seven_course"))

    def test_slots_follow_the_published_rules(self):
        self.assertEqual([s.slot_id for s in self.chabot.slots],
                         ["1A", "1B", "1C", "2", "3A", "3B", "4#1", "4#2", "5A", "5B", "6"])
        self.assertEqual(self.chabot.lab["subarea_id"], "5C")
        self.assertEqual(self.chabot.slots[6].discipline_area, 2)                  # Area 4: two disciplines

    def test_matching_counts_each_course_once(self):
        his1 = course_item(self.chabot, "HIS 1")                                     # listed in 3B and 4
        self.assertEqual({o[0] for o in his1.options}, {"3B", "4"})
        states, _ = match_items(self.chabot.slots, True, [his1, course_item(self.chabot, "HIS 2")])
        filled = {s.spec.slot_id: s.satisfied_by.ref for s in states if s.satisfied_by}
        self.assertEqual(len(filled), 2)                                              # one area each, never both
        self.assertEqual(set(filled.values()), {"HIS 1", "HIS 2"})

    def test_area_4_needs_two_disciplines(self):
        items = [course_item(self.chabot, c) for c in ("PSY 2", "PSY 3")]
        states, _ = match_items(self.chabot.slots, True, items)
        self.assertEqual(sum(1 for s in states if s.spec.area_id == "4" and s.satisfied_by), 1)

    def test_lab_counts_with_its_area(self):
        states, lab = match_items(self.chabot.slots, True, [course_item(self.chabot, "PHYS 4A")])
        self.assertEqual(([s.spec.slot_id for s in states if s.satisfied_by], lab.ref), (["5A"], "PHYS 4A"))

    def test_ap_ge_statements(self):
        self.assertEqual(parse_ge_areas("5B and 5C"), (("5B", True),))
        self.assertEqual(parse_ge_areas("3A or 3B"), (("3A", False), ("3B", False)))
        self.assertEqual(parse_ge_areas("4; US-2"), (("4", False),))
        self.assertEqual(parse_ge_areas("3B; IGETC 6A"), (("3B", False),))
        self.assertEqual(parse_ge_areas(None), ())

    def test_uc_seven_course_is_an_admission_requirement(self):
        uc = DATA.ge.uc_seven
        self.assertEqual([r["id"] for r in uc.requirements], ["E", "M", "BR"])
        self.assertEqual(uc.ap_exams["ap:calculus_bc"], ["UC-M"])
        row = next(r for r in DATA.ge.status() if r["pathway"] == "uc_seven_course")
        self.assertEqual(row["college_course_list"], "not_loaded")


# =====================================================================
class APTests(unittest.TestCase):
    ap = DATA.ap

    def test_exam_names_normalize(self):
        for name in ("AP Calculus BC", "Calculus BC", "ap:calculus_bc", "ap calculus bc"):
            self.assertEqual(self.ap.normalize_exam(name), "ap:calculus_bc", name)
        self.assertEqual(self.ap.normalize_exam("AP Physics C: Mechanics"), "ap:physics_c_mechanics")
        self.assertIsNone(self.ap.normalize_exam("AP Underwater Basketry"))
        self.assertEqual(len(self.ap.exam_list()), 44)

    def test_college_effect_only_what_the_chart_says(self):
        bc = self.ap.community_college_effect("chabot", "ap:calculus_bc", 5, set())
        self.assertEqual((bc["status"], bc["waived_courses"]), ("applied", ["MTH 2"]))   # MTH 1 NOT waived
        self.assertTrue(bc["conditions"])
        low = self.ap.community_college_effect("chabot", "ap:calculus_bc", 2, set())
        self.assertEqual((low["status"], low["waived_courses"]), ("below_minimum", []))
        self.assertEqual(self.ap.community_college_effect("chabot", "ap:research", 5, set())["status"], "not_listed")
        self.assertEqual(self.ap.community_college_effect("laney", "ap:calculus_bc", 5, set())["status"], "not_loaded")
        env = self.ap.community_college_effect("chabot", "ap:environmental_science", 4, set())
        self.assertEqual((env["status"], env["ge_areas_text"]), ("no_course_waiver", "5A and 5C"))  # GE only

    def test_choose_one_never_waives_every_option(self):
        phys = self.ap.community_college_effect("chabot", "ap:physics_c_mechanics", 4, {"PHYS7A"})
        self.assertEqual(phys["waived_courses"], ["PHYS 7A"])                   # the one this pathway uses
        both = self.ap.community_college_effect("chabot", "ap:physics_c_mechanics", 4, {"PHYS4A", "PHYS7A"})
        self.assertEqual(len(both["waived_courses"]), 1)
        none = self.ap.community_college_effect("chabot", "ap:physics_c_mechanics", 4, set())
        self.assertEqual((none["status"], none["waived_courses"]), ("choice_not_applied", []))
        self.assertEqual(self.ap.community_college_effect("chabot", "ap:physics_c_mechanics", 4, None)["status"],
                         "choose_one")

    def test_target_campus_effect_is_reported_not_applied(self):
        t = self.ap.target_campus_effect("csueb", "ap:computer_science_a", 3)
        self.assertEqual(t["status"], "requires_context_and_policy_review")
        self.assertEqual(t["rules"][0]["course_credit"], "CS 101")
        self.assertEqual(self.ap.target_campus_effect("csueb", "ap:african_american_studies", 5)["status"], "not_listed")
        self.assertEqual(self.ap.target_campus_effect("csueb", "ap:biology", 2)["status"], "no_rule_for_score")
        self.assertEqual(self.ap.target_campus_effect("stanford", "ap:biology", 5)["status"], "not_loaded")
        manual = self.ap.target_campus_effect("csueb", "ap:3_d_art_and_design", 4)
        self.assertEqual(manual["status"], "manual_review")

    def test_loader_skips_unknown_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.json").write_text("{}", encoding="utf-8")
            store = APStore()
            store.load_directory(Path(d))
            self.assertTrue(store.report.skipped)


class RegistryTests(unittest.TestCase):
    def test_years(self):
        self.assertEqual(pick_year(["2025-2026"], "2026-2027"), "2025-2026")
        self.assertIsNone(pick_year(["2027-2028"], "2026-2027"))
        self.assertEqual(year_status("2025-2026", "2026-2027"), "older_year")
        self.assertEqual(year_status("2026-2027", "2026-2027"), "exact")

    def test_reference_files_are_listed_not_used(self):
        kinds = {r["file"]: r["kind"] for r in DATA.reference}
        self.assertEqual(kinds, {"chabot_2025_2026_pages.json": "raw_catalog_pages",
                                 "chabot_assist_articulation.json": "manual_articulation_template"})
        self.assertFalse(any("reference" in str(p) for p in DATA_DIR.glob("catalogs/*")))

    def test_institutions_from_data(self):
        self.assertEqual(DATA.resolve("Chabot College"), "chabot")
        self.assertEqual(DATA.resolve("UC Davis"), "davis")
        self.assertEqual(set(DATA.colleges), {"chabot", "porterville"})


if __name__ == "__main__":
    unittest.main()
