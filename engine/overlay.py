"""Where the personal layer is.

The engine ships without one. A personal environment repository holds it (manifest/habits.toml, repos.json,
profiles/, its own skills) and runs the engine either from a checkout of this repository or vendored inside it
(for example as a git subtree at `vendor/agent-env/`). The personal repository is the nearest directory that
has manifest/habits.toml, starting at the engine's own root and walking up at most MAX_UP levels; without one
the engine's root is used, and the manifest has to come from --manifest or AGENT_ENV_MANIFEST.

Stdlib only. No user-specific path lives in this file.
"""
from pathlib import Path

MANIFEST = Path("manifest") / "habits.toml"
REPOS = "repos.json"
MAX_UP = 3
ENGINE_ROOT = Path(__file__).resolve().parent.parent


def overlay_root(start: Path = ENGINE_ROOT) -> Path:
    """The personal repository the engine runs for (see the module docstring)."""
    for d in [start, *start.parents][: MAX_UP + 1]:
        if (d / MANIFEST).is_file():
            return d
    return start
