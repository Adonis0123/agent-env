"""Habit cells (习惯格, docs/status-schema.md): the five states and the one cell builder every check uses.

`kind` is the habit a cell reports under (rules / skills / hooks / mcp); `expect_kind` (set by `tag`)
names the check that produced it, so readers such as issues.habit_source never have to guess.
"""

STATES = ("synced", "customized", "drift", "missing", "unknown")
SYNCED, CUSTOMIZED, DRIFT, MISSING, UNKNOWN = STATES
HABITS = ("rules", "skills", "hooks", "mcp")
EXPECT_KIND = "expect_kind"


def cell(kind, name, state, detail, fix="", layer="host") -> dict:
    return {"kind": kind, "name": name, "state": state, "layer": layer, "detail": detail, "fix": fix}


def habit_of(exp: dict) -> str:
    """The habit an expectation reports under; anything else counts as rules."""
    h = exp.get("habit")
    return h if h in HABITS else "rules"


def name_of(exp: dict, default: str) -> str:
    """The row name: the expectation's own `name`, else the kind's default."""
    return exp["name"] if isinstance(exp.get("name"), str) and exp["name"] else default


def tag(out: list, kind: str) -> list:
    """Mark every cell with the check that produced it: an expectation kind, or "mcp" / "accounts"."""
    for c in out:
        c[EXPECT_KIND] = kind
    return out
