from __future__ import annotations

from ccp.evals.compare import compare


def test_plain_equality_and_paths() -> None:
    assert compare({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []
    assert compare({"a": 1}, {"a": 2}) == ["$.a: expected 1, got 2"]
    assert compare({"a": 1}, {"a": 1, "z": 0}) == ["$: unexpected keys ['z']"]
    assert compare([1, 2], [1]) == ["$: expected 2 items, got 1"]


def test_operators() -> None:
    assert compare({"$contains": "ghp_"}, "token ghp_abc") == []
    assert compare({"$contains": "x"}, ["x", "y"]) == []
    assert compare({"$subset": {"a": 1}}, {"a": 1, "b": 2}) == []
    assert compare({"$subset": {"a": 1}}, {"b": 2}) == ["$.a: missing"]
    assert (
        compare({"$any": {"$subset": {"path": "p"}}}, [{"path": "q"}, {"path": "p", "x": 1}]) == []
    )
    assert compare({"$any": {"path": "p"}}, [{"path": "q"}]) == [
        "$: no element matches {'path': 'p'}"
    ]
    assert compare({"$gte": 3}, 3) == [] and compare({"$lte": 3}, 4) != []
    assert compare({"$len": 2}, [1, 2]) == [] and compare({"$len": 2}, "abc") != []
    assert compare({"$startswith": "ab"}, "abc") == []
    assert compare({"$in": ["pass", "partial"]}, "pass") == []
    assert compare({"$in": ["pass"]}, "fail") == ["$: 'fail' not in ['pass']"]


def test_nested_operator_inside_structure() -> None:
    expected = {
        "matches": [
            {
                "$subset": {
                    "payload": {
                        "$subset": {
                            "files": {"$any": {"$subset": {"content": {"$contains": "ghp_"}}}}
                        }
                    }
                }
            }
        ]
    }
    actual = {
        "matches": [
            {"payload": {"files": [{"content": "nope"}, {"content": "ghp_x"}], "other": 1}, "id": 1}
        ]
    }
    assert compare(expected, actual) == []
