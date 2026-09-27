"""API tests for main.py. Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import tempfile
import unittest
import warnings
from unittest import mock

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import main
from articulation import PathwayStore
from tag_engine import registry_from

BODY = {"college": "Chabot College", "university": "UC Berkeley", "major": "Computer Science",
        "ge_pathway": "CAL-GETC", "completed_courses": ["CS 1", "ENGL 1A"],
        "start_term": "Fall 2026", "include_summer": False}


def empty_store() -> PathwayStore:
    """A store loaded from an empty folder: the tests don't depend on the real data/articulation/ files."""
    store = PathwayStore()
    with tempfile.TemporaryDirectory() as d:
        store.load_directory(d)
    return store


class APITests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(main, "STORE", empty_store())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def post(self, **overrides):
        return self.client.post("/generate-sep", json={**BODY, **overrides})

    def test_zero_data_by_default(self):
        r = self.post()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], main.NOT_UPLOADED)
        self.assertEqual(self.client.get("/colleges/Chabot College/catalog").status_code, 404)
        self.assertFalse(self.client.get("/dev/mock-data").json()["loaded"])

    def test_inject_then_generate(self):
        self.assertTrue(self.client.post("/dev/mock-data").json()["loaded"])
        for ge in ("CAL-GETC", "7-Course Pattern"):
            r = self.post(ge_pathway=ge)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            # the demo pathway is a normal workload (53 units ≈ 13.2/semester) -> one balanced plan
            self.assertEqual(body["workload"]["classification"], "normal")
            self.assertEqual([p["label"] for p in body["plans"]], ["Balanced Plan"])
        self.assertEqual(self.post(major="Computer Engineering").status_code, 404)
        self.client.delete("/dev/mock-data")
        self.assertEqual(self.post().status_code, 404)

    def test_real_terms_start_term_and_spring_end(self):
        self.client.post("/dev/mock-data")
        for start, summer in [("Fall 2026", False), ("Spring 2027", False), ("Spring 2027", True), ("Summer 2027", True)]:
            data = self.post(start_term=start, include_summer=summer).json()
            self.assertEqual(data["pathway"]["start_term"], start)
            for plan in data["plans"]:
                terms = [s["term"] for s in plan["semesters"]]
                self.assertEqual(terms[0], start)
                self.assertTrue(terms[-1].startswith("Spring"), terms)
                self.assertEqual(any(t.startswith("Summer") for t in terms), summer and len(terms) > 1 or start.startswith("Summer"))
                for s in plan["semesters"]:
                    if s["season"] == "Summer":
                        self.assertLessEqual(s["units"], 9)
                        self.assertTrue(all(c["is_ge"] for c in s["courses"]))

    def test_tag_campuses_come_from_the_dataset(self):
        body = self.client.get("/tag/campuses").json()
        self.assertEqual([c["id"] for c in body["campuses"]],
                         ["davis", "irvine", "merced", "riverside", "santa_barbara", "santa_cruz"])
        self.assertEqual([d["id"] for d in body["datasets"]], ["uc_tag_matrix_2027_2028"])
        merced = next(c for c in body["campuses"] if c["id"] == "merced")
        self.assertEqual([t["label"] for t in merced["entry_terms"]], ["Fall 2027", "Spring 2028"])

    def test_tag_runs_only_when_a_campus_is_chosen(self):
        self.client.post("/dev/mock-data")
        self.assertIsNone(self.post().json()["tag_evaluations"])
        self.assertIsNone(self.post(tag_university="None").json()["tag_evaluations"])

    def test_tag_is_evaluated_for_each_plans_transfer_term(self):
        # The demo plan (Fall 2026 start) transfers for Fall 2028; the loaded matrix covers Fall 2027 entry
        self.client.post("/dev/mock-data")
        for choice in ("riverside", "UC Riverside"):                 # campus id or name
            [entry] = self.post(tag_university=choice).json()["tag_evaluations"]
            self.assertEqual(entry["plan_ids"], ["A"])
            tag = entry["evaluation"]
            self.assertEqual((tag["status"], tag["entry_term"]["key"], tag["rules_term"]), ("needs_review", "fall_2028", None))
            self.assertEqual(tag["reference"]["rules_term"]["key"], "fall_2027")
            self.assertEqual(tag["reference"]["minimum_gpa"]["minimum_gpa"], 3.6)
        # Target university and TAG university are independent: Berkeley can be the target, not a TAG campus
        [entry] = self.post(tag_university="UC Berkeley").json()["tag_evaluations"]
        self.assertEqual(entry["evaluation"]["status"], "not_applicable")

    def test_tag_evaluate_endpoint(self):
        r = self.client.post("/tag/evaluate", json={"campus": "riverside", "entry_term": "Fall 2027", "student": {
            "intended_major": "Computer Science", "uc_transferable_gpa": 3.5}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ineligible")
        self.assertIn("below the 3.6 minimum", r.json()["blocking_reasons"][0])
        self.assertEqual(self.client.post("/tag/evaluate", json={"campus": "davis", "entry_term": "Autumn 2027"}).status_code, 422)
        self.assertEqual(self.client.post("/tag/evaluate", json={"campus": None, "entry_term": "fall_2027"}).json()["status"],
                         "not_applicable")

    def test_tag_major_check_before_any_plan(self):
        # What the form sends as soon as a major and a TAG University are chosen (no SEP generated)
        def exclusion(campus, major):
            r = self.client.post("/tag/evaluate", json={"campus": campus, "entry_term": "fall_2027",
                                                        "student": {"intended_major": major}})
            self.assertEqual(r.status_code, 200, r.text)
            return next(c for c in r.json()["checks"] if c["id"] == "campus.major_exclusion")
        cs = exclusion("irvine", "Computer Science")
        self.assertEqual(cs["status"], "not_met")
        self.assertIn("Donald Bren School of Information and Computer Sciences, whose majors are excluded", cs["explanation"])
        self.assertIn("https://catalogue.uci.edu/undergraduatedegrees/", cs["urls"])       # where the school comes from
        self.assertEqual(exclusion("irvine", "Mathematics")["status"], "met")
        self.assertEqual(exclusion("santa_barbara", "Computer Science")["status"], "not_met")
        unknown = exclusion("irvine", "Underwater Basket Weaving")
        self.assertEqual(unknown["status"], "manual_review")
        self.assertIn("affiliation for this major could not be verified", unknown["explanation"])

    def test_tag_evaluation_reports_the_major_affiliation(self):
        r = self.client.post("/tag/evaluate", json={"campus": "UC Irvine", "entry_term": "fall_2027",
                                                    "student": {"intended_major": "Computer Science, B.S."}})
        aff = r.json()["major_affiliation"]
        self.assertEqual((aff["status"], aff["major"], aff["degree"]), ("resolved", "Computer Science", "B.S."))
        self.assertEqual(aff["school"], {"id": "donald_bren_ics", "type": "school",
                                         "name": "Donald Bren School of Information and Computer Sciences"})
        self.assertIsNone(aff["college"])
        self.assertTrue(aff["source_urls"])

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_generate_still_works_without_tag_data(self):
        self.client.post("/dev/mock-data")
        with mock.patch.object(main, "TAG_REGISTRY", registry_from([])):
            r = self.post(tag_university="UC Davis")
        self.assertEqual(r.status_code, 200, r.text)
        tag = r.json()["tag_evaluations"][0]["evaluation"]
        self.assertEqual((tag["status"], tag["headline"]), ("needs_review", "TAG data unavailable"))

    def test_ap_scores_accepted_and_validated(self):
        self.client.post("/dev/mock-data")
        r = self.post(ap_scores=[{"subject": "AP Calculus BC", "score": 5}, {"subject": "AP Computer Science A", "score": 4}])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.post(ap_scores=[{"subject": "AP Calculus BC", "score": 7}]).status_code, 422)

    def test_summer_start_requires_summer(self):
        self.client.post("/dev/mock-data")
        self.assertEqual(self.post(start_term="Summer 2027", include_summer=False).status_code, 422)
        self.assertEqual(self.post(start_term="Autumn 2026").status_code, 422)

    def test_frontend_is_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("javascript", self.client.get("/ui.js").headers["content-type"])
        html = self.client.get("/").text
        # Styles, fonts and icons are self-hosted so the page never renders unstyled offline
        for path in ("assets/tailwind.css", "assets/vendor/inter/inter.css",
                     "assets/vendor/fontawesome/css/all.min.css"):
            self.assertIn(path, html)
            self.assertEqual(self.client.get("/" + path).status_code, 200, path)
        self.assertNotIn("cytoscape", html, "The graph view was removed; results are semester cards only")
        # Frontend files must be revalidated on every load, or browsers keep showing stale layouts
        for path in ("/", "/ui.js", "/api.js", "/assets/tailwind.css"):
            self.assertEqual(self.client.get(path).headers.get("cache-control"), "no-cache", path)
        self.assertNotEqual(self.client.get("/health").headers.get("cache-control"), "no-cache")
        self.assertLess(html.index("fontawesome/css/all.min.css"), html.index("assets/tailwind.css"),
                        "Tailwind must load after Font Awesome so utilities like `hidden` win")


if __name__ == "__main__":
    unittest.main()
