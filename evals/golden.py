"""The golden set (golden.yaml): loading it, picking cases, and the world each case starts from."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

import yaml

from . import score

GOLDEN = Path(__file__).with_name("golden.yaml")
CATEGORIES = ("tool", "memory", "plan", "safety", "abstain")
WORLD_KEYS = ("emails", "events", "tasks", "facts")


@lru_cache(maxsize=4)
def _read(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load(path: Path | str = GOLDEN) -> dict:
    """A fresh copy every call: runs may not change each other's data."""
    return copy.deepcopy(_read(str(path)))


def select(golden: dict, subset: str | None = None, categories: list[str] | None = None,
           ids: list[str] | None = None) -> list[dict]:
    """Cases in file order, narrowed by subset (``ci``: the cases marked ``ci: true``), category and id."""
    out = []
    for c in golden["cases"]:
        if subset and subset != "full" and not c.get(subset):
            continue
        if categories and c["category"] not in categories:
            continue
        if ids and c["id"] not in ids:
            continue
        out.append(c)
    return out


def interleave(cases: list[dict]) -> list[dict]:
    """Round-robin over the categories, so a run cut short by its time budget still covers each of them."""
    queues = {}
    for c in cases:
        queues.setdefault(c["category"], []).append(c)
    out = []
    while any(queues.values()):
        for q in queues.values():
            if q:
                out.append(q.pop(0))
    return out


def toolset(golden: dict, subset: str | None) -> list[str]:
    sets = golden["toolsets"]
    return list(sets.get(subset or "full") or sets["full"])


def world(golden: dict, case: dict) -> dict:
    """What a case starts from: the shared world plus the case's own emails, events, tasks and facts (with
    ``fresh: true``, only its own)."""
    base = {} if case.get("fresh") else copy.deepcopy(golden.get("world") or {})
    for k in WORLD_KEYS:
        base[k] = list(base.get(k) or []) + list(case.get(k) or [])
    return base


def bench_cases(golden: dict | None = None) -> list[dict]:
    """The tool-choice cases the model test (Setup → Brain → Test my models) also scores: ``bench: true``."""
    golden = golden or load()
    return [c for c in golden["cases"] if c.get("bench") and c["category"] == "tool"]


def bench_ok(case: dict, calls: list[dict]) -> bool:
    """Did the model's calls do the case's job (right tool, right key arguments)?"""
    checks = score.check_calls(calls, case["expect"])
    return bool(checks) and all(checks.values())
