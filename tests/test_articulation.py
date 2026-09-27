"""Articulation data layer (articulation/): institution ids, loading, lookup and the API that uses it.
Run with: python -m unittest discover -s tests -v

The fixture agreements use real institution ids but made-up courses (FIX 1, UNIV 101, ...): they test the
loader, and are not articulation data."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import main
from articulation import ARTICULATION_DIR, InstitutionRegistry, PathwayStore, academic_year_of
from sep_engine import mock_data

CSUEB = "California State University, East Bay"


def dataset(**changes) -> dict:
    """A well-formed synthetic Chabot -> CSUEB Computer Science, B.S. agreement (2026-2027)."""
    ds = {
        "schema_version": "1.0.0",
        "dataset_type": "transfer_articulation",
        "dataset_id": "fixture_chabot_csueb_cs_2026_2027",
        "source": {"publisher": "Test fixture", "url": "https://example.edu/fixture", "retrieved_on": "2026-09-26"},
        "agreement": {"from_institution_id": "chabot", "to_institution_id": "csueb", "major": "Computer Science",
                      "degree": "B.S.", "academic_year": "2026-2027"},
        "institutions": [{"id": "chabot", "name": "Chabot College"}, {"id": "csueb", "name": CSUEB}],
        "courses": [
            {"code": "FIX 1", "title": "Fixture One", "units": 4},
            {"code": "FIX 2", "title": "Fixture Two", "units": 4, "prereqs": ["FIX 1"]},
            {"code": "FIX 3", "title": "Fixture Three", "units": 3},
            {"code": "FIX 4", "title": "Fixture Four", "units": 3},
            {"code": "FIX 5", "title": "Fixture Five", "units": 5},
        ],
        "requirements": [
            {"id": "r1", "receiving": [{"code": "UNIV 101", "title": "Target One"}],
             "articulation": {"type": "course", "course": "FIX 1"}},
            {"id": "r2", "receiving": [{"code": "UNIV 102"}], "articulation": {"type": "all_of", "courses": ["FIX 2", "FIX 5"]}},
            {"id": "r3", "receiving": [{"code": "UNIV 103"}],
             "articulation": {"type": "one_of", "options": [["FIX 3"], ["FIX 4"]]}},
            {"id": "r4", "receiving": [{"code": "UNIV 104", "title": "Target Four"}],
             "articulation": {"type": "none", "reason": "No course articulated"}},
        ],
    }
    ds = copy.deepcopy(ds)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(ds.get(key), dict):
            ds[key] = {**ds[key], **value}
        else:
            ds[key] = value
    return ds


def with_articulation(ds: dict, rid: str, articulation: dict) -> dict:
    ds = copy.deepcopy(ds)
    next(r for r in ds["requirements"] if r["id"] == rid)["articulation"] = articulation
    return ds


class Folder:
    """A temporary articulation folder; write files, then load a store from it."""

    def __init__(self, test: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)

    def write(self, name: str, content: dict | str) -> None:
        text = content if isinstance(content, str) else json.dumps(content)
        (self.path / name).write_text(text, encoding="utf-8")

    def store(self) -> PathwayStore:
        store = PathwayStore()
        with mock.patch("articulation.store.log"):              # expected problems: keep test output quiet
            store.load_directory(self.path)
        return store


# =====================================================================
# Institution ids
# =====================================================================
class InstitutionTests(unittest.TestCase):
    def setUp(self):
        self.registry = InstitutionRegistry()
        self.registry.register("csueb", CSUEB)
        self.registry.register("berkeley", "University of California, Berkeley")
        self.registry.register("chabot", "Chabot College")

    def test_cal_state_east_bay_spellings(self):
        for name in ("Cal State East Bay", CSUEB, "CSUEB", "csueb", "CSU East Bay", "california state university east bay"):
            self.assertEqual(self.registry.resolve(name), "csueb", name)

    def test_uc_berkeley_spellings(self):
        for name in ("UC Berkeley", "University of California, Berkeley", "UCB", "berkeley"):
            self.assertEqual(self.registry.resolve(name), "berkeley", name)

    def test_college_spellings(self):
        for name in ("Chabot College", "Chabot", "chabot"):
            self.assertEqual(self.registry.resolve(name), "chabot", name)

    def test_unknown_names_only_match_themselves(self):
        self.assertFalse(self.registry.known("Cal State Fullerton"))
        self.assertNotEqual(self.registry.resolve("Cal State Fullerton"), "csueb")
        self.assertTrue(self.registry.same("Some College", "some college"))
        self.assertFalse(self.registry.same("Some College", "Chabot College"))

    def test_listed_aliases_and_ambiguous_spellings(self):
        self.registry.register("sjsu", "San José State University", ["San Jose State"])
        self.assertEqual(self.registry.resolve("San Jose State University"), "sjsu")    # accents ignored
        self.assertEqual(self.registry.resolve("SJSU"), "sjsu")                          # initials
        self.registry.register("a", "Alpha Beta College")
        self.registry.register("b", "Alpha Beta Community College")
        self.assertFalse(self.registry.known("Alpha Beta"))                             # shared by two: never used
        self.assertEqual(self.registry.resolve("Alpha Beta College"), "a")


# =====================================================================
# Loading: available, unsupported, skipped
# =====================================================================
class LoaderTests(unittest.TestCase):
    def test_a_supported_agreement_becomes_a_pathway(self):
        folder = Folder(self)
        folder.write("fixture.json", dataset())
        store = folder.store()
        self.assertEqual((store.unsupported, store.skipped), ([], []))
        [p] = store.pathways
        self.assertEqual((p.source, p.college, p.university, p.major, p.degree, p.academic_year),
                         ("assist", "Chabot College", CSUEB, "Computer Science", "B.S.", "2026-2027"))
        ag = p.agreement
        self.assertEqual(ag.required_courses, ["FIX 1", "FIX 2", "FIX 5"])          # course + all_of
        [group] = ag.requirement_groups                                                # one_of -> choose one
        self.assertEqual((group.category.value, group.choose, group.options), ("major", 1, ["FIX 3", "FIX 4"]))
        self.assertIn("CSUEB UNIV 103", group.name)
        self.assertEqual(ag.degree, "Computer Science, B.S.")
        self.assertEqual(p.catalog["FIX 2"].prereqs, ["FIX 1"])
        self.assertEqual(p.catalog["FIX 5"].units, 5)
        self.assertEqual(p.not_articulated, ["CSUEB UNIV 104 · Target Four (No course articulated)"])
        self.assertTrue(p.prerequisites_enforced)
        self.assertIsNone(p.ge_pattern)                                                # articulation has no GE

    def test_requirement_groups_of_single_courses(self):
        ds = dataset(requirement_groups=[{"id": "g", "title": "Complete 2 of these", "choose": 2,
                                          "requirement_ids": ["r1", "r2b", "r2c"]}])
        ds["requirements"] += [
            {"id": "r2b", "receiving": [{"code": "UNIV 201"}], "articulation": {"type": "course", "course": "FIX 3"}},
            {"id": "r2c", "receiving": [{"code": "UNIV 202"}], "articulation": {"type": "course", "course": "FIX 4"}}]
        ds = with_articulation(ds, "r3", {"type": "course", "course": "FIX 5"})
        folder = Folder(self)
        folder.write("groups.json", ds)
        [p] = folder.store().pathways
        [group] = p.agreement.requirement_groups
        self.assertEqual((group.name, group.choose, group.options), ("Complete 2 of these", 2, ["FIX 1", "FIX 3", "FIX 4"]))
        self.assertNotIn("FIX 1", p.agreement.required_courses)                     # grouped, so not required

    def test_structures_the_planner_cannot_express_are_unsupported(self):
        base = dataset()
        cases = {
            "bundles.json": (with_articulation(base, "r3", {"type": "one_of", "options": [["FIX 3", "FIX 4"], ["FIX 5"]]}),
                             "different course bundles"),
            "other.json": (with_articulation(base, "r1", {"type": "other", "description": "Minimum 8 units from a list"}),
                           "Minimum 8 units from a list"),
            "no_units.json": (dataset(courses=[{**c, "units": None} if c["code"] == "FIX 1" else c
                                               for c in base["courses"]]), "no units are stated for FIX 1"),
            "no_record.json": (with_articulation(base, "r1", {"type": "course", "course": "FIX 9"}),
                               "FIX 9 has no course record"),
            "unknown_prereq.json": (dataset(courses=[*base["courses"][:4],
                                                     {"code": "FIX 5", "title": "Five", "units": 5, "prereqs": ["FIX 8"]}]),
                                    "FIX 8 (a prerequisite of FIX 5) has no course record"),
            "cycle.json": (dataset(courses=[{"code": "FIX 1", "title": "One", "units": 4, "prereqs": ["FIX 2"]},
                                            *base["courses"][1:]]), "Prerequisite cycle"),
            "group.json": (dataset(requirement_groups=[{"id": "g", "title": "Complete 1", "choose": 1,
                                                        "requirement_ids": ["r1", "r2"]}]), "isn't met by a single course"),
            "nothing.json": (dataset(requirements=[base["requirements"][3]]), "articulates no Chabot College course"),
        }
        folder = Folder(self)
        for i, (name, (ds, _)) in enumerate(cases.items()):
            folder.write(name, {**ds, "dataset_id": f"case_{i}"})
        store = folder.store()
        self.assertEqual((store.pathways, store.skipped), ([], []))
        reasons = {u.file: u.reason for u in store.unsupported}
        for name, (_, expected) in cases.items():
            self.assertIn(expected, reasons[name], name)

    def test_unreadable_files_are_skipped_with_the_reason(self):
        folder = Folder(self)
        folder.write("broken.json", "{ not json")
        folder.write("other_data.json", {"dataset_type": "uc_tag_requirements", "campuses": []})
        folder.write("bad_schema.json", dataset(agreement={"to_institution_id": "nowhere"}))
        folder.write("bad_year.json", dataset(dataset_id="x", agreement={"academic_year": "2026-2028"}))
        folder.write("fixture.json", dataset())
        folder.write("fixture_copy.json", dataset())
        (folder.path / "notes.md").write_text("not JSON, not loaded", encoding="utf-8")
        store = folder.store()
        reasons = {s.file: s.reason for s in store.skipped}
        self.assertIn("not readable JSON", reasons["broken.json"])
        self.assertIn("not a transfer_articulation dataset", reasons["other_data.json"])
        self.assertIn("to_institution_id 'nowhere' isn't in institutions", reasons["bad_schema.json"])
        self.assertIn("agreement.academic_year", reasons["bad_year.json"])
        self.assertIn("same dataset_id as fixture.json", reasons["fixture_copy.json"])
        self.assertEqual(len(store.pathways), 1)
        self.assertNotIn("notes.md", reasons)

    def test_the_repository_data_folder_loads_cleanly(self):
        store = PathwayStore()
        store.load_directory(ARTICULATION_DIR)
        self.assertEqual(store.skipped, [], [s.reason for s in store.skipped])
        self.assertEqual(store.unsupported, [], [u.message for u in store.unsupported])


# =====================================================================
# Lookup: institutions, major/degree, academic year
# =====================================================================
class LookupTests(unittest.TestCase):
    def store(self, *datasets) -> PathwayStore:
        folder = Folder(self)
        for i, ds in enumerate(datasets or (dataset(),)):
            folder.write(f"f{i}.json", ds)
        return folder.store()

    def test_exact_campus_major_and_year(self):
        store = self.store()
        for university in ("Cal State East Bay", CSUEB, "CSUEB"):
            for major in ("Computer Science", "Computer Science, B.S.", "Computer Science B.S."):
                m = store.find("Chabot College", university, major, "CAL-GETC", "Fall 2026")
                self.assertIsNotNone(m, (university, major))
                self.assertEqual((m.pathway.academic_year, m.academic_year), ("2026-2027", "2026-2027"))
                self.assertFalse([w for w in m.warnings if "agreement is used" in w])
        m = store.find("Chabot", "Cal State East Bay", "computer science", "7-Course Pattern", "Spring 2027")
        self.assertEqual(m.pathway.agreement_for("7-Course Pattern").ge_pattern, "7-Course Pattern")
        self.assertFalse(any("GE" in w for w in m.warnings))      # GE is the planner's job, not the store's
        self.assertTrue(any("CSUEB UNIV 104" in w for w in m.warnings))              # not articulated

    def test_degree_is_matched_separately(self):
        store = self.store(dataset(), dataset(dataset_id="ba", agreement={"degree": "B.A."}))
        self.assertEqual(store.find("Chabot College", "CSUEB", "Computer Science, B.A.", "CAL-GETC", "Fall 2026")
                         .pathway.degree, "B.A.")
        m = store.find("Chabot College", "CSUEB", "Computer Science", "CAL-GETC", "Fall 2026")
        self.assertEqual(m.pathway.degree, "B.A.")                                    # deterministic choice ...
        self.assertTrue(any("also available: B.S." in w for w in m.warnings))         # ... and the other is named
        only_bs = self.store()
        self.assertIsNone(only_bs.find("Chabot College", "CSUEB", "Computer Science, B.A.", "CAL-GETC", "Fall 2026"))

    def test_unknown_pathways_are_not_found(self):
        store = self.store()
        for college, university, major in [("Chabot College", "UC Berkeley", "Computer Science"),
                                           ("Chabot College", "Cal State East Bay", "Mathematics"),
                                           ("Laney College", "Cal State East Bay", "Computer Science"),
                                           ("Chabot College", "Cal State Fullerton", "Computer Science")]:
            lookup = store.lookup(college, university, major, "CAL-GETC", "Fall 2026")
            self.assertEqual((lookup.match, lookup.unsupported, lookup.reason), (None, None, None), (university, major))

    def test_unsupported_pathways_are_reported_by_lookup(self):
        store = self.store(with_articulation(dataset(), "r1", {"type": "other", "description": "Series of 3 courses"}))
        lookup = store.lookup("Chabot College", "Cal State East Bay", "Computer Science", "CAL-GETC", "Fall 2026")
        self.assertIsNone(lookup.match)
        self.assertIn("Series of 3 courses", lookup.unsupported.message)

    def test_academic_years(self):
        self.assertEqual([academic_year_of(t) for t in ("Fall 2026", "Spring 2027", "Summer 2027", "Winter 2027")],
                         ["2026-2027", "2026-2027", "2026-2027", None])
        store = self.store(dataset(), dataset(dataset_id="older", agreement={"academic_year": "2025-2026"}))
        pick = lambda term: store.find("Chabot College", "CSUEB", "Computer Science", "CAL-GETC", term)  # noqa: E731
        self.assertEqual(pick("Fall 2026").pathway.academic_year, "2026-2027")
        self.assertEqual(pick("Spring 2026").pathway.academic_year, "2025-2026")
        later = pick("Fall 2027")                                   # 2027-2028 isn't loaded: nearest earlier + warning
        self.assertEqual(later.pathway.academic_year, "2026-2027")
        self.assertTrue(any("No 2027-2028 articulation agreement" in w for w in later.warnings))
        lookup = store.lookup("Chabot College", "CSUEB", "Computer Science", "CAL-GETC", "Fall 2024")
        self.assertIsNone(lookup.match)                             # only later years: never used silently
        self.assertIn("none covers 2024-2025", lookup.reason)

    def test_loaded_data_wins_over_mock_data(self):
        berkeley = dataset(dataset_id="b", agreement={"to_institution_id": "berkeley", "degree": "B.A."},
                           institutions=[{"id": "chabot", "name": "Chabot College"},
                                         {"id": "berkeley", "name": "University of California, Berkeley"}])
        store = self.store(berkeley)
        store.add_mock(mock_data.MOCK_CATALOG, mock_data.MOCK_AGREEMENTS)
        self.assertEqual(store.find("Chabot College", "UC Berkeley", "Computer Science", "CAL-GETC", "Fall 2026")
                         .pathway.source, "assist")
        store.remove_mock()
        self.assertEqual([p.source for p in store.pathways], ["assist"])


# =====================================================================
# API: real data without mock injection, diagnostics, reload
# =====================================================================
class ArticulationAPITests(unittest.TestCase):
    BODY = {"college": "Chabot College", "university": "Cal State East Bay", "major": "Computer Science",
            "ge_pathway": "CAL-GETC", "completed_courses": [], "start_term": "Fall 2026", "include_summer": False}

    def setUp(self):
        self.folder = Folder(self)
        self.folder.write("fixture.json", dataset())
        patcher = mock.patch.object(main, "STORE", self.folder.store())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def test_loaded_pathway_plans_without_mock_injection(self):
        self.assertFalse(self.client.get("/dev/mock-data").json()["loaded"])
        r = self.client.post("/generate-sep", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["pathway"]["university"], body["pathway"]["degree"], body["pathway"]["ge_pathway"]),
                         (CSUEB, "Computer Science, B.S.", "CAL-GETC"))
        art = body["articulation"]
        self.assertEqual((art["source"], art["file"], art["academic_year"], art["url"]),
                         ("assist", "fixture.json", "2026-2027", "https://example.edu/fixture"))
        [plan] = body["plans"]
        term_of = {c["code"]: s["index"] for s in plan["semesters"] for c in s["courses"]}
        self.assertLess(term_of["FIX 1"], term_of["FIX 2"])                          # prerequisite respected
        self.assertEqual(sum(c in term_of for c in ("FIX 3", "FIX 4")), 1)            # exactly one of the choice
        courses = [c for s in plan["semesters"] for c in s["courses"]]
        self.assertEqual({c["type"] for c in courses if c["code"].startswith("FIX")}, {"major_prep"})
        ge = [c for c in courses if c["is_ge"]]                     # GE now comes from Chabot's real Cal-GETC list
        self.assertTrue(ge and all(c["type"] == "ge_slot" and c["ge"]["institution_id"] == "chabot" for c in ge))
        self.assertEqual(plan["ge"]["pathway"], "cal_getc")
        self.assertEqual(sorted(self.client.get("/colleges/Chabot College/catalog").json()),
                         ["FIX 1", "FIX 2", "FIX 3", "FIX 4", "FIX 5"])

    def test_pathways_diagnostics_and_reload(self):
        listing = self.client.get("/pathways").json()
        [row] = listing["available"]
        self.assertEqual((row["college_id"], row["university_id"], row["source"]), ("chabot", "csueb", "assist"))
        self.assertEqual((listing["unsupported"], listing["skipped"]), ([], []))
        self.folder.write("broken.json", "{")
        self.folder.write("unsupported.json", with_articulation(dataset(dataset_id="u", agreement={"major": "Mathematics"}),
                                                                "r1", {"type": "other", "description": "Placement"}))
        with mock.patch("articulation.store.log"):
            listing = self.client.post("/pathways/reload").json()
        self.assertEqual([s["file"] for s in listing["skipped"]], ["broken.json"])
        self.assertEqual([u["major"] for u in listing["unsupported"]], ["Mathematics"])
        r = self.client.post("/generate-sep", json={**self.BODY, "major": "Mathematics"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("can't use it: CSUEB UNIV 101 · Target One uses a structure the planner can't read: Placement",
                      r.json()["detail"])

    def test_mock_data_stays_separate(self):
        self.client.post("/dev/mock-data")
        mock_body = {**self.BODY, "university": "UC Berkeley", "completed_courses": ["CS 1"]}
        r = self.client.post("/generate-sep", json=mock_body)
        self.assertEqual((r.status_code, r.json()["articulation"]["source"]), (200, "mock"))
        self.assertTrue(any(c["is_ge"] for s in r.json()["plans"][0]["semesters"] for c in s["courses"]))
        self.client.delete("/dev/mock-data")
        self.assertEqual(self.client.post("/generate-sep", json=mock_body).status_code, 404)
        self.assertEqual(self.client.post("/generate-sep", json=self.BODY).status_code, 200)   # real data stays


if __name__ == "__main__":
    unittest.main()
