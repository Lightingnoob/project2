"""
Canonical institution ids and name matching for the pathway store.

Each institution has ONE canonical id, declared by the articulation data that introduces it ("chabot",
"csueb", "berkeley"). Any spelling a person or a dropdown might use resolves to that id:

    "California State University, East Bay" | "Cal State East Bay" | "CSU East Bay" | "CSUEB"  -> csueb
    "University of California, Berkeley" | "UC Berkeley" | "UCB"                                -> berkeley
    "Chabot College" | "Chabot"                                                                  -> chabot

Everyday spellings are derived from the official name by rule (name_variants), plus any aliases the data
lists. Matching ignores case, accents and punctuation. A spelling two institutions would share is never
used, and a name nobody registered resolves to a private key of itself, so it only matches itself.
"""

from __future__ import annotations

from tag_engine.selectors import norm

STOP_WORDS = frozenset({"of", "the", "at", "and"})


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w not in STOP_WORDS)


def name_variants(name: str) -> set[str]:
    """Everyday spellings of an official name, normalized:
    'California State University, East Bay' -> cal state east bay, csu east bay, csueb, ...
    'University of California, Berkeley' -> uc berkeley, ucb      'Chabot College' -> chabot"""
    n = norm(name)
    words = n.split()
    out = {n}
    for prefix in ("california state university ", "cal state university ", "cal state ", "csu "):
        if n.startswith(prefix):
            campus = n.removeprefix(prefix)
            out |= {f"california state university {campus}", f"cal state {campus}", f"csu {campus}",
                    "csu" + _initials(campus.split())}
    for prefix in ("university of california ", "uc "):
        if n.startswith(prefix):
            campus = n.removeprefix(prefix)
            out |= {f"university of california {campus}", f"uc {campus}", "uc" + _initials(campus.split())}
    if len(words) > 1 and words[-1] == "college":
        out.add(" ".join(words[:-1]))                           # "Chabot College" -> "chabot"
        if len(words) > 2 and words[-2] == "community":
            out.add(" ".join(words[:-2]))
    if sum(w not in STOP_WORDS for w in words) >= 3:
        out.add(_initials(words))                               # "Diablo Valley College" -> "dvc"
    return {v for v in out if v}


class InstitutionRegistry:
    def __init__(self) -> None:
        self._exact: dict[str, str] = {}             # normalized id / official name / listed alias -> id
        self._variants: dict[str, set[str]] = {}     # rule-derived spelling -> ids that produce it
        self._names: dict[str, str] = {}             # id -> official name

    def register(self, inst_id: str, name: str | None = None, aliases: tuple[str, ...] | list[str] = ()) -> str:
        """Record an institution. The first registration of an id, name or alias wins."""
        self._exact.setdefault(norm(inst_id), inst_id)
        if name:
            self._names.setdefault(inst_id, name)
            self._exact.setdefault(norm(name), inst_id)
            for v in name_variants(name):
                self._variants.setdefault(v, set()).add(inst_id)
        for alias in aliases:
            self._exact.setdefault(norm(alias), inst_id)
        return inst_id

    def resolve(self, text: str) -> str:
        """Canonical id for any spelling; an unregistered name gets a private key that only matches itself."""
        key = norm(text)
        if key in self._exact:
            return self._exact[key]
        ids = self._variants.get(key, set())
        return next(iter(ids)) if len(ids) == 1 else "~" + key.replace(" ", "-")

    def known(self, text: str) -> bool:
        return not self.resolve(text).startswith("~")

    def same(self, a: str, b: str) -> bool:
        return self.resolve(a) == self.resolve(b)

    def name(self, inst_id: str) -> str | None:
        return self._names.get(inst_id)
