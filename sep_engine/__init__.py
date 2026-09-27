"""
TransferPath SEP engine.

Pipeline (see pipeline.generate_sep):
  1. pathway.select_pathway    — Dijkstra over requirement choices (GE + major prep options)
  2. toposort.topological_sort — Kahn's algorithm with cycle detection
  3. scheduler.schedule_courses — CSP: DFS backtracking + forward checking into terms
"""

from .errors import CycleError, InfeasibleRequirementError, SchedulingError, SEPError, UnknownCourseError
from .models import (Course, GroupCategory, MeetingTime, PlanConfig, RequirementGroup, SEPRequest,
                     StudentProfile, Term, TransferAgreement)
from .pathway import select_pathway
from .pipeline import analyze_workload, choose_plans, generate_sep
from .scheduler import schedule_courses
from .toposort import topological_sort

__all__ = [
    "Course", "CycleError", "GroupCategory", "InfeasibleRequirementError", "MeetingTime",
    "PlanConfig", "RequirementGroup", "SEPError", "SEPRequest", "SchedulingError", "StudentProfile", "Term",
    "TransferAgreement", "UnknownCourseError", "analyze_workload", "choose_plans", "generate_sep",
    "schedule_courses", "select_pathway", "topological_sort",
]
