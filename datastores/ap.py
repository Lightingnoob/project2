"""
AP data (data/ap/*.json), evaluated in SEPARATE layers:

  community_college_effect   the college's own AP chart (e.g. data/ap/chabot_ap_waivers.json): which college
                             course(s) an exam waives. Only these waivers can remove a course from the SEP.
                             choose_one = credit for ONE of the listed courses, never all of them.
  ge_effect                  the Cal-GETC area(s) the college chart lists for the exam ("5B and 5C", "3A or 3B").
  target_campus_effect       the target university's own AP chart (data/ap/ap_campus_score_requirements.json).
                             Every rule there is published as needing context/policy review, so it is reported,
                             never applied: a university course award does NOT complete a college course.

Unknown stays unknown: an exam missing from a chart is "not_listed" (not a denial), a campus without data is
"not_loaded".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tag_engine.selectors import norm

from .common import DATA_DIR, LoadReport, code_key, read_json

AP_DIR = DATA_DIR / "ap"
GENERAL_CONTEXTS = {"campus_chart", "all_colleges"}


@dataclass
class CollegeAPChart:
    institution_id: str
    file: str
    catalog: str | None
    academic_year: str | None
    note: str | None
    default_min_score: int
    rules: dict[str, dict] = field(default_factory=dict)        # exam id -> rule


@dataclass
class CampusAPChart:
    campus_id: str
    name: str
    unit_system: str | None
    source_period: str | None
    ge_policy: str | None
    limitations: list[str]
    rules: list[dict]
    coverage: dict[str, str]                                   # exam id -> recorded | not_listed_or_not_resolved


class APStore:
    def __init__(self) -> None:
        self.exams: dict[str, str] = {}                        # exam id -> official name
        self._by_name: dict[str, str] = {}
        self.college: dict[str, CollegeAPChart] = {}
        self.campuses: dict[str, CampusAPChart] = {}
        self.campus_dataset: dict | None = None
        self.report = LoadReport()

    def load_directory(self, folder: Path = AP_DIR) -> None:
        self.__init__()
        for path in sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []:
            try:
                raw = read_json(path)
            except (OSError, ValueError) as e:
                self.report.skip(path, f"not readable JSON ({e})")
                continue
            if isinstance(raw, dict) and isinstance(raw.get("campuses"), list) and isinstance(raw.get("exams"), list):
                self._load_campuses(path, raw)
            elif isinstance(raw, dict) and raw.get("institution_id") and isinstance(raw.get("rules"), list):
                self._load_college(path, raw)
            else:
                self.report.skip(path, "not an AP dataset this app understands (college waiver chart or campus AP chart)")
        for exam_id in {e for c in self.college.values() for e in c.rules}:
            self.exams.setdefault(exam_id, _name_from_id(exam_id))
        self._by_name = {}
        for exam_id, name in self.exams.items():
            for k in {norm(name), norm(name.removeprefix("AP ")), norm(exam_id.removeprefix("ap:")), norm(exam_id)}:
                self._by_name.setdefault(k, exam_id)

    def _load_campuses(self, path: Path, raw: dict) -> None:
        self.campus_dataset = {"file": path.name, "dataset_id": raw.get("dataset_id"), "reviewed_on": raw.get("reviewed_on"),
                               "interpretation": raw.get("interpretation") or {},
                               "unresolved_items": (raw.get("coverage") or {}).get("unresolved_items") or []}
        for e in raw["exams"]:
            self.exams[e["id"]] = e.get("name") or _name_from_id(e["id"])
        for c in raw["campuses"]:
            self.campuses[c["id"]] = CampusAPChart(
                campus_id=c["id"], name=c.get("name") or c["id"], unit_system=c.get("unit_system"),
                source_period=c.get("source_period"), ge_policy=c.get("campus_ge_policy"),
                limitations=list(c.get("limitations") or []), rules=list(c.get("rules") or []),
                coverage={e["exam_id"]: e.get("campus_rule_status") for e in c.get("exam_coverage") or []})
        self.report.loaded.append({"file": path.name, "kind": "campus_ap_chart", "campuses": len(self.campuses),
                                   "rules": sum(len(c.rules) for c in self.campuses.values()),
                                   "reviewed_on": raw.get("reviewed_on")})

    def _load_college(self, path: Path, raw: dict) -> None:
        m = re.search(r"(\d{4}-\d{4})", str(raw.get("catalog") or ""))
        chart = CollegeAPChart(institution_id=raw["institution_id"], file=path.name, catalog=raw.get("catalog"),
                               academic_year=m[1] if m else None, note=raw.get("note"),
                               default_min_score=int(raw.get("default_min_score") or 3))
        for r in raw["rules"]:
            if r.get("exam_id"):
                chart.rules[r["exam_id"]] = r
        self.college[chart.institution_id] = chart
        self.report.loaded.append({"file": path.name, "kind": "college_ap_chart", "institution_id": chart.institution_id,
                                   "academic_year": chart.academic_year, "rules": len(chart.rules)})

    # --- lookup ------------------------------------------------------------------------------------
    def normalize_exam(self, subject: str) -> str | None:
        """'AP Calculus BC' / 'Calculus BC' / 'ap:calculus_bc' -> 'ap:calculus_bc'; unknown -> None."""
        key = norm(subject)
        if key in self._by_name:
            return self._by_name[key]
        return self._by_name.get(norm(re.sub(r"^\s*ap\s+", "", subject, flags=re.IGNORECASE)))

    def exam_list(self) -> list[dict]:
        return sorted(({"id": i, "name": n} for i, n in self.exams.items()), key=lambda e: e["name"].lower())

    def status(self) -> list[dict]:
        rows = [{"kind": "college_ap_chart", "institution_id": c.institution_id, "file": c.file,
                 "academic_year": c.academic_year, "rules": len(c.rules), "status": "loaded"} for c in self.college.values()]
        if self.campus_dataset:
            rows.append({"kind": "campus_ap_chart", "file": self.campus_dataset["file"], "campuses": sorted(self.campuses),
                         "reviewed_on": self.campus_dataset["reviewed_on"], "status": "loaded"})
        return rows

    # --- evaluation --------------------------------------------------------------------------------
    def community_college_effect(self, inst_id: str, exam_id: str, score: int, relevant: set[str] | None) -> dict:
        """What the COLLEGE's chart does with this exam. `relevant` = course keys the pathway can use (to pick
        the course of a choose-one waiver that this plan needs); None = no plan context (the choice stays open)."""
        chart = self.college.get(inst_id)
        if chart is None:
            return {"status": "not_loaded", "institution_id": inst_id, "waived_courses": [],
                    "message": "No AP chart is loaded for this college."}
        rule = chart.rules.get(exam_id)
        base = {"institution_id": inst_id, "source": chart.catalog, "academic_year": chart.academic_year}
        if rule is None:
            return {**base, "status": "not_listed", "waived_courses": [],
                    "message": "This exam isn't in the college's AP chart (not a denial; check with the college)."}
        minimum = int(rule.get("min_score") or chart.default_min_score)
        out = {**base, "minimum_score": minimum, "listed_courses": list(rule.get("waives") or []),
               "choose_one": bool(rule.get("choose_one")), "conditions": list(rule.get("conditions") or []),
               "ge_areas_text": rule.get("cal_getc"), "waived_courses": []}
        if score < minimum:
            return {**out, "status": "below_minimum", "message": f"Needs a score of {minimum} or higher."}
        listed = out["listed_courses"]
        if not listed:
            return {**out, "status": "no_course_waiver", "message": "The chart lists no college course for this exam."}
        if out["choose_one"]:
            if relevant is None:
                return {**out, "status": "choose_one", "message": (
                    f"Credit for ONE of {' or '.join(listed)} (the plan applies the one your major uses).")}
            usable = [c for c in listed if code_key(c) in relevant]
            if not usable:
                return {**out, "status": "choice_not_applied", "message": (
                    f"Credit for one of {' or '.join(listed)} (student's choice); none of them is part of this plan, "
                    f"so no course was marked complete.")}
            return {**out, "status": "applied", "waived_courses": [usable[0]], "chosen_from": listed,
                    "message": f"Counts as {usable[0]} (one of {' or '.join(listed)}; the choice is yours to confirm)."}
        return {**out, "status": "applied", "waived_courses": listed,
                "message": f"Waives {', '.join(listed)}."}

    def target_campus_effect(self, campus_id: str | None, exam_id: str, score: int) -> dict:
        """The TARGET university's published AP rules for this exam and score. Reported only, never applied."""
        chart = self.campuses.get(campus_id or "")
        if chart is None:
            return {"status": "not_loaded", "campus_id": campus_id, "rules": [],
                    "message": "No AP credit data is loaded for this university."}
        base = {"campus_id": chart.campus_id, "campus": chart.name, "unit_system": chart.unit_system,
                "source_period": chart.source_period, "limitations": chart.limitations}
        coverage = chart.coverage.get(exam_id)
        all_rules = [r for r in chart.rules if exam_id in (r.get("exam_ids") or [])]
        if not all_rules:
            return {**base, "status": "not_listed", "coverage": coverage, "rules": [],
                    "message": "Not listed in this campus chart (unknown, not a denial)."}
        rules = [_rule(r) for r in all_rules if score in (r.get("scores") or [])]
        if not rules:
            return {**base, "status": "no_rule_for_score", "coverage": coverage, "rules": [],
                    "message": f"The chart lists this exam but no award for a score of {score} (unknown, not a denial)."}
        manual = any(r["manual_review"] for r in rules)
        specific = any(r["context_scope"] == "specific" for r in rules)
        return {**base, "status": "manual_review" if manual else "requires_context_and_policy_review",
                "coverage": coverage, "rules": rules, "context_specific": specific,
                "message": ("Published rule(s) found; the campus decides course credit, GE and major use"
                            + (", and some rules apply only to certain colleges/majors" if specific else "")
                            + ". Not applied to your community college plan.")}


def _rule(r: dict) -> dict:
    return {"id": r.get("id"), "context": r.get("context"),
            "context_scope": "general" if r.get("context") in GENERAL_CONTEXTS else "specific",
            "course_credit": r.get("course_credit"), "ge_or_other_requirements": r.get("ge_or_other_requirements"),
            "units": r.get("units"), "ge_units": r.get("ge_units"), "conditions": list(r.get("conditions") or []),
            "exam_period": r.get("exam_period") or r.get("exam_year"), "combination": r.get("combination"),
            "manual_review": bool(r.get("manual_review")), "automation_status": r.get("automation_status")}


def _name_from_id(exam_id: str) -> str:
    return "AP " + exam_id.removeprefix("ap:").replace("_", " ").title()
