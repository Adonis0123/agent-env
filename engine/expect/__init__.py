"""Expectation kinds: one module per `kind` of a [[targets.expect]] in the manifest, and the registry
every caller (habits_check, doctor, the remote round trip) goes through instead of branching on kind names.

A kind module owns everything about its kind:

  KIND                  the manifest name, e.g. "broken-links"
  validate(exp, target, label) -> [(message, fix)]
                        its own fields; kind, habit and name are checked here for every kind
  check_local(manifest, target, exp, rules_text) -> [cell]
                        doctor on this machine
  check_remote(manifest, spec, env, target, exp, rules_text)
                        doctor in a remote-exec environment: (sh fragment, parse(output lines) -> cell or [cell])
                        for the one remote round trip, or [cell] when the check runs on this machine instead;
                        raises remote.RemoteError for an `unknown` cell; None when the kind is not checked there
  env_errors(target, remote_spec) -> [(message, fix)]
                        optional: rules between the kind and the target's environment (remote_spec is the
                        environment table of a remote-exec target, else None); called once per target
  GATES_REMOTE = True   optional: a cell that is not synced stops the remote round trip (login)

Every cell handed out through here carries `expect_kind` (cells.tag, docs/status-schema.md), which is how
issues.habit_source knows the kind. A new kind is one module plus one line in REGISTRY.
"""
from cells import HABITS, UNKNOWN, cell, habit_of, name_of, tag

from . import (broken_links, company_skills, composed, contains, delivered, generated, link, login, plugins, portal,
               pulled, secrets, service, token)

REGISTRY = {m.KIND: m for m in (link, contains, generated, delivered, login, service, broken_links, company_skills,
                                portal, secrets, plugins, token, pulled, composed)}
KINDS = tuple(REGISTRY)


def get(kind):
    """The module of a registered kind, else None."""
    return REGISTRY.get(kind) if isinstance(kind, str) else None


def of(exp):
    """The module for one [[targets.expect]] table, else None."""
    return get(exp.get("kind")) if isinstance(exp, dict) else None


def validate(exp, target: dict, label: str) -> list:
    """(message, fix) pairs for one expectation: kind, habit, the kind's own fields, name."""
    k = of(exp)
    if k is None:
        kind = exp.get("kind") if isinstance(exp, dict) else exp
        return [(f"{label} 的 kind={kind!r} 无效", f"kind 写成 {list(KINDS)} 之一")]
    errs = []
    if exp.get("habit") not in HABITS:
        errs.append((f"{label} 的 habit={exp.get('habit')!r} 无效", f"habit 写成 {list(HABITS)} 之一"))
    errs += k.validate(exp, target, label)
    if "name" in exp and (not isinstance(exp["name"], str) or not exp["name"]):
        errs.append((f"{label} 的 name 不是非空字符串", "写成矩阵里的行名，或删掉用默认名"))
    return errs


def env_errors(target: dict, remote_spec) -> list:
    """(message, fix) pairs tying the target's kinds to its environment, once per kind it uses."""
    exps = target.get("expect") if isinstance(target.get("expect"), list) else []
    errs = []
    for k in REGISTRY.values():
        hook = getattr(k, "env_errors", None)
        if hook and any(isinstance(e, dict) and e.get("kind") == k.KIND for e in exps):
            errs += hook(target, remote_spec)
    return errs


def check(manifest: dict, target: dict, exp, rules_text) -> list:
    """doctor on this machine: the cells of one expectation, each tagged with its kind."""
    k = of(exp)
    if k is None:
        return [cell("rules", "expect", UNKNOWN, f"无法识别的期望：{exp!r}（先跑 bin/agent-env check）")]
    try:
        out = k.check_local(manifest, target, exp, rules_text)
    except Exception as exc:  # never crash the whole doctor on one cell
        out = [cell(habit_of(exp), name_of(exp, exp.get("kind", "expect")), UNKNOWN,
                    f"检查出错：{type(exc).__name__}: {exc}")]
    return tag(out, k.KIND)
