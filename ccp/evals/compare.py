"""Typed comparison of expected against actual for exact-graded cases.

Plain values compare with ==. A dict with one ``$op`` key is an operator:
  {"$contains": x}    x in actual (substring for str, membership for list)
  {"$subset": {...}}  every listed key of actual equals the given value (recursive)
  {"$any": {...}}     some element of the actual list matches the given expectation
  {"$gte": n} / {"$lte": n}
  {"$len": n}
  {"$startswith": s}
  {"$in": [...]}
Mismatches are returned as strings naming the path, so a failure says exactly what differed.
"""

from __future__ import annotations

from typing import Any

_OPS = {"$contains", "$subset", "$any", "$gte", "$lte", "$len", "$startswith", "$in"}


def compare(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if isinstance(expected, dict) and len(expected) == 1 and next(iter(expected)) in _OPS:
        return _operator(next(iter(expected)), expected[next(iter(expected))], actual, path)
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected mapping, got {type(actual).__name__}"]
        mismatches: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing")
                continue
            mismatches.extend(compare(value, actual[key], f"{path}.{key}"))
        extra = set(actual) - set(expected)
        if extra:
            mismatches.append(f"{path}: unexpected keys {sorted(extra)}")
        return mismatches
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if len(expected) != len(actual):
            return [f"{path}: expected {len(expected)} items, got {len(actual)}"]
        mismatches = []
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            mismatches.extend(compare(e, a, f"{path}[{i}]"))
        return mismatches
    if expected != actual:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


def _operator(op: str, arg: Any, actual: Any, path: str) -> list[str]:
    if op == "$contains":
        if isinstance(actual, str | list | tuple | set | dict) and arg in actual:
            return []
        return [f"{path}: {arg!r} not in {actual!r}"]
    if op == "$subset":
        if not isinstance(actual, dict):
            return [f"{path}: expected mapping, got {type(actual).__name__}"]
        mismatches: list[str] = []
        for key, value in arg.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing")
            else:
                mismatches.extend(compare(value, actual[key], f"{path}.{key}"))
        return mismatches
    if op == "$any":
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        for item in actual:
            if not compare(arg, item, path):
                return []
        return [f"{path}: no element matches {arg!r}"]
    if op == "$gte":
        return (
            []
            if isinstance(actual, int | float) and actual >= arg
            else [f"{path}: {actual!r} < {arg!r}"]
        )
    if op == "$lte":
        return (
            []
            if isinstance(actual, int | float) and actual <= arg
            else [f"{path}: {actual!r} > {arg!r}"]
        )
    if op == "$len":
        try:
            n = len(actual)
        except TypeError:
            return [f"{path}: has no length"]
        return [] if n == arg else [f"{path}: length {n}, expected {arg}"]
    if op == "$startswith":
        return (
            []
            if isinstance(actual, str) and actual.startswith(arg)
            else [f"{path}: {actual!r} does not start with {arg!r}"]
        )
    if op == "$in":
        return [] if actual in arg else [f"{path}: {actual!r} not in {arg!r}"]
    return [f"{path}: unknown operator {op}"]
