"""Major affiliations (tag_engine/affiliations.py) on data/uc_major_affiliations.json.
Run with: python -m unittest discover -s tests -v   (TAG integration: tests/test_tag_engine.py)"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tag_engine import (
    AFFILIATIONS_FILE, DATA_DIR, TagDatasetError, TagStudentProfile, load_affiliations, load_tag_dataset,
    match_exclusions, normalize_affiliations,
)
from tag_engine.affiliations import UNVERIFIED
from tag_engine.models import EntryTerm
from tag_engine.selectors import UNIT_EXCLUSION_SCOPES, CAMPUS_WIDE_SCOPES, MajorProfile, find_campus, unit_relation

AFFILIATIONS = load_affiliations()
TAG = load_tag_dataset(DATA_DIR / "uc_tag_requirements_2027_2028.json")
BREN = "Donald Bren School of Information and Computer Sciences"
SOURCE = {"test": {"title": "Test fixture", "url": "https://example.edu/majors"}}


def fixture(*organizations, unresolved=()):
    return normalize_affiliations({"schema_version": "1.0.0", "sources": SOURCE, "campuses": [
        {"id": "irvine", "name": "UC Irvine", "organizations": list(organizations), "unresolved": list(unresolved)}]})


def raw() -> dict:
    return json.loads(AFFILIATIONS_FILE.read_text(encoding="utf-8"))


class ResolverTests(unittest.TestCase):
    def test_required_affiliations(self):
        cases = [
            ("UC Irvine", "Computer Science", "school", "donald_bren_ics", BREN),
            ("UC Irvine", "Data Science", "school", "donald_bren_ics", BREN),
            ("UC Santa Barbara", "Computer Science", "college", "engineering", "Robert Mehrabian College of Engineering"),
            ("UC Santa Barbara", "Computer Engineering", "college", "engineering", "Robert Mehrabian College of Engineering"),
            ("UC Riverside", "Computer Science", "college", "bourns_engineering",
             "Marlan and Rosemary Bourns College of Engineering"),
            ("UC Riverside", "Computer Engineering", "college", "bourns_engineering",
             "Marlan and Rosemary Bourns College of Engineering"),
            ("UC Davis", "Computer Engineering", "college", "engineering", "College of Engineering"),
            ("UC Merced", "Computer Science and Engineering", "school", "engineering", "School of Engineering"),
        ]
        for university, major, kind, oid, name in cases:
            a = AFFILIATIONS.resolve(university, major)
            self.assertIsNotNone(a, (university, major))
            org = a.school if kind == "school" else a.college
            self.assertEqual((org.type, org.id, org.name), (kind, oid, name), (university, major))
            self.assertEqual(a.organization, org)
            self.assertTrue(a.source_urls, (university, major))
        # UC Santa Barbara's college keeps its official name; the TAG matrix's name is an alias
        self.assertIn("College of Engineering", AFFILIATIONS.resolve("UC Santa Barbara", "Computer Science").unit_names)

    def test_as_dict(self):
        a = AFFILIATIONS.resolve("UC Irvine", "Computer Science", "B.S.").as_dict()
        self.assertEqual({k: v for k, v in a.items() if k != "source_urls"},
                         {"campus_id": "irvine", "school_id": "donald_bren_ics", "school_name": BREN, "college_id": None,
                          "college_name": None, "major": "Computer Science", "degree": "B.S."})
        self.assertIn("https://catalogue.uci.edu/donaldbrenschoolofinformationandcomputersciences/"
                      "departmentofcomputerscience/computerscience_bs/", a["source_urls"])

    def test_campus_identity_uses_the_tag_campus_ids(self):
        for name in ("UC Irvine", "irvine", "Irvine", "University of California, Irvine", "UCI", "uc irvine"):
            self.assertEqual(AFFILIATIONS.campus(name).id, "irvine", name)
        self.assertEqual(AFFILIATIONS.campus("UCSB").id, "santa_barbara")
        self.assertIsNone(AFFILIATIONS.campus("UC Berkeley"))
        lookup = AFFILIATIONS.lookup("UC Berkeley", "Computer Science")
        self.assertEqual((lookup.status, lookup.reason), ("unresolved", UNVERIFIED))

    def test_major_and_degree_spellings(self):
        for major, degree in [("Computer Science", None), ("Computer Science, B.S.", None), ("Computer Science B.S.", None),
                              ("computer science (BS)", None), ("Computer Science, Bachelor of Science", None),
                              ("Computer Science", "B.S."), ("COMPUTER SCIENCE", "BS")]:
            a = AFFILIATIONS.resolve("UC Irvine", major, degree)
            self.assertEqual((a.major, a.degree, a.school.id), ("Computer Science", "B.S.", "donald_bren_ics"), major)
        # Offered, but not with that degree: never placed anyway
        lookup = AFFILIATIONS.lookup("UC Irvine", "Computer Science", "B.A.")
        self.assertEqual((lookup.status, lookup.reason), ("unresolved", "UC Irvine lists Computer Science only as B.S., not B.A."))
        # UC Davis prints "Bachelor of Arts" (A.B.)
        self.assertEqual(AFFILIATIONS.resolve("UC Davis", "Economics, A.B.").degree, "B.A.")

    def test_the_degree_decides_between_organizations(self):
        lookup = AFFILIATIONS.lookup("UC Irvine", "Psychology")
        self.assertEqual(lookup.status, "ambiguous")
        self.assertEqual({a.school.name for a in lookup.candidates}, {"School of Social Sciences", "School of Social Ecology"})
        self.assertIsNone(AFFILIATIONS.resolve("UC Irvine", "Psychology"))
        self.assertEqual(AFFILIATIONS.resolve("UC Irvine", "Psychology", "B.A.").school.name, "School of Social Ecology")
        self.assertEqual(AFFILIATIONS.resolve("UC Irvine", "Psychology, B.S.").school.name, "School of Social Sciences")

    def test_names_offered_by_several_organizations_are_ambiguous(self):
        for major in ("Mathematics", "Physics", "Art"):
            lookup = AFFILIATIONS.lookup("UC Santa Barbara", major)
            self.assertEqual(lookup.status, "ambiguous", major)
            self.assertEqual({a.college.id for a in lookup.candidates}, {"letters_science", "creative_studies"})
            self.assertIn("College of Creative Studies", lookup.reason)
        self.assertEqual({a.college.id for a in AFFILIATIONS.lookup("UC Riverside", "Data Science").candidates},
                         {"bourns_engineering", "natural_agricultural_sciences"})

    def test_unknown_and_unresolved_majors(self):
        lookup = AFFILIATIONS.lookup("UC Irvine", "Underwater Basket Weaving")
        self.assertEqual((lookup.status, lookup.affiliation, lookup.reason), ("unresolved", None, UNVERIFIED))
        # Run jointly by two schools: listed as unresolved, with the catalogue's words and page
        for major, words in [("Computer Science and Engineering", "administered by faculty from two academic units"),
                             ("Business Information Management", "jointly offered by")]:
            lookup = AFFILIATIONS.lookup("UC Irvine", major)
            self.assertEqual(lookup.status, "unresolved", major)
            self.assertIn(words, lookup.reason)
            self.assertTrue(any("/interdisciplinarystudies/" in u for u in lookup.source_urls), major)
        # Not majors at UC Merced (Computer Science and Engineering is; Computer Engineering is an emphasis)
        for major in ("Computer Science", "Computer Engineering"):
            self.assertEqual(AFFILIATIONS.lookup("UC Merced", major).status, "unresolved", major)

    def test_majors_in(self):
        self.assertEqual(set(AFFILIATIONS.majors_in("UC Irvine", BREN)),
                         {"Computer Science", "Data Science", "Game Design and Interactive Media", "Informatics",
                          "Information and Computer Science", "Software Engineering"})
        self.assertEqual(AFFILIATIONS.majors_in("irvine", "donald_bren_ics"), AFFILIATIONS.majors_in("UCI", BREN))
        self.assertIn("Computer Science", AFFILIATIONS.majors_in("UCSB", "College of Engineering"))   # by alias
        self.assertEqual(AFFILIATIONS.majors_in("UC Irvine", "No Such School"), ())

    def test_a_school_within_a_college(self):
        registry = fixture(
            {"id": "letters", "type": "college", "name": "College of Letters", "sources": ["test"], "majors": []},
            {"id": "poetry", "type": "school", "name": "School of Poetry", "parent_id": "letters", "sources": ["test"],
             "majors": [{"name": "Poetry", "degrees": ["B.A."], "aliases": ["Poetics"]}]})
        a = registry.resolve("irvine", "Poetics")                            # a major alias
        self.assertEqual((a.major, a.school.id, a.college.id), ("Poetry", "poetry", "letters"))
        self.assertEqual(a.units, ("School of Poetry", "College of Letters"))
        self.assertEqual(registry.majors_in("irvine", "College of Letters"), ("Poetry",))


class DataQualityTests(unittest.TestCase):
    def test_campuses_are_tag_campuses(self):
        self.assertEqual(set(AFFILIATIONS.campus_ids), {"davis", "irvine", "merced", "riverside", "santa_barbara"})
        self.assertLessEqual(set(AFFILIATIONS.campus_ids), {c.id for c in TAG.campuses})

    def test_every_mapping_is_traceable(self):
        data = raw()
        for sid, s in data["sources"].items():
            self.assertTrue(s.get("url") or s.get("document"), sid)
            self.assertTrue(s.get("retrieved_on"), sid)
        for cid in AFFILIATIONS.campus_ids:
            for org in AFFILIATIONS.organizations(cid):
                self.assertTrue(org.sources, (cid, org.id))
            for major in AFFILIATIONS.majors(cid):
                lookup = AFFILIATIONS.lookup(cid, major)
                self.assertTrue(lookup.source_urls, (cid, major, lookup.status))

    def test_unit_names_match_the_tag_matrix(self):
        """Every school/college a TAG rule names is one of the campus's organizations (by name or alias), except
        UC Riverside units whose majors aren't in the data yet."""
        unmatched = set()
        for cid in AFFILIATIONS.campus_ids:
            c = find_campus(TAG, cid)
            names = [r.name for r in c.exclusions.rules if r.scope_type in UNIT_EXCLUSION_SCOPES]
            names += [r.scope_name for r in c.gpa.rules if r.scope_type not in CAMPUS_WIDE_SCOPES]
            for name in names:
                relations = {unit_relation(name, n) for o in AFFILIATIONS.organizations(cid) for n in o.names}
                if "same" not in relations:
                    unmatched.add((cid, name))
        self.assertEqual(unmatched, {("riverside", "School of Education"), ("riverside", "School of Public Policy")})

    def test_listed_majors_get_definite_answers_from_unit_rules(self):
        term = EntryTerm.parse("fall_2027")
        for cid in ("irvine", "santa_barbara"):                          # the campuses that exclude whole units
            c = find_campus(TAG, cid)
            for major in AFFILIATIONS.majors(cid):
                lookup = AFFILIATIONS.lookup(cid, major)
                if lookup.status != "resolved":
                    continue
                profile = MajorProfile.from_student(TagStudentProfile(intended_major=major), lookup)
                results = {m.result for m in match_exclusions(c, term, profile) if m.rule.scope_type in UNIT_EXCLUSION_SCOPES}
                self.assertNotIn("possible", results, (cid, major))

    def test_invalid_data_is_rejected_with_its_location(self):
        org = {"id": "ics", "type": "school", "name": BREN, "sources": ["test"], "majors": [{"name": "Informatics"}]}
        with self.assertRaisesRegex(TagDatasetError, r"campuses\[0\]\(irvine\)\.organizations\[0\]\.type"):
            fixture({**org, "type": "department"})
        with self.assertRaisesRegex(TagDatasetError, "unknown source ids"):
            fixture({**org, "sources": ["nowhere"]})
        with self.assertRaisesRegex(TagDatasetError, "has no source"):
            fixture({**org, "sources": []})
        with self.assertRaisesRegex(TagDatasetError, "unknown or loops"):
            fixture({**org, "parent_id": "ics"})
        with self.assertRaisesRegex(TagDatasetError, "schema_version"):
            normalize_affiliations({"schema_version": "2.0.0", "campuses": []})

    def test_a_broken_file_gives_an_empty_registry(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "uc_major_affiliations.json"
            broken.write_text("{ not json", encoding="utf-8")
            with self.assertLogs("uvicorn.error", level="ERROR"):
                empty = load_affiliations(broken)
        self.assertEqual(empty.campus_ids, ())
        self.assertEqual(empty.lookup("UC Irvine", "Computer Science").status, "unresolved")


if __name__ == "__main__":
    unittest.main()
