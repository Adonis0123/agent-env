"""kind = "token": the read token a cloud-hosted target uses for private plugins. The manifest records only
the token's name and expiry date (never its value); the cell warns 7 days before and fails once expired.
"""
import plugins as source
from cells import cell, habit_of, name_of

KIND = "token"


def validate(exp: dict, target: dict, label: str) -> list:
    if "plugins" not in target:
        return [(f"{label} 是 token，但落点没有 plugins 列表", "给落点写 plugins = [...]，或删掉这条期望")]
    return []


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    state, detail, fix, warn = source.token_state(target, manifest)
    c = cell(habit_of(exp), name_of(exp, "插件 token"), state, detail, fix)
    if warn:
        c["warn"] = True  # still usable; doctor prints the fix as a reminder
    return [c]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    return check_local(manifest, target, exp, rules_text)
