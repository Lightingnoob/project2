"""
Runtime data stores, each loaded ONCE at startup and indexed (articulation lives in the `articulation` package):

    catalog.CatalogStore            data/catalogs/       course search, titles, units, catalog text
    prerequisites.PrerequisiteStore data/prerequisites/  scheduling prerequisite rules (+ non-guessing resolver)
    ge.GEStore                      data/ge/             college Cal-GETC lists; UC 7-course (admission requirement)
    ap.APStore                      data/ap/             college AP waivers; target-campus AP charts (separate)
    reference files                 data/reference/      listed for audit only, never used for planning

DataRegistry.load() builds all of them plus one shared institution registry (canonical ids for every college
and university any dataset names).
"""

from __future__ import annotations

import logging
from pathlib import Path

from articulation.institutions import InstitutionRegistry

from .ap import APStore
from .catalog import CatalogStore
from .common import DATA_DIR, read_json
from .ge import GEStore
from .prerequisites import PrerequisiteStore

log = logging.getLogger("uvicorn.error")
REFERENCE_DIR = DATA_DIR / "reference"


def university_system(name: str) -> str | None:
    n = name.lower()
    if n.startswith("uc") or "university of california" in n:
        return "UC"
    if "california state" in n or n.startswith("cal state") or n.startswith("csu") or "cal poly" in n:
        return "CSU"
    return None


class DataRegistry:
    def __init__(self, root: Path = DATA_DIR) -> None:
        self.root = Path(root)
        self.institutions = InstitutionRegistry()
        self.catalogs = CatalogStore()
        self.prereqs = PrerequisiteStore()
        self.ge = GEStore()
        self.ap = APStore()
        self.reference: list[dict] = []
        self.colleges: dict[str, str] = {}          # id -> name
        self.universities: dict[str, str] = {}

    def load(self, pathway_store=None) -> DataRegistry:
        root = self.root
        self.institutions = InstitutionRegistry()
        self.colleges, self.universities = {}, {}
        self.prereqs.load_directory(root / "prerequisites")
        self.ge.load_directory(root / "ge")
        self.ap.load_directory(root / "ap")
        if pathway_store is not None:
            for p in pathway_store.pathways:
                if p.source != "mock":
                    self._college(p.college_ref, p.college)
                    self._university(p.university_ref, p.university)
        for sets in self.ge.college.values():
            for d in sets:
                self._college(d.institution_id, d.institution_name)
        for campus in self.ap.campuses.values():
            self._university(campus.campus_id, campus.name)
        for inst_id in [*self.prereqs.institutions, *self.ap.college]:
            self.institutions.register(inst_id)
        self.catalogs.load_directory(root / "catalogs", resolve_institution=self._resolve_college_name)
        for inst_id, sets in ((k[0], v) for k, v in self.ge.college.items()):
            for d in sets:
                self.catalogs.add_former_codes(inst_id, {f: c.code for c in d.courses.values() for f in c.former_codes})
        self.reference = self._scan_reference(root / "reference")
        log.info("Data loaded: %d catalog(s), %d prerequisite registr(ies), %d GE list(s)%s, %d AP chart(s).",
                 sum(len(v) for v in self.catalogs.catalogs.values()), len(self.prereqs.institutions),
                 sum(len(v) for v in self.ge.college.values()), " + UC 7-course" if self.ge.uc_seven else "",
                 len(self.ap.college) + (1 if self.ap.campus_dataset else 0))
        return self

    def _college(self, inst_id: str, name: str) -> None:
        self.institutions.register(inst_id, name)
        self.colleges.setdefault(inst_id, name)

    def _university(self, inst_id: str, name: str) -> None:
        self.institutions.register(inst_id, name)
        self.universities.setdefault(inst_id, name)

    def _resolve_college_name(self, name: str) -> str:
        inst = self.institutions.resolve(name)
        if inst.startswith("~"):                       # a college no other dataset names: register it by its name
            inst = inst.lstrip("~").replace("-", "_")
            self._college(inst, name)
        return inst

    def resolve(self, text: str) -> str:
        return self.institutions.resolve(text)

    @staticmethod
    def _scan_reference(folder: Path) -> list[dict]:
        rows = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            kind, detail = "unknown", None
            try:
                raw = read_json(path)
                if isinstance(raw, dict) and isinstance(raw.get("pages"), list):
                    kind, detail = "raw_catalog_pages", f"{raw.get('catalog')}: {len(raw['pages'])} pages"
                elif isinstance(raw, dict) and isinstance(raw.get("campuses"), dict) and "how_to_fill" in raw:
                    filled = sum(bool(v) for v in raw["campuses"].values())
                    kind, detail = "manual_articulation_template", f"{filled} of {len(raw['campuses'])} campuses filled in"
                elif isinstance(raw, dict):
                    kind = "reference_json"
            except (OSError, ValueError) as e:
                kind, detail = "unreadable", str(e)
            rows.append({"file": path.name, "kind": kind, "detail": detail, "size_bytes": path.stat().st_size,
                         "runtime_use": "none (audit / source lookup / debugging only)"})
        return rows

    # --- diagnostics -----------------------------------------------------------------------------
    def status(self, pathway_store=None, tag_registry=None, affiliations=None) -> dict:
        art = None
        if pathway_store is not None:
            listing = pathway_store.listing()
            art = {"status": "loaded" if listing["available"] else "not_loaded",
                   "datasets_loaded": len([p for p in listing["available"] if p["source"] != "mock"]),
                   "pathways": [{"file": p["file"], "college": p["college"], "university": p["university"],
                                 "major": p["major"], "degree": p["degree"], "academic_year": p["academic_year"],
                                 "source": p["source"]} for p in listing["available"]],
                   "unsupported": listing["unsupported"], "skipped": listing["skipped"]}
        out = {
            "articulation": art,
            "catalogs": {"status": "loaded" if self.catalogs.catalogs else "not_loaded",
                         "datasets": self.catalogs.status(), "skipped": self.catalogs.report.skipped},
            "prerequisites": {"status": "loaded" if self.prereqs.institutions else "not_loaded",
                              "datasets": self.prereqs.status(), "skipped": self.prereqs.report.skipped},
            "ge": {"status": "loaded" if (self.ge.college or self.ge.uc_seven) else "not_loaded",
                   "datasets": self.ge.status(), "skipped": self.ge.report.skipped},
            "ap": {"status": "loaded" if (self.ap.college or self.ap.campuses) else "not_loaded",
                   "datasets": self.ap.status(), "exams": len(self.ap.exams), "skipped": self.ap.report.skipped},
            "reference_only": self.reference,
        }
        if tag_registry is not None:
            out["tag"] = {"status": "loaded" if tag_registry.datasets else "not_loaded",
                          "datasets": [{"id": d.dataset_id, "title": d.label, "accurate_as_of": d.accurate_as_of,
                                        "file": d.path.name if d.path else None} for d in tag_registry.datasets],
                          "major_affiliations": "loaded" if affiliations is not None else "not_loaded"}
        return out
