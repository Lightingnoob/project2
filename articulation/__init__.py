"""
Articulation data: which community college courses a university accepts for a major (major preparation).

    DATA      data/articulation/*.json         one agreement per file (schema.py; README.md in that folder)
    read      loader.read_dataset()            parse + validate
    convert   loader.build_pathway()           -> sep_engine Course catalog + TransferAgreement, or unsupported
    store     store.PathwayStore               load the folder, find a pathway, list what loaded (GET /pathways)

Separate from GE (general education), TAG (admission rules) and major affiliations: the planner joins them.
"""

from .institutions import InstitutionRegistry, name_variants
from .loader import NotArticulationData, UnsupportedAgreement, build_pathway, read_dataset, validate_dataset
from .models import Pathway, SkippedFile, UnsupportedPathway
from .schema import ArticulationDataset
from .store import ARTICULATION_DIR, Lookup, Match, PathwayStore, academic_year_of

__all__ = [
    "ARTICULATION_DIR", "ArticulationDataset", "InstitutionRegistry", "Lookup", "Match", "NotArticulationData",
    "Pathway", "PathwayStore", "SkippedFile", "UnsupportedAgreement", "UnsupportedPathway", "academic_year_of",
    "build_pathway", "name_variants", "read_dataset", "validate_dataset",
]
