"""Where the engine keeps local state; every Python path to it comes from here.

- state_base(): $XDG_STATE_HOME, or ~/.local/state.
- state_dir(): the engine's own state (snapshots, outbox, eval results, deliveries, promotions, plugin builds).

Stdlib only: a personal layer's own scripts may import it outside the engine.
"""
import os
from pathlib import Path


def state_base() -> Path:
    """$XDG_STATE_HOME, or ~/.local/state when it is unset or empty (as bash's ${VAR:-...})."""
    return Path(os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"))


def state_dir() -> Path:
    return state_base() / "agent-env"

