"""ASSIST-extracted agreements (articulation/assist.py): requirement trees -> valid alternatives, never flattened.
Run with: python -m unittest discover -s tests -v

The fixture uses real institution ids but made-up courses (FIX 1, UNIV 101, ...)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from articulation import ARTICULATION_DIR, PathwayStore
from articulation.assist import is_assist_dataset, parse_assist


def C(cid):
    return {"operator": "COURSE", "course_id": f"chabot:{cid}"}


def OR(*ops):
    return {"operator": "OR", "operands": list(ops)}


def AND(*ops):
    return {"operator": "AND", "operands": list(ops)}


def req(rid, source, units=4.0, status="articulated"):
    return {"id": rid, "target_course_id": f"csueb:{rid}", "articulation_status": status,
            "source_requirement": source, "source_note": None if source else "No Course Articulated"}


def dataset(requirements, groups, courses=("FIX1", "FIX2", "FIX3", "FIX4", "FIX5"), **agreement):
    ds = {
        "schema_version": "1.2.0", "dataset_id": "fixture_assist",
        "source": {"publisher": "Test fixture", "document_title": "Fixture agreement"},
        "agreement": {"academic_year": "2026-2027", "from_institution_id": "chabot", "to_institution_id": "csueb",
                      "major": "Fixture Studies", "degree": "B.S.", **agreement},
        "institutions": [{"id": "chabot", "name": "Chabot College"},
                         {"id": "csueb", "name": "California State University, East Bay"}],
        "courses": [{"id": f"chabot:{c}", "institution_id": "chabot", "code": f"FIX {c[3:]}", "title": c, "units": 4.0,
                     "cross_listed_codes": ["ALT 9"] if c == "FIX1" else []} for c in courses]
                   + [{"id": f"csueb:{r['id']}", "institution_id": "csueb", "code": f"UNIV {r['id']}", "title": r["id"],
                       "units": 3.0} for r in requirements],
        "requirements": requirements, "requirement_groups": groups,
    }
    return copy.deepcopy(ds)


def keys(ag, alt):
    return sorted(ag.code(k) for k in alt.required)


class AssistTests(unittest.TestCase):
    def test_single_course_or_stays_one_choice(self):
        ds = dataset([req("R1", OR(C("FIX1"), C("FIX2"))), req("R2", C("FIX3"))],
                     [{"id": "core", "operator": "AND", "requirement_ids": ["R1", "R2"]}])
        self.assertTrue(is_assist_dataset(ds))
        ag = parse_assist(ds, "f.json")
        [alt] = ag.alternatives
        self.assertEqual(keys(ag, alt), ["FIX 3"])                     # the OR is NOT turned into required courses
        self.assertEqual([(r, [ag.code(o) for o in opts]) for r, opts in alt.choices], [("R1", ["FIX 1", "FIX 2"])])

    def test_or_of_bundles_becomes_alternatives(self):
        ds = dataset([req("R1", OR(AND(C("FIX1"), C("FIX2")), AND(C("FIX3"), C("FIX4"))))],
                     [{"id": "core", "operator": "AND", "requirement_ids": ["R1"]}])
        ag = parse_assist(ds, "f.json")
        self.assertEqual([keys(ag, a) for a in ag.alternatives], [["FIX 1", "FIX 2"], ["FIX 3", "FIX 4"]])

    def test_or_of_groups(self):
        ds = dataset([req("R1", C("FIX1")), req("R2", C("FIX2")), req("R3", C("FIX3"))],
                     [{"id": "a", "operator": "AND", "requirement_ids": ["R1", "R2"]},
                      {"id": "b", "operator": "AND", "requirement_ids": ["R3"]},
                      {"id": "pick", "operator": "OR", "group_ids": ["a", "b"], "source_instruction": "Select A or B"}],
                     root_requirement_group_id="pick")
        ag = parse_assist(ds, "f.json")
        self.assertEqual([keys(ag, a) for a in ag.alternatives], [["FIX 1", "FIX 2"], ["FIX 3"]])
        self.assertTrue(ag.alternatives[0].path[0].startswith("Select A or B"))

    def test_minimum_selects_enough_entries(self):
        ds = dataset([req(f"R{i}", C(f"FIX{i}")) for i in range(1, 5)],
                     [{"id": "el", "operator": "MINIMUM", "requirement_ids": ["R1", "R2", "R3", "R4"],
                       "selection_constraints": {"minimum_units": 6.0, "minimum_items": 2}}])
        ag = parse_assist(ds, "f.json")
        self.assertEqual(len(ag.alternatives), 6)                        # every minimal pair (3 + 3 units >= 6)
        self.assertTrue(all(len(a.required) == 2 for a in ag.alternatives))

    def test_no_course_articulated_is_never_required(self):
        ds = dataset([req("R1", C("FIX1")), req("R2", None, status="no_course_articulated")],
                     [{"id": "core", "operator": "AND", "requirement_ids": ["R1", "R2"]}])
        ag = parse_assist(ds, "f.json")
        [alt] = ag.alternatives
        self.assertEqual((keys(ag, alt), alt.remaining), (["FIX 1"], ("R2",)))

    def test_cross_listed_codes(self):
        ag = parse_assist(dataset([req("R1", C("FIX1"))], [{"id": "c", "operator": "AND", "requirement_ids": ["R1"]}]),
                          "f.json")
        self.assertEqual(ag.cross_listed(), {"ALT9": "FIX1"})

    def test_store_reports_unsupported_structures(self):
        bad = dataset([req("R1", C("FIX1"))], [{"id": "c", "operator": "XOR", "requirement_ids": ["R1"]}])
        missing = dataset([req("R1", C("NOPE7"))], [{"id": "c", "operator": "AND", "requirement_ids": ["R1"]}],
                          dataset_id="x")
        missing["dataset_id"] = "missing"
        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.json").write_text(json.dumps(bad), encoding="utf-8")
            Path(d, "missing.json").write_text(json.dumps(missing), encoding="utf-8")
            store = PathwayStore()
            with mock.patch("articulation.store.log"):
                store.load_directory(d)
        reasons = {u.file: u.reason for u in store.unsupported}
        self.assertIn("operator 'XOR'", reasons["bad.json"])
        self.assertIn("no course record", reasons["missing.json"])
        self.assertEqual(store.pathways, [])

    def test_real_agreements_load(self):
        store = PathwayStore()
        store.load_directory(ARTICULATION_DIR)
        self.assertEqual((store.skipped, store.unsupported), ([], []))
        by_major = {p.major: p for p in store.pathways}
        self.assertEqual(sorted(by_major), ["Biochemistry", "Computer Engineering", "Computer Science", "Physics"])
        cs = by_major["Computer Science"].assist
        [alt] = cs.alternatives
        self.assertEqual(dict(alt.choices), {"requirement:CS101": ("CSCI15", "CSCI19A"),
                                             "requirement:PHYS135": ("PHYS4A", "PHYS7A")})
        self.assertEqual(alt.remaining, ("requirement:CS230", "requirement:MATH225"))
        self.assertEqual(len(by_major["Biochemistry"].assist.alternatives), 2)     # concentration option A or B
        self.assertEqual(len(by_major["Physics"].assist.alternatives), 165)        # sequence x MINIMUM electives


if __name__ == "__main__":
    unittest.main()
