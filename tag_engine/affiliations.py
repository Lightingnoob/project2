"""
UC major affiliations: which school or college offers a major at a TAG campus.

    DATA      data/uc_major_affiliations.json    campus -> organization (school / college) -> major -> degrees,
                                                 with official names, aliases and the source of every mapping
    load      load_affiliations()                validate + index (read-only); a broken file gives an empty registry
    resolve   MajorAffiliationRegistry.lookup(university, major, degree)
                 resolved     one organization offers it (with its parents: major -> school -> college)
                 ambiguous    the name is offered by several (UC Santa Barbara Mathematics: L&S and Creative Studies)
                 unresolved   not listed, listed as unresolved (a joint major), or not offered with that degree

The TAG matrix excludes some majors by school or college and sets some minimum GPAs that way; the evaluator
joins this data with it (MajorProfile.from_student). Campus ids are the TAG matrix's, and campus and major
names are matched with the same normalization as the TAG rules (selectors.norm, split_degree, degree_key).
Nothing is guessed: only a single listed organization ever resolves.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from .data import DATA_DIR, SUPPORTED_SCHEMA_MAJOR, TagDatasetError, _fail, _seq, _str, freeze, log
from .selectors import campus_keys, degree_key, join_names, norm, split_degree

AFFILIATIONS_FILE = DATA_DIR / "uc_major_affiliations.json"
ORGANIZATION_TYPES = ("school", "college")


# =====================================================================
# Types
# =====================================================================
@dataclass(frozen=True)
class AffiliationSource:
    id: str
    title: str
    url: str | None                   # a web page, or
    document: str | None              # a document without a URL (e.g. the TAG matrix PDF)
    publisher: str | None
    retrieved_on: str | None


@dataclass(frozen=True)
class Organization:
    """A school or college, typed as the campus names it."""

    campus_id: str
    id: str
    type: str                         # 'school' | 'college'
    name: str                         # official name
    aliases: tuple[str, ...]          # other official spellings / abbreviations, or the TAG matrix's spelling
    parent_id: str | None             # a school within a college
    sources: tuple[AffiliationSource, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class MajorAffiliation:
    """One major at one campus, placed in its organization: major -> school -> college."""

    campus_id: str
    campus_name: str
    major: str                        # as the source names it
    degree: str | None                # the requested degree as listed, else the only listed degree
    degrees: tuple[str, ...]          # every degree listed for it (empty: the source doesn't say)
    organizations: tuple[Organization, ...]   # the offering organization first, then its parents
    source_urls: tuple[str, ...]      # the major's own page, then its organizations' sources

    @property
    def organization(self) -> Organization:
        return self.organizations[0]

    @property
    def school(self) -> Organization | None:
        return next((o for o in self.organizations if o.type == "school"), None)

    @property
    def college(self) -> Organization | None:
        return next((o for o in self.organizations if o.type == "college"), None)

    @property
    def units(self) -> tuple[str, ...]:
        """Official names, offering organization first (for messages)."""
        return tuple(o.name for o in self.organizations)

    @property
    def unit_names(self) -> tuple[str, ...]:
        """Every name of every organization (for matching TAG rules, which may use another spelling)."""
        return tuple(n for o in self.organizations for n in o.names)

    def as_dict(self) -> dict[str, Any]:
        s, c = self.school, self.college
        return {"campus_id": self.campus_id, "school_id": s and s.id, "school_name": s and s.name,
                "college_id": c and c.id, "college_name": c and c.name, "major": self.major, "degree": self.degree,
                "source_urls": list(self.source_urls)}


@dataclass(frozen=True)
class AffiliationLookup:
    status: Literal["resolved", "ambiguous", "unresolved"]
    affiliation: MajorAffiliation | None = None           # when resolved
    candidates: tuple[MajorAffiliation, ...] = ()         # when ambiguous: one per organization
    reason: str | None = None     # why it isn't resolved, as a clause ("UC Irvine lists X only as B.S., not B.A.")
    source_urls: tuple[str, ...] = ()                     # where the answer (or the unresolved entry) comes from


@dataclass(frozen=True)
class _Major:
    name: str
    keys: frozenset[str]              # normalized name and aliases
    degrees: tuple[str, ...]
    organization: Organization
    source_url: str | None


@dataclass(frozen=True)
class _Unresolved:
    name: str
    keys: frozenset[str]
    reason: str
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class _Campus:
    id: str
    name: str
    keys: frozenset[str]
    organizations: Mapping[str, Organization]
    majors: tuple[_Major, ...]
    unresolved: tuple[_Unresolved, ...]


# =====================================================================
# Registry
# =====================================================================
UNVERIFIED = "academic school/college affiliation for this major could not be verified"


class MajorAffiliationRegistry:
    def __init__(self, campuses: Iterable[_Campus] = ()) -> None:
        self._campuses = {c.id: c for c in campuses}

    @property
    def campus_ids(self) -> tuple[str, ...]:
        return tuple(self._campuses)

    def campus(self, university: str | None) -> _Campus | None:
        """By TAG campus id ('irvine'), name ('UC Irvine'), official name or alias ('UCI')."""
        key = norm(university)
        return next((c for c in self._campuses.values() if key and key in c.keys), None)

    def resolve(self, university: str | None, major: str | None, degree: str | None = None) -> MajorAffiliation | None:
        """The major's affiliation when exactly one organization offers it; otherwise None (see lookup)."""
        return self.lookup(university, major, degree).affiliation

    def lookup(self, university: str | None, major: str | None, degree: str | None = None) -> AffiliationLookup:
        """
        Where `major` sits at `university`. The degree may be part of the major ('Computer Science, B.S.') or
        given separately; with a degree, only listings with that degree count (UC Irvine Psychology is a B.A. in
        Social Ecology and a B.S. in Social Sciences).
        """
        campus = self.campus(university)
        name, printed = split_degree(major) if major and major.strip() else (None, None)
        degree = degree or printed
        if campus is None or not name:
            return AffiliationLookup("unresolved", reason=UNVERIFIED)
        key = norm(name)
        flagged = next((u for u in campus.unresolved if key in u.keys), None)
        if flagged is not None:
            return AffiliationLookup("unresolved", reason=flagged.reason, source_urls=flagged.source_urls)
        listed = [m for m in campus.majors if key in m.keys]
        if not listed:
            return AffiliationLookup("unresolved", reason=UNVERIFIED)
        if degree:
            wanted = degree_key(degree)
            offered = [m for m in listed if not m.degrees or wanted in {degree_key(d) for d in m.degrees}]
            if not offered:
                degrees = sorted({d for m in listed for d in m.degrees})
                return AffiliationLookup("unresolved", reason=f"{campus.name} lists {listed[0].name} only as "
                                                              f"{join_names(degrees, 'or')}, not {degree}")
            listed = offered
        by_org: dict[str, list[_Major]] = {}
        for m in listed:
            by_org.setdefault(m.organization.id, []).append(m)
        found = tuple(self._affiliation(campus, ms, degree) for ms in by_org.values())
        if len(found) == 1:
            return AffiliationLookup("resolved", affiliation=found[0], source_urls=found[0].source_urls)
        return AffiliationLookup("ambiguous", candidates=found,
                                 reason=f"{campus.name} offers {found[0].major} in "
                                        f"{join_names((f'the {a.organization.name}' for a in found), 'and')}",
                                 source_urls=tuple(dict.fromkeys(u for a in found for u in a.source_urls)))

    def organizations(self, university: str | None) -> tuple[Organization, ...]:
        campus = self.campus(university)
        return tuple(campus.organizations.values()) if campus else ()

    def majors(self, university: str | None) -> tuple[str, ...]:
        """Every major name listed for the campus (resolvable or not)."""
        campus = self.campus(university)
        return tuple(dict.fromkeys([*(m.name for m in campus.majors), *(u.name for u in campus.unresolved)])) if campus else ()

    def majors_in(self, university: str | None, organization: str) -> tuple[str, ...]:
        """Majors an organization (id or any of its names) offers, its schools' majors included."""
        campus = self.campus(university)
        if campus is None:
            return ()
        key = norm(organization)
        org = next((o for o in campus.organizations.values() if key in {norm(o.id), *map(norm, o.names)}), None)
        if org is None:
            return ()
        return tuple(dict.fromkeys(m.name for m in campus.majors
                                   if org.id in {o.id for o in _chain(campus, m.organization)}))

    @staticmethod
    def _affiliation(campus: _Campus, majors: list[_Major], degree: str | None) -> MajorAffiliation:
        first = majors[0]
        degrees = tuple(dict.fromkeys(d for m in majors for d in m.degrees))
        chosen = next((d for d in degrees if degree and degree_key(d) == degree_key(degree)), None)
        chain = _chain(campus, first.organization)
        urls = [m.source_url for m in majors if m.source_url]
        urls += [s.url for o in chain for s in o.sources if s.url]
        return MajorAffiliation(campus.id, campus.name, first.name, chosen or (degrees[0] if len(degrees) == 1 else None),
                                degrees, chain, tuple(dict.fromkeys(urls)))


def _chain(campus: _Campus, org: Organization) -> tuple[Organization, ...]:
    chain = [org]
    while chain[-1].parent_id:
        chain.append(campus.organizations[chain[-1].parent_id])
    return tuple(chain)


# =====================================================================
# Loading
# =====================================================================
def load_affiliations(path: Path | str = AFFILIATIONS_FILE) -> MajorAffiliationRegistry:
    """
    Load the affiliation data. A missing or broken file is logged and gives an empty registry, so the API
    keeps running: every major's school/college is then unknown, and school- and college-level TAG rules
    need review instead of passing.
    """
    path = Path(path)
    try:
        return normalize_affiliations(json.loads(path.read_text(encoding="utf-8")), where=path.name)
    except (OSError, ValueError) as e:                   # JSONDecodeError and TagDatasetError are ValueErrors
        log.error("Major affiliations %s could not be loaded (%s). School- and college-level TAG rules will "
                  "need review.", path, e)
        return MajorAffiliationRegistry()


@lru_cache(maxsize=1)
def default_affiliations() -> MajorAffiliationRegistry:
    """The bundled data, loaded once (what the evaluator uses unless given another registry)."""
    return load_affiliations()


def normalize_affiliations(raw: Mapping[str, Any], *, where: str = "affiliations") -> MajorAffiliationRegistry:
    """Raw affiliation JSON -> registry. Raises TagDatasetError naming the bad location."""
    if not isinstance(raw, Mapping):
        raise TagDatasetError(f"{where}: the top level must be an object")
    try:
        return _registry(freeze(raw), where)
    except (KeyError, TypeError, AttributeError) as e:
        raise TagDatasetError(f"{where}: unexpected structure ({type(e).__name__}: {e})") from None


def _registry(src: Mapping, where: str) -> MajorAffiliationRegistry:
    version = _str(src, "schema_version", where)
    if version.split(".")[0] != SUPPORTED_SCHEMA_MAJOR:
        raise _fail(where, f"schema_version {version} isn't supported (expected {SUPPORTED_SCHEMA_MAJOR}.x)")
    sources = {}
    for sid, s in (src.get("sources") or {}).items():
        w = f"{where}.sources.{sid}"
        url, document = _str(s, "url", w, required=False), _str(s, "document", w, required=False)
        if not url and not document:
            raise _fail(w, "needs a 'url' or a 'document'")
        sources[sid] = AffiliationSource(sid, _str(s, "title", w), url, document, _str(s, "publisher", w, required=False),
                                         _str(s, "retrieved_on", w, required=False))

    def cited(ids: Any, w: str) -> tuple[AffiliationSource, ...]:
        missing = [i for i in ids or () if i not in sources]
        if missing:
            raise _fail(w, f"unknown source ids {missing}")
        return tuple(sources[i] for i in ids or ())

    campuses, seen = [], set()
    for i, c in enumerate(_seq(src, "campuses", where)):
        w = f"{where}.campuses[{i}]"
        cid = _str(c, "id", w)
        if cid in seen:
            raise _fail(w, f"duplicate campus id {cid!r}")
        seen.add(cid)
        w = f"{w}({cid})"
        name = _str(c, "name", w)
        aliases = [a for a in (c.get("official_name"), *(c.get("aliases") or ())) if a]
        orgs: dict[str, Organization] = {}
        majors: list[_Major] = []
        for j, o in enumerate(_seq(c, "organizations", w)):
            ow = f"{w}.organizations[{j}]"
            oid, kind = _str(o, "id", ow), _str(o, "type", ow)
            if oid in orgs:
                raise _fail(ow, f"duplicate organization id {oid!r}")
            if kind not in ORGANIZATION_TYPES:
                raise _fail(f"{ow}.type", f"expected one of {', '.join(ORGANIZATION_TYPES)}, got {kind!r}")
            org_aliases = []
            for k, a in enumerate(_seq(o, "aliases", ow, required=False)):
                cited([_str(a, "source", f"{ow}.aliases[{k}]")], f"{ow}.aliases[{k}]")
                org_aliases.append(_str(a, "name", f"{ow}.aliases[{k}]"))
            org = Organization(cid, oid, kind, _str(o, "name", ow), tuple(org_aliases),
                               _str(o, "parent_id", ow, required=False), cited(o.get("sources"), f"{ow}.sources"))
            orgs[oid] = org
            for k, m in enumerate(_seq(o, "majors", ow)):
                mw = f"{ow}.majors[{k}]"
                mname = _str(m, "name", mw)
                url = _str(m, "source_url", mw, required=False)
                if not org.sources and not url:
                    raise _fail(mw, "has no source: give the organization 'sources' or the major a 'source_url'")
                majors.append(_Major(mname, frozenset(norm(n) for n in (mname, *(m.get("aliases") or ()))),
                                     tuple(m.get("degrees") or ()), org, url))
        for oid, org in orgs.items():                     # parents exist and never loop
            chain, parent = {oid}, org.parent_id
            while parent:
                if parent not in orgs or parent in chain:
                    raise _fail(f"{w}.organizations({oid}).parent_id", f"{parent!r} is unknown or loops")
                chain.add(parent)
                parent = orgs[parent].parent_id
        unresolved = []
        for k, u in enumerate(_seq(c, "unresolved", w, required=False)):
            uw = f"{w}.unresolved[{k}]"
            urls = [_str(u, "source_url", uw, required=False)] + [s.url for s in cited(u.get("sources"), f"{uw}.sources")]
            uname = _str(u, "name", uw)
            unresolved.append(_Unresolved(uname, frozenset({norm(uname)}), _str(u, "reason", uw),
                                          tuple(dict.fromkeys(x for x in urls if x))))
        campuses.append(_Campus(cid, name, frozenset(campus_keys(cid, name, *aliases)), MappingProxyType(orgs),
                                tuple(majors), tuple(unresolved)))
    return MajorAffiliationRegistry(campuses)
