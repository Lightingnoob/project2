"""
TAG (Transfer Admission Guarantee) engine: UC TAG rules as data, evaluated for one student.

Separate from sep_engine (scheduling): it neither imports it nor changes plans or prerequisite graphs.
The API (main.py) builds a TagStudentProfile from the planner's request and plans and calls
evaluate_tag(); TAG admission criteria never become course prerequisites.

    DATA        data/uc_tag_requirements_*.json   one file per admission cycle, kept verbatim
                data/uc_major_affiliations.json   campus -> school/college -> major -> degree (affiliations.py)
    load        data.load_registry()              validate + normalize (read-only), index by entry term
    select      selectors                         campus/term lookup, exclusion matcher, GPA resolver, ...
    evaluate    evaluator.evaluate_tag()          -> TagEvaluation (eligible / ineligible / needs_review /
                                                     not_applicable, with one TagCheck per requirement)
"""

from .affiliations import (
    AFFILIATIONS_FILE, AffiliationLookup, MajorAffiliation, MajorAffiliationRegistry, Organization,
    default_affiliations, load_affiliations, normalize_affiliations,
)
from .data import (
    DATA_DIR, TagDatasetError, TagDatasetRegistry, campus_directory, freeze, load_registry, load_tag_dataset,
    normalize_tag_dataset, registry_from, thaw,
)
from .evaluator import evaluate_tag, evaluate_tag_eligibility
from .models import (
    EntryTerm, GpaResolution, MajorAffiliationRef, TagCampus, TagCheck, TagDataset, TagDatasetRef, TagEvaluation,
    TagGeStatus, TagStudentProfile, TermRef,
)
from .selectors import MajorProfile, match_exclusions, resolve_gpa_rule

__all__ = [
    "AFFILIATIONS_FILE", "AffiliationLookup", "DATA_DIR", "EntryTerm", "GpaResolution", "MajorAffiliation",
    "MajorAffiliationRef", "MajorAffiliationRegistry", "MajorProfile", "Organization", "TagCampus", "TagCheck",
    "TagDataset", "TagDatasetError", "TagDatasetRef", "TagDatasetRegistry", "TagEvaluation", "TagGeStatus",
    "TagStudentProfile", "TermRef", "campus_directory", "default_affiliations", "evaluate_tag",
    "evaluate_tag_eligibility", "freeze", "load_affiliations", "load_registry", "load_tag_dataset",
    "match_exclusions", "normalize_affiliations", "normalize_tag_dataset", "registry_from", "resolve_gpa_rule", "thaw",
]
