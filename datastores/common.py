"""Small helpers every store shares: course-code keys, academic-year status, dataset file listings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

YearStatus = Literal["exact", "older_year", "newer_year", "unknown"]


def code_key(code: str | None) -> str:
    """Spacing/case-insensitive course key: 'mth1', 'MTH 1', 'Mth-1' -> 'MTH1'."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def id_to_key(course_id: str) -> str:
    """'chabot:MTH1' -> 'MTH1' (dataset course ids are '<institution>:<code without spaces>')."""
    return code_key(course_id.split(":", 1)[-1])


def split_code(key: str) -> str:
    """Best-effort display code for a spaceless key when no catalog entry names it: 'MTH31S' -> 'MTH 31S'."""
    m = (re.fullmatch(r"([A-Z]+?)(C\d{4}[A-Z]*)", key) or re.fullmatch(r"([A-Z]+)(\d+[A-Z]*)", key)
         or re.fullmatch(r"([A-Z]+)([A-Z]\d+[A-Z]*)", key))
    return f"{m[1]} {m[2]}" if m else key


CLEAN_CODE = re.compile(r"^[A-Z]{2,5} ?[A-Z]?\d{1,4}[A-Z]{0,3}$")


def academic_year_of(term: str | None) -> str | None:
    """'Fall 2026' -> '2026-2027'; 'Spring 2027' / 'Summer 2027' -> '2026-2027'."""
    m = re.fullmatch(r"\s*(fall|spring|summer)\s+(\d{4})\s*", term or "", re.IGNORECASE)
    if not m:
        return None
    start = int(m[2]) if m[1].lower() == "fall" else int(m[2]) - 1
    return f"{start}-{start + 1}"


def year_status(dataset_year: str | None, wanted: str | None) -> YearStatus:
    if not dataset_year or not wanted:
        return "unknown"
    if dataset_year == wanted:
        return "exact"
    return "older_year" if dataset_year < wanted else "newer_year"


def pick_year(years: list[str], wanted: str | None) -> str | None:
    """The wanted academic year if loaded, else the latest EARLIER one, never a later one (None = no usable year)."""
    if not wanted:
        return max(years) if years else None
    usable = [y for y in years if y <= wanted]
    return max(usable) if usable else None


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class LoadReport:
    """What one store read: datasets it uses, files it skipped (with the reason)."""

    loaded: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def skip(self, path: Path, reason: str) -> None:
        self.skipped.append({"file": path.name, "reason": reason})
