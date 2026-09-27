"""
Algorithm 1 — Prerequisite resolution via topological sorting (Kahn's algorithm).

Courses are nodes; an edge P -> C means "P is a prerequisite of C". Only the
courses in the plan participate; completed courses are treated as already
satisfied and dropped from the graph. Co-requisites are NOT edges here: they
allow same-term enrollment, which the scheduler handles.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .errors import CycleError, SEPError, UnknownCourseError
from .models import Course


def get_course(catalog: Mapping[str, Course], code: str, context: str = "") -> Course:
    try:
        return catalog[code]
    except KeyError:
        raise UnknownCourseError(code, context) from None


def topological_sort(
    catalog: Mapping[str, Course],
    courses: Iterable[str],
    completed: Iterable[str] = (),
    key: Callable[[str], Any] | None = None,
) -> list[str]:
    """
    Return the plan's courses in an order where every prerequisite comes first.

    Kahn's algorithm touches each node and edge once, O(V + E); the ready set
    is a heap so ties break deterministically by `key` (default: course code),
    adding a log V factor per node.

    Raises CycleError (naming the loop) if prerequisites are circular, and
    SEPError if a prerequisite is neither completed nor part of the plan.
    """
    done = set(completed)
    nodes = set(courses) - done
    key = key or (lambda c: c)

    children: dict[str, list[str]] = {c: [] for c in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for c in nodes:
        for p in get_course(catalog, c).prereqs:
            if p in nodes:
                children[p].append(c)
                indegree[c] += 1
            elif p not in done:
                get_course(catalog, p, context=c)   # unknown code -> UnknownCourseError
                raise SEPError(f"{c} requires {p}, which is neither completed nor part of the plan.")

    ready = [(key(c), c) for c in nodes if indegree[c] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, c = heapq.heappop(ready)
        order.append(c)
        for child in children[c]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (key(child), child))

    if len(order) < len(nodes):
        raise CycleError(_find_cycle(catalog, nodes - set(order)))
    return order


def _find_cycle(catalog: Mapping[str, Course], nodes: set[str]) -> list[str]:
    """DFS over the nodes Kahn's algorithm could not release; returns e.g. [A, B, A]."""
    state: dict[str, int] = {}   # 1 = on the current DFS path, 2 = fully explored
    path: list[str] = []

    def dfs(u: str) -> list[str] | None:
        state[u] = 1
        path.append(u)
        for p in catalog[u].prereqs:
            if p not in nodes:
                continue
            if state.get(p) == 1:
                return path[path.index(p):] + [p]
            if p not in state and (found := dfs(p)):
                return found
        state[u] = 2
        path.pop()
        return None

    for start in sorted(nodes):
        if start not in state and (cycle := dfs(start)):
            return cycle
    return sorted(nodes)   # unreachable when `nodes` really contains a cycle


def chain_metrics(order: list[str], catalog: Mapping[str, Course]) -> tuple[dict[str, int], dict[str, int]]:
    """
    For a topologically ordered plan return (depth, height):
      depth[c]  = number of in-plan prerequisite terms that must come before c
      height[c] = length of the longest prerequisite chain starting at c (c counts as 1)
    max(height) is a lower bound on the number of terms needed.
    """
    in_plan = set(order)
    depth: dict[str, int] = {}
    for c in order:
        depth[c] = max((depth[p] + 1 for p in catalog[c].prereqs if p in in_plan), default=0)

    children: dict[str, list[str]] = {c: [] for c in order}
    for c in order:
        for p in catalog[c].prereqs:
            if p in in_plan:
                children[p].append(c)
    height: dict[str, int] = {}
    for c in reversed(order):
        height[c] = 1 + max((height[k] for k in children[c]), default=0)
    return depth, height


def critical_path(order: list[str], catalog: Mapping[str, Course]) -> list[str]:
    """Longest prerequisite chain among the plan's courses, e.g. ['MATH 1', 'MATH 2', 'MATH 6']."""
    if not order:
        return []
    _, height = chain_metrics(order, catalog)
    position = {c: i for i, c in enumerate(order)}
    children: dict[str, list[str]] = {c: [] for c in order}
    for c in order:
        for p in catalog[c].prereqs:
            if p in children:
                children[p].append(c)
    node = max(order, key=lambda c: (height[c], -position[c]))
    path = [node]
    while children[node]:
        node = max(children[node], key=lambda c: (height[c], -position[c]))
        path.append(node)
    return path
