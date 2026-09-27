"""TAG engine (tag_engine/) on data/uc_tag_requirements_2027_2028.json. Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tag_engine import (
    DATA_DIR, MajorAffiliationRegistry, MajorProfile, TagDatasetError, TagGeStatus, TagStudentProfile, campus_directory,
    evaluate_tag, evaluate_tag_eligibility, load_affiliations, load_registry, load_tag_dataset, match_exclusions,
    normalize_affiliations, normalize_tag_dataset, registry_from, resolve_gpa_rule, thaw,
)
from tag_engine.models import EntryTerm
from tag_engine.selectors import find_campus, major_preparation_for

DATA_FILE = DATA_DIR / "uc_tag_requirements_2027_2028.json"
DATASET = load_tag_dataset(DATA_FILE)
AFFILIATIONS = load_affiliations()
NO_AFFILIATIONS = MajorAffiliationRegistry()          # nothing known about any major's school/college
FALL_2027, SPRING_2028, FALL_2028 = "fall_2027", "spring_2028", "fall_2028"
BREN = "Donald Bren School of Information and Computer Sciences"


def raw_json() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def ev(campus, term=FALL_2027, dataset=DATASET, affiliations=None, **student):
    return evaluate_tag_eligibility(dataset, campus, term, TagStudentProfile(**student), affiliations=affiliations)


def check(evaluation, check_id):
    return next(c for c in evaluation.checks if c.id == check_id)


def campus(cid, dataset=DATASET):
    return find_campus(dataset, cid)


def major(name, **kw) -> MajorProfile:
    return MajorProfile.from_student(TagStudentProfile(intended_major=name, **kw))


# Everything a student could confirm for the shared requirements (keyed by the dataset's requirement ids)
ALL_FACTS = {
    "units_at_tag_submission": 34, "first_english_and_math": {"UC-E": 1, "UC-M": 1},
    "remaining_seven_course_pattern": True, "junior_standing_units": 62,
    "ccc_units_and_last_attendance": {"units": 40, "last_regular_session_ccc": True},
    "last_regular_term_units_needed": 12, "maximum_transfer_units": 70, "minimum_grades_and_standing": True,
    "tag_application": True, "uc_application_and_major_match": True, "transfer_academic_update": True,
}
NO_FLAGS = {r.id: False for r in DATASET.ineligibility_rules}
MODEL_STUDENT = dict(intended_major="Computer Science", uc_transferable_gpa=3.8, requirement_facts=ALL_FACTS,
                     student_flags=NO_FLAGS, major_preparation_complete=True)


def resolved_dataset(*, final: bool = True):
    """A hypothetical copy of the dataset with its blocking issues resolved: a verified seven-course date,
    UC Riverside's and UC Irvine's major-prep courses enumerated, and (if `final`) automation marked sufficient."""
    raw = raw_json()
    item = next(i for i in raw["shared_requirements"]["items"] if i["id"] == "remaining_seven_course_pattern")
    for key in ("review_status", "automatic_deadline_evaluation_allowed", "review_issue_ids", "effective_deadline"):
        item.pop(key, None)
    item["deadline"] = {"season": "spring", "year": 2027, "boundary": "end"}
    raw["automation"].update(sufficient_for_final_automated_TAG_eligibility=final,
                             blocking_issue_ids=[] if final else raw["automation"]["blocking_issue_ids"])
    for c in raw["campuses"]:
        if c["id"] in ("riverside", "irvine"):
            c["major_preparation"]["individual_courses"] = ["(enumerated by a future matrix)"]
    return normalize_tag_dataset(raw)


def next_cycle_raw() -> dict:
    """A synthetic 2028-29 dataset (Fall 2028 entry only), built from the real file's structure."""
    raw = raw_json()
    raw["dataset_id"] = "uc_tag_matrix_2028_2029"
    raw["coverage"]["entry_terms"] = [FALL_2028]
    raw["shared_requirements"]["scope"] = FALL_2028
    for c in raw["campuses"]:
        c["entry_terms"] = [{"season": "fall", "year": 2028, "calendar_term_label": "fall"}]
        c["excluded_majors"]["applies_to_entry_term"] = {"season": "fall", "year": 2028}
        c["excluded_majors"].pop("applies_to_entry_terms", None)
        for key in ("future_exclusions", "spring_2028_requirements"):
            c.pop(key, None)
        c["tag_filing_windows"] = []
        c["coursework_pre_evaluation"]["decision_release_dates"] = []
        c["minimum_uc_transferable_gpa"]["deadline_scope"] = "fall_2028_entry"
    return raw


# =====================================================================
# The JSON is the source of truth, loaded intact
# =====================================================================
class DatasetTests(unittest.TestCase):
    def test_loads_the_matrix(self):
        self.assertEqual(DATASET.dataset_id, "uc_tag_matrix_2027_2028")
        self.assertEqual([t.key for t in DATASET.coverage_terms], [FALL_2027, SPRING_2028])
        self.assertEqual([t.key for t in DATASET.shared_scope], [FALL_2027])
        self.assertEqual(len(DATASET.shared_requirements), 12)
        self.assertEqual(len(DATASET.ineligibility_rules), 4)
        self.assertFalse(DATASET.automation.sufficient_for_final_decision)
        self.assertEqual(DATASET.automation.unresolved_rule_result, "needs_review")

    def test_campus_list_comes_from_the_json(self):
        names = sorted(c["name"] for c in raw_json()["campuses"])
        self.assertEqual([c["name"] for c in campus_directory(load_registry())], names)
        self.assertEqual(names, ["UC Davis", "UC Irvine", "UC Merced", "UC Riverside", "UC Santa Barbara", "UC Santa Cruz"])
        merced = next(c for c in campus_directory(load_registry()) if c["id"] == "merced")
        self.assertEqual([t["key"] for t in merced["entry_terms"]], [FALL_2027, SPRING_2028])

    def test_source_json_is_never_modified(self):
        before = hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
        for cid in ("davis", "irvine", "merced", "riverside", "santa_barbara", "santa_cruz", "UC Berkeley", None):
            for term in (FALL_2027, SPRING_2028, FALL_2028, "fall_2029"):
                for name in ("Computer Science", "Psychology", "Public Health", "Dance", None):
                    ev(cid, term, intended_major=name, requirement_facts=ALL_FACTS, student_flags=NO_FLAGS)
        self.assertEqual(thaw(DATASET.raw), raw_json())
        self.assertEqual(hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(), before)
        with self.assertRaises(TypeError):
            DATASET.raw["campuses"][0]["name"] = "changed"          # read-only view

    def test_null_values_stay_unresolved(self):
        irvine = campus("irvine")
        self.assertIsNone(irvine.major_preparation.individual_courses)          # not an empty list
        self.assertIsNone(irvine.major_preparation.separate_gpa_thresholds)
        self.assertIsNone(campus("santa_cruz").major_guarantee.screening_major_names)
        self.assertIsNone(campus("merced").term_requirements[SPRING_2028].application_dates)
        seven = next(s for s in DATASET.shared_requirements if s.id == "remaining_seven_course_pattern")
        self.assertIsNone(seven.deadline)                                        # effective_deadline is null
        self.assertEqual(seven.printed_deadline.label, "By Spring 2026")        # kept as printed

    def test_invalid_data_is_rejected_with_its_location(self):
        raw = raw_json()
        raw["campuses"][3]["minimum_uc_transferable_gpa"]["rules"][0]["minimum_gpa"] = 5
        with self.assertRaisesRegex(TagDatasetError, r"campuses\[3\]\(riverside\)\.minimum_uc_transferable_gpa"):
            normalize_tag_dataset(raw)
        raw = raw_json()
        raw["automation"]["blocking_issue_ids"].append("no_such_issue")
        with self.assertRaisesRegex(TagDatasetError, "no_such_issue"):
            normalize_tag_dataset(raw)
        raw = raw_json()
        raw["automation"]["unresolved_rule_result"] = "eligible"
        with self.assertRaises(TagDatasetError):
            normalize_tag_dataset(raw)

    def test_broken_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "uc_tag_requirements_2027_2028.json").write_bytes(DATA_FILE.read_bytes())
            (Path(d) / "uc_tag_requirements_broken.json").write_text("{ not json", encoding="utf-8")
            with self.assertLogs("uvicorn.error", level="ERROR"):
                registry = load_registry(d)
        self.assertEqual([x.dataset_id for x in registry.datasets], ["uc_tag_matrix_2027_2028"])
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("not valid JSON", registry.errors[0])

    def test_no_data_means_needs_review(self):
        e = evaluate_tag("riverside", FALL_2027, TagStudentProfile(intended_major="Computer Science"),
                         registry=registry_from([]))
        self.assertEqual((e.status, e.headline), ("needs_review", "TAG data unavailable"))

    def test_a_new_annual_dataset_needs_no_code(self):
        future = normalize_tag_dataset(next_cycle_raw())
        registry = registry_from([DATASET, future])
        self.assertIs(registry.for_term(FALL_2028), future)
        self.assertIs(registry.for_term("Fall 2027"), DATASET)
        self.assertIs(registry.latest, future)
        e = evaluate_tag("riverside", FALL_2028, TagStudentProfile(intended_major="Computer Science"), registry=registry)
        self.assertEqual(e.rules_term.key, FALL_2028)                 # evaluated, not a reference
        self.assertIsNone(e.reference)
        self.assertEqual(e.minimum_gpa.minimum_gpa, 3.6)


# =====================================================================
# The 12 required scenarios
# =====================================================================
class RequiredScenarioTests(unittest.TestCase):
    def test_01_davis_excluded_major(self):
        for name in ("Computer Science", "Data Science", "Business"):
            e = ev("davis", intended_major=name)
            self.assertEqual(e.status, "ineligible", name)
            self.assertEqual(check(e, "campus.major_exclusion").status, "not_met")
            self.assertIn(f"{name} is excluded from TAG at UC Davis for Fall 2027 entry", e.blocking_reasons[0])
        self.assertEqual(ev("davis", intended_major="Mechanical Engineering").status, "needs_review")

    def test_02_irvine_school_level_exclusion(self):
        e = ev("irvine", intended_major="Computer Science", major_school=BREN)
        self.assertEqual(e.status, "ineligible")
        self.assertIn(f"Computer Science is in UC Irvine's {BREN}, whose majors are excluded", e.blocking_reasons[0])
        # Without the major's school (stated or in the affiliation data), the school-wide exclusion can't be
        # ruled out: needs review, never "eligible"
        e = ev("irvine", intended_major="Computer Science", affiliations=NO_AFFILIATIONS)
        self.assertEqual((e.status, check(e, "campus.major_exclusion").status), ("needs_review", "manual_review"))
        e = ev("irvine", intended_major="Computer Engineering", major_school="Henry Samueli School of Engineering")
        self.assertEqual(check(e, "campus.major_exclusion").status, "met")

    def test_03_irvine_fall_2028_exclusion_not_applied_to_fall_2027(self):
        samueli = dict(intended_major="Computer Engineering", major_school="Henry Samueli School of Engineering")
        fall_2027 = ev("irvine", FALL_2027, **samueli)
        self.assertEqual(check(fall_2027, "campus.major_exclusion").status, "met")
        self.assertNotEqual(fall_2027.status, "ineligible")
        self.assertFalse(any(m.basis != "current" for m in match_exclusions(campus("irvine"), EntryTerm.parse(FALL_2027),
                                                                            major("Computer Engineering"))))
        fall_2028 = ev("irvine", FALL_2028, **samueli)
        self.assertEqual(fall_2028.status, "ineligible")
        self.assertIn("starting with Fall 2028 entry", fall_2028.blocking_reasons[0])

    def test_04_riverside_cs_major_gpa_override(self):
        res = resolve_gpa_rule(campus("riverside"), major("Computer Science"), EntryTerm.parse(FALL_2027))
        self.assertEqual((res.status, res.minimum_gpa, res.rule.scope_type), ("resolved", 3.6, "major"))
        self.assertIn("BCOE", res.rule.parent_scope_name)          # overrides BCOE's 3.0, not "the lowest value"
        self.assertEqual(res.deadline, "End of Summer 2026")
        self.assertEqual(ev("riverside", intended_major="Computer Science", uc_transferable_gpa=3.5).status, "ineligible")
        e = ev("riverside", intended_major="Computer Science", uc_transferable_gpa=3.72)
        self.assertEqual(check(e, "campus.gpa").status, "met")

    def test_05_riverside_computer_engineering_override(self):
        res = resolve_gpa_rule(campus("riverside"), major("Computer Engineering"))
        self.assertEqual((res.status, res.minimum_gpa), ("resolved", 3.3))
        # Exact major names only: 'Computer Science w/ Business Applications' has its own 3.3, not CS's 3.6
        self.assertEqual(resolve_gpa_rule(campus("riverside"), major("Computer Science w/ Business Applications")).minimum_gpa, 3.3)

    def test_06_ucsb_college_of_engineering_excluded(self):
        e = ev("santa_barbara", intended_major="Computer Science", major_college="College of Engineering")
        self.assertEqual(e.status, "ineligible")
        self.assertIn("Computer Science is in UC Santa Barbara's College of Engineering, whose majors are excluded",
                      e.blocking_reasons[0])
        # A differently worded college is a possible match (needs review), not a silent pass
        e = ev("santa_barbara", intended_major="Computer Science", major_college="Robert Mehrabian College of Engineering")
        self.assertEqual(check(e, "campus.major_exclusion").status, "manual_review")
        e = ev("santa_barbara", intended_major="Economics", major_college="College of Letters and Science")
        self.assertEqual(check(e, "campus.major_exclusion").status, "met")

    def test_07_ucsc_has_no_excluded_majors(self):
        ucsc = campus("santa_cruz")
        self.assertEqual(ucsc.exclusions.rules, ())
        self.assertTrue(ucsc.exclusions.all_majors_open)
        for name in ("Computer Science", "Art", "Nursing Science"):
            self.assertEqual(match_exclusions(ucsc, EntryTerm.parse(FALL_2027), major(name)), [])
            self.assertEqual(check(ev("santa_cruz", intended_major=name), "campus.major_exclusion").status, "met")

    def test_08_merced_spring_2028_is_separate_from_fall_2027(self):
        spring = ev("merced", SPRING_2028, intended_major="Psychology")
        ids = [c.id for c in spring.checks]
        self.assertEqual(spring.rules_term.key, SPRING_2028)
        self.assertIn("term.spring_2028.uc_e_2", ids)
        self.assertFalse(any(i.startswith("shared.") for i in ids), "fall-dated shared criteria must not be copied")
        self.assertEqual(check(spring, "term.spring_2028.uc_e_1").deadline, "End of Spring 2027")
        apps = check(spring, "term.spring_2028.application_dates")
        self.assertEqual((apps.status, apps.review_issue_ids), ("manual_review", ["spring_2028_unenumerated_application_details"]))
        self.assertEqual(check(spring, "timeline.filing_window").required, "May 1–31, 2027 (year inferred)")
        self.assertEqual(check(spring, "timeline.decision_release").required, "Jul 1, 2027 (year inferred)")
        self.assertNotEqual(check(spring, "campus.gpa").deadline, "End of Summer 2026")     # printed for fall 2027 only
        fall = ev("merced", FALL_2027, intended_major="Psychology")
        self.assertIn("shared.units_at_tag_submission", [c.id for c in fall.checks])
        self.assertEqual(check(fall, "timeline.decision_release").required, "Nov 15, 2026 (year inferred)")
        # Other campuses have no Spring 2028 TAG in this matrix
        davis = ev("davis", SPRING_2028, intended_major="Psychology")
        self.assertEqual(davis.status, "not_applicable")
        self.assertIn("Spring 2028 TAG is offered at UC Merced only", davis.summary)

    def test_09_unknown_or_unresolved_is_needs_review(self):
        e = ev("santa_cruz", intended_major="Computer Science")
        self.assertEqual(e.status, "needs_review")
        self.assertEqual(check(e, "eligibility.disqualifying_conditions").status, "unknown")   # not assumed false
        self.assertEqual(check(e, "campus.gpa").status, "unknown")                             # not assumed enough
        self.assertEqual(check(e, "campus.major_guarantee").status, "manual_review")           # screening list is null
        self.assertEqual(ev("davis", intended_major="Computer Science",
                            student_flags={**NO_FLAGS, "degree_already_earned": True}).status, "ineligible")
        merced_cs = resolve_gpa_rule(campus("merced"), major("Computer Science"))
        self.assertEqual((merced_cs.status, merced_cs.minimum_gpa), ("ambiguous", None))      # never the lowest value
        self.assertEqual(sorted(t.minimum_gpa for t in merced_cs.candidates), [2.8, 2.9, 3.0])

    def test_10_seven_course_deadline_never_passes_or_fails_automatically(self):
        for reported in (True, False, None):
            facts = {**ALL_FACTS, "remaining_seven_course_pattern": reported} if reported is not None else {}
            e = ev("riverside", intended_major="Computer Science", requirement_facts=facts)
            c = check(e, "shared.remaining_seven_course_pattern")
            self.assertEqual(c.status, "manual_review")
            self.assertEqual(c.review_issue_ids, ["seven_course_deadline_year"])
            self.assertIn("requires official verification", c.explanation)
            self.assertNotEqual(e.status, "ineligible")
            self.assertNotIn(c.explanation, e.blocking_reasons)
        self.assertIn("seven_course_deadline_year", [r.id for r in e.review_items])

    def test_11_missing_major_prep_details_are_not_empty_requirements(self):
        for c in DATASET.campuses:
            finding = major_preparation_for(c, major("Computer Science"))
            self.assertIsNone(finding.courses, c.id)
        e = ev("riverside", **MODEL_STUDENT)                    # a student who has confirmed everything else
        prep = check(e, "campus.major_preparation")
        self.assertEqual((prep.status, prep.required), ("manual_review", "Courses not listed in the TAG matrix"))
        self.assertIn("must be verified", prep.explanation)
        self.assertTrue(prep.urls)
        self.assertEqual(e.status, "needs_review")              # so never "eligible" with this dataset

    def test_12_no_tag_university_is_not_applicable(self):
        for choice in (None, "None", "", "  "):
            e = ev(choice, intended_major="Computer Science")
            self.assertEqual((e.status, e.checks), ("not_applicable", []))
        self.assertEqual(evaluate_tag(None, FALL_2027, registry=load_registry()).status, "not_applicable")


# =====================================================================
# Combination, entry terms and matchers
# =====================================================================
class EvaluationTests(unittest.TestCase):
    def test_eligible_only_when_every_requirement_is_met_and_the_data_is_final(self):
        e = ev("riverside", dataset=resolved_dataset(), **MODEL_STUDENT)
        self.assertEqual(e.status, "eligible", [(c.id, c.status) for c in e.checks if c.status not in ("met", "info")])
        self.assertEqual(e.headline, "TAG requirements appear to be met")
        self.assertNotIn("guaranteed admission", e.summary.lower())
        # Shared AND campus: one failing campus rule, or one failing shared rule, makes it ineligible
        self.assertEqual(ev("riverside", dataset=resolved_dataset(), **{**MODEL_STUDENT, "uc_transferable_gpa": 3.5}).status,
                         "ineligible")
        facts = {**ALL_FACTS, "junior_standing_units": 55}
        self.assertEqual(ev("riverside", dataset=resolved_dataset(), **{**MODEL_STUDENT, "requirement_facts": facts}).status,
                         "ineligible")
        # The same student against data the matrix itself marks as not final: needs review, not eligible
        e = ev("riverside", dataset=resolved_dataset(final=False), **MODEL_STUDENT)
        self.assertEqual(e.status, "needs_review")
        self.assertIn("isn't complete enough for a final automated decision", e.summary)
        # Even with final data, missing student information is needs_review, never assumed fine
        e = ev("riverside", dataset=resolved_dataset(), intended_major="Computer Science")
        self.assertEqual(e.status, "needs_review")
        self.assertEqual(check(e, "eligibility.disqualifying_conditions").status, "unknown")

    def test_ambiguous_gpa_is_never_decided_by_the_lowest_minimum(self):
        # UC Merced sets the minimum by school (2.8-3.0) and has no "Computer Science" major (it's Computer
        # Science and Engineering), so which school applies is unknown; a 2.85 GPA meets only the lowest one
        between = check(ev("merced", intended_major="Computer Science", uc_transferable_gpa=2.85), "campus.gpa")
        self.assertEqual(between.status, "manual_review")
        self.assertEqual(between.required, "2.8–3.0, depending on the college or school")
        self.assertIn("depends on which one applies", between.explanation)
        # UC Davis Physics, with its college unknown: 3.2 or 3.5 by college
        davis = ev("davis", dataset=resolved_dataset(), affiliations=NO_AFFILIATIONS, intended_major="Physics",
                   uc_transferable_gpa=3.3, requirement_facts=ALL_FACTS, student_flags=NO_FLAGS)
        self.assertEqual(check(davis, "campus.gpa").status, "manual_review")
        self.assertEqual(davis.minimum_gpa.minimum_gpa, None)
        # ... and with the affiliation data: Letters and Science's 3.2
        davis = ev("davis", dataset=resolved_dataset(), intended_major="Physics", uc_transferable_gpa=3.3,
                   requirement_facts=ALL_FACTS, student_flags=NO_FLAGS)
        self.assertEqual((check(davis, "campus.gpa").status, davis.minimum_gpa.minimum_gpa), ("met", 3.2))

    def test_uncovered_term_uses_the_latest_rules_as_reference_only(self):
        e = ev("davis", FALL_2028, intended_major="Computer Science")
        self.assertEqual((e.status, e.rules_term), ("needs_review", None))    # not "ineligible" from 2027 rules
        self.assertEqual(e.reference.rules_term.key, FALL_2027)
        self.assertEqual(e.reference.status, "ineligible")
        self.assertEqual([c.id for c in e.checks], ["coverage"])
        self.assertIn("For reference, under UC Davis's Fall 2027 rules", e.summary)

    def test_non_tag_campus(self):
        e = ev("UC Berkeley", intended_major="Computer Science")
        self.assertEqual(e.status, "not_applicable")
        self.assertIn("can still be your target university", e.summary)
        self.assertEqual(ev("UC Riverside", intended_major="Computer Science").campus.id, "riverside")

    def test_gpa_specificity(self):
        ucr = campus("riverside")
        ambiguous = resolve_gpa_rule(ucr, major("Electrical Engineering"))
        self.assertEqual((ambiguous.status, ambiguous.minimum_gpa), ("ambiguous", None))
        for college in ("Marlan and Rosemary Bourns College of Engineering (BCOE)", "BCOE"):
            self.assertEqual(resolve_gpa_rule(ucr, major("Electrical Engineering", major_college=college)).minimum_gpa, 3.0)
        # A stated college that isn't the exception's parent: the exception doesn't apply
        cs_in_cnas = resolve_gpa_rule(ucr, major("Computer Science", major_college="College of Natural and Agricultural Sciences (CNAS)"))
        self.assertEqual(cs_in_cnas.minimum_gpa, 2.8)
        self.assertEqual(resolve_gpa_rule(campus("merced"), major("Psychology")).minimum_gpa, 3.0)   # over SSHA 2.8
        self.assertEqual(resolve_gpa_rule(campus("irvine"), major("Anything")).rule.scope_type, "campus")
        self.assertEqual(resolve_gpa_rule(campus("davis"), major("Physics", major_college="College of Engineering")).minimum_gpa, 3.5)

    def test_exclusion_scopes(self):
        term = EntryTerm.parse(FALL_2027)
        davis, ucsb = campus("davis"), campus("santa_barbara")
        self.assertEqual([m.result for m in match_exclusions(davis, term, major("Undeclared - Physical Sciences"))], ["match"])
        self.assertEqual(match_exclusions(davis, term, major("Mechanical Engineering")), [])
        self.assertEqual([(m.result, m.effect) for m in match_exclusions(davis, term, major("Landscape Architecture"))],
                         [("match", "pre_major_only")])
        e = ev("davis", intended_major="Landscape Architecture")
        self.assertEqual((check(e, "campus.major_exclusion").status, check(e, "campus.major_guarantee").status),
                         ("met", "manual_review"))
        dance = lambda degree: {m.result for m in match_exclusions(ucsb, term, major("Dance", major_degree=degree,
                                                                                      major_college="College of Letters & Science"))}
        self.assertEqual(dance("B.A."), {"match"})
        self.assertEqual(dance("B.S."), set())
        self.assertEqual(dance(None), {"possible"})
        self.assertEqual({m.result for m in match_exclusions(ucsb, term, major("Theater", major_degree="B.A.",
                                                                              major_college="College of Letters & Science"))},
                         {"possible"})                    # excluded only for the Theater Design emphasis

    def test_general_education_rules_stay_separate_from_the_ge_planner(self):
        ph = dict(intended_major="Public Health")
        self.assertEqual(check(ev("merced", ge=TagGeStatus(pattern="7-Course Pattern", status="planned"), **ph),
                               "campus.general_education").status, "not_met")
        self.assertEqual(check(ev("merced", ge=TagGeStatus(pattern="CAL-GETC", status="planned",
                                                          completion_term="Spring 2027"), **ph),
                               "campus.general_education").status, "manual_review")
        self.assertEqual(check(ev("merced", ge=TagGeStatus(pattern="IGETC", status="completed"), **ph),
                               "campus.general_education").status, "met")
        self.assertEqual(check(ev("riverside", intended_major="Computer Science"), "campus.general_education").status, "met")
        rec = check(ev("riverside", affiliations=NO_AFFILIATIONS, intended_major="Business Administration"),
                    "campus.general_education")
        self.assertIn("If your major is in the School of Business", rec.explanation)      # school unknown
        rec = check(ev("riverside", intended_major="Business Administration"), "campus.general_education")
        self.assertIn("is highly recommended for the School of Business", rec.explanation)  # from the affiliation data


# =====================================================================
# School/college-level rules via major affiliations (tests/test_major_affiliations.py covers the resolver)
# =====================================================================
SOURCE = {"test": {"title": "Test fixture", "url": "https://example.edu/majors"}}


class MajorAffiliationTagTests(unittest.TestCase):
    """The planner sends only the major's name; the affiliation data supplies its school/college."""

    COMPLETE = {k: v for k, v in MODEL_STUDENT.items() if k != "intended_major"}   # everything but the major

    def test_irvine_computer_science_fall_2027_is_excluded_by_its_school(self):
        e = ev("irvine", intended_major="Computer Science")
        self.assertEqual(e.status, "ineligible")
        self.assertEqual(e.headline, "TAG unavailable for Computer Science at UC Irvine")
        exclusion = check(e, "campus.major_exclusion")
        self.assertEqual(exclusion.status, "not_met")
        self.assertEqual(exclusion.explanation,
                         f"Computer Science is in UC Irvine's {BREN}, whose majors are excluded from TAG for Fall 2027 entry.")
        self.assertIn("https://catalogue.uci.edu/undergraduatedegrees/", exclusion.urls)
        self.assertEqual((e.major_affiliation.status, e.major_affiliation.school.id), ("resolved", "donald_bren_ics"))
        resolved = MajorProfile.from_student(TagStudentProfile(intended_major="Computer Science"),
                                             AFFILIATIONS.lookup("irvine", "Computer Science"))
        [m] = match_exclusions(campus("irvine"), EntryTerm.parse(FALL_2027), resolved)
        self.assertEqual((m.result, m.basis, m.rule.scope_type, m.rule.name),
                         ("match", "current", "all_majors_in_school", BREN))
        # The same verdict with the most complete student profile: the school rule alone decides it
        e = ev("irvine", dataset=resolved_dataset(), intended_major="Computer Science", **self.COMPLETE)
        self.assertEqual(e.status, "ineligible")

    def test_other_bren_majors_are_excluded(self):
        for name in ("Data Science", "Informatics", "Software Engineering", "Game Design and Interactive Media",
                     "Information and Computer Science"):
            e = ev("irvine", intended_major=name)
            self.assertEqual(e.status, "ineligible", name)
            self.assertIn(f"{name} is in UC Irvine's {BREN}", e.blocking_reasons[0])

    def test_irvine_majors_outside_bren_are_not_excluded_by_the_school_rule(self):
        term = EntryTerm.parse(FALL_2027)
        for name in ("Mathematics", "Computer Engineering", "Economics", "Film and Media Studies", "Psychology"):
            profile = MajorProfile.from_student(TagStudentProfile(intended_major=name), AFFILIATIONS.lookup("irvine", name))
            self.assertEqual(match_exclusions(campus("irvine"), term, profile), [], name)
            self.assertEqual(check(ev("irvine", intended_major=name), "campus.major_exclusion").status, "met", name)
        # Psychology is in two schools (B.A. Social Ecology, B.S. Social Sciences), neither of them Bren
        self.assertEqual(ev("irvine", intended_major="Psychology").major_affiliation.status, "ambiguous")
        # Nothing else holds it back once every other requirement is confirmed
        e = ev("irvine", dataset=resolved_dataset(), intended_major="Mathematics", **self.COMPLETE)
        self.assertEqual(e.status, "eligible", [(c.id, c.status) for c in e.checks if c.status not in ("met", "info")])

    def test_ucsb_engineering_majors_are_excluded_by_their_college(self):
        for name in ("Computer Science", "Computer Engineering", "Electrical Engineering"):
            e = ev("santa_barbara", intended_major=name)
            self.assertEqual((e.status, check(e, "campus.major_exclusion").status), ("ineligible", "not_met"), name)
            self.assertIn(f"{name} is in UC Santa Barbara's College of Engineering, whose majors are excluded",
                          e.blocking_reasons[0])
            self.assertEqual(e.major_affiliation.college.name, "Robert Mehrabian College of Engineering")
        # The official name alone only "may be" the matrix's College of Engineering: the recorded alias decides it
        official_only = normalize_affiliations({"schema_version": "1.0.0", "sources": SOURCE, "campuses": [
            {"id": "santa_barbara", "name": "UC Santa Barbara", "organizations": [
                {"id": "engineering", "type": "college", "name": "Robert Mehrabian College of Engineering",
                 "sources": ["test"], "majors": [{"name": "Computer Science"}]}]}]})
        e = ev("santa_barbara", affiliations=official_only, intended_major="Computer Science")
        self.assertEqual(check(e, "campus.major_exclusion").status, "manual_review")
        # UC Santa Barbara Economics is in Letters and Science: neither excluded college
        self.assertEqual(check(ev("santa_barbara", intended_major="Economics"), "campus.major_exclusion").status, "met")

    def test_riverside_computer_science_uses_its_major_gpa_rule(self):
        e = ev("riverside", intended_major="Computer Science")
        self.assertEqual(check(e, "campus.major_exclusion").status, "met")      # not excluded by a college rule
        self.assertEqual(e.status, "needs_review")                              # ... and evaluated as usual
        self.assertFalse(e.blocking_reasons)
        self.assertEqual(e.major_affiliation.college.name, "Marlan and Rosemary Bourns College of Engineering")
        gpa = e.minimum_gpa
        self.assertEqual((gpa.status, gpa.minimum_gpa, gpa.rule.scope_type), ("resolved", 3.6, "major"))
        self.assertEqual(gpa.rule.parent_scope_name, "Marlan and Rosemary Bourns College of Engineering (BCOE)")
        self.assertEqual(ev("riverside", intended_major="Computer Science", uc_transferable_gpa=3.5).status, "ineligible")
        # A BCOE major without its own minimum now resolves to the college's (it was ambiguous without the data)
        ee = ev("riverside", intended_major="Electrical Engineering").minimum_gpa
        self.assertEqual((ee.status, ee.minimum_gpa, ee.rule.scope_type), ("resolved", 3.0, "college"))
        self.assertEqual(ev("riverside", affiliations=NO_AFFILIATIONS, intended_major="Electrical Engineering")
                         .minimum_gpa.status, "ambiguous")

    def test_college_and_school_gpa_rules_use_the_affiliation(self):
        cases = [("davis", "Physics", 3.2, "College of Letters and Science"),
                 ("davis", "Computer Engineering", 3.5, "College of Engineering"),
                 ("merced", "Physics", 2.9, "School of Natural Science"),            # the matrix's spelling (an alias)
                 ("merced", "Computer Science and Engineering", 3.0, "School of Engineering")]
        for cid, name, minimum, scope in cases:
            gpa = ev(cid, intended_major=name).minimum_gpa
            self.assertEqual((gpa.status, gpa.minimum_gpa, gpa.rule.scope_name), ("resolved", minimum, scope), (cid, name))
        # UC Davis Business is in the Graduate School of Management, which the matrix gives no minimum
        self.assertEqual(ev("davis", intended_major="Business").minimum_gpa.status, "unresolved")

    def test_unknown_affiliation_needs_review_never_eligible(self):
        final = resolved_dataset()
        # A major the affiliation data doesn't list at UC Irvine: its school could be Bren
        e = ev("irvine", dataset=final, intended_major="Underwater Basket Weaving", **self.COMPLETE)
        self.assertEqual(e.status, "needs_review")
        self.assertEqual([c.id for c in e.checks if c.status not in ("met", "info")], ["campus.major_exclusion"])
        self.assertIn("academic school/college affiliation for this major could not be verified",
                      check(e, "campus.major_exclusion").explanation)
        self.assertEqual(e.major_affiliation.status, "unresolved")
        # The same student and major flip from eligible to needs_review when the affiliation is unknown
        self.assertEqual(ev("irvine", dataset=final, intended_major="Mathematics", **self.COMPLETE).status, "eligible")
        e = ev("irvine", dataset=final, affiliations=NO_AFFILIATIONS, intended_major="Mathematics", **self.COMPLETE)
        self.assertEqual((e.status, check(e, "campus.major_exclusion").status), ("needs_review", "manual_review"))
        # Run jointly by Bren and another school: the data says so, and it stays needs_review
        e = ev("irvine", intended_major="Computer Science and Engineering")
        self.assertEqual((e.status, check(e, "campus.major_exclusion").status), ("needs_review", "manual_review"))
        self.assertIn("administered by faculty from two academic units", check(e, "campus.major_exclusion").explanation)
        # Offered by two colleges, one excluded: only that college's rule needs review
        e = ev("santa_barbara", intended_major="Mathematics")
        exclusion = check(e, "campus.major_exclusion")
        self.assertEqual((e.status, exclusion.status), ("needs_review", "manual_review"))
        self.assertIn("excludes every major in the College of Creative Studies", exclusion.explanation)
        self.assertNotIn("College of Engineering", exclusion.explanation)
        self.assertIn("offers Mathematics in the College of Letters and Science and the College of Creative Studies",
                      exclusion.explanation)

    def test_the_students_own_school_or_college_wins(self):
        e = ev("irvine", intended_major="Computer Science", major_school="The Henry Samueli School of Engineering")
        self.assertEqual(check(e, "campus.major_exclusion").status, "met")
        self.assertEqual((e.major_affiliation.status, e.major_affiliation.school.name),
                         ("stated", "The Henry Samueli School of Engineering"))

    def test_fall_2028_exclusions_are_not_applied_to_fall_2027(self):
        for name in ("Computer Engineering", "Economics"):               # on the Fall 2028 list only
            fall_2027 = ev("irvine", FALL_2027, intended_major=name)
            self.assertEqual(check(fall_2027, "campus.major_exclusion").status, "met", name)
            self.assertNotEqual(fall_2027.status, "ineligible", name)
            fall_2028 = ev("irvine", FALL_2028, intended_major=name)
            self.assertEqual(fall_2028.status, "ineligible", name)
            self.assertIn("starting with Fall 2028 entry", fall_2028.blocking_reasons[0])
        # The Bren school exclusion continues alongside the Fall 2028 list
        self.assertEqual(ev("irvine", FALL_2028, intended_major="Computer Science").status, "ineligible")

    def test_college_rules_match_majors_of_a_school_within_the_college(self):
        nested = normalize_affiliations({"schema_version": "1.0.0", "sources": SOURCE, "campuses": [
            {"id": "santa_barbara", "name": "UC Santa Barbara", "organizations": [
                {"id": "ccs", "type": "college", "name": "College of Creative Studies", "sources": ["test"], "majors": []},
                {"id": "imaginary", "type": "school", "name": "School of Imaginary Studies", "parent_id": "ccs",
                 "sources": ["test"], "majors": [{"name": "Imaginary Studies", "degrees": ["B.A."]}]}]}]})
        e = ev("santa_barbara", affiliations=nested, intended_major="Imaginary Studies (B.A.)")
        self.assertEqual(e.status, "ineligible")
        self.assertIn("UC Santa Barbara's College of Creative Studies, whose majors are excluded", e.blocking_reasons[0])
        self.assertEqual((e.major_affiliation.school.id, e.major_affiliation.college.id), ("imaginary", "ccs"))

    def test_broken_affiliation_data_means_needs_review(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "uc_major_affiliations.json"
            broken.write_text("{ not json", encoding="utf-8")
            with self.assertLogs("uvicorn.error", level="ERROR"):
                empty = load_affiliations(broken)
        self.assertEqual(empty.campus_ids, ())
        self.assertEqual(ev("irvine", affiliations=empty, intended_major="Computer Science").status, "needs_review")


if __name__ == "__main__":
    unittest.main()
