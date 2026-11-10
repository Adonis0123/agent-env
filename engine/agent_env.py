#!/usr/bin/env python3
"""agent-env: one entry for the habit engine.

  check      validate the manifest (habits_check)
  render     render / check / write a generated rule file (render)
  doctor     check every expectation of every target in this environment
  status     the same result as schema-1 JSON; --all merges status/<env> branches;
             --commit stores it on the local branch status/<env> (git plumbing)
  rollback   put the latest snapshot back, after snapshotting the current file
  deliver    print what a cloud-hosted target should receive (also written to
             outbox/<target>.md), or --record it after the user stored it;
             for a remote-exec target: print the remote steps, --yes runs them
  eval       behaviour spot checks per target (evals); results go to eval/<env>.json
  skills     install: copy [sources] company_skills into a target (dry run; --yes backs up and
             installs); rollback: restore the latest backup (company_skills)
  promote    scan: read-only promotion candidates (JSON); propose: Markdown draft
  plugin     build: generate plugin directories (plugins); publish: build → scan → commit (into a
             working clone of the plugin's repo) → push with a step ledger; push is a dry run unless
             --yes, and never goes to a public repo for a personal/company plugin; pointer: print or
             --record a cloud-hosted target's pointer rule; vendor-diff: a vendored third-party skill's
             local copy against upstream HEAD (network), --write-license saves the upstream license

Paths come from the manifest (`~` expanded at run time); no user-specific
path, machine name or account lives in this file.
Requires Python 3.11+ (tomllib).
"""
import sys

if sys.version_info < (3, 11):
    print("agent-env 需要 Python 3.11+（依赖 tomllib），当前是 %d.%d。"
          "修复：装 Python 3.11 以上，设 AGENT_ENV_PYTHON=<新 python3 的路径> 后重跑 bin/agent-env；"
          "或改用 `uv run --python 3.12 python3 agent_env.py ...` 运行。"
          % sys.version_info[:2], file=sys.stderr)
    sys.exit(2)

import argparse  # noqa: E402
import concurrent.futures  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import unicodedata  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE_DIR))
import company_skills  # noqa: E402
import evals  # noqa: E402
import expect  # noqa: E402
import habits_check  # noqa: E402
import issues  # noqa: E402
import mcp  # noqa: E402
import plugins  # noqa: E402
import plugins_view  # noqa: E402
import promote  # noqa: E402
import pull  # noqa: E402
import remote  # noqa: E402
import render  # noqa: E402
from state import state_dir  # noqa: E402
from cells import (CUSTOMIZED, DRIFT, HABITS, MISSING, STATES, SYNCED, UNKNOWN,  # noqa: E402
                   cell, tag)
import overlay  # noqa: E402
from habits import find_target, load_manifest, normalize, read_rules  # noqa: E402

# the personal repository: this checkout, or the one that vendors the engine (engine/overlay.py)
REPO_DIR = overlay.overlay_root()
DEFAULT_MANIFEST = REPO_DIR / overlay.MANIFEST
SCHEMA = 1
KINDS = HABITS
STATE_LABELS = {SYNCED: "已同步", CUSTOMIZED: "已定制", DRIFT: "漂移", MISSING: "缺失", UNKNOWN: "未知"}
STATUS_FILE = "status.json"
BRANCH_PREFIX = "status/"
ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.+-]*[0-9A-Za-z])?")
VERSION_TIMEOUT = 15
ACCOUNTS_TIMEOUT = 120


class AgentEnvError(Exception):
    """A failure whose message already says how to fix it."""


# ── small helpers ─────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tilde(path) -> str:
    return render._tilde(path)


def manifest_arg(value=None) -> Path:
    return Path(value or os.environ.get("AGENT_ENV_MANIFEST") or DEFAULT_MANIFEST)


def repo_arg(value=None) -> Path:
    return Path(value or os.environ.get("AGENT_ENV_REPO") or REPO_DIR)


def _load(manifest_path) -> dict:
    try:
        return load_manifest(manifest_path)
    except Exception as exc:  # missing file, bad TOML, bad encoding
        raise AgentEnvError(f"清单读不了：{manifest_path}（{exc}）。修复：确认路径存在且是合法的 UTF-8 TOML，"
                            f"或用 --manifest / AGENT_ENV_MANIFEST 指定；{habits_check.NO_MANIFEST_HINT}")


# ── environment ───────────────────────────────────────────────────────────

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _env_matches(spec) -> bool:
    if not isinstance(spec, dict) or not isinstance(spec.get("platform"), str):
        return False
    if not sys.platform.startswith(spec["platform"]):
        return False
    if "wsl" in spec and bool(spec["wsl"]) != _is_wsl():
        return False
    return True


def known_envs(manifest: dict) -> list:
    """Every environment name --env may take: declared in [environments] or used as `env` by a [[targets]] entry
    (a target may belong to an environment that is deliberately not declared), sorted."""
    envs, targets = manifest.get("environments", {}), manifest.get("targets", [])
    names = set(envs) if isinstance(envs, dict) else set()
    for t in targets if isinstance(targets, list) else []:
        if isinstance(t, dict) and isinstance(t.get("env"), str) and t["env"]:
            names.add(t["env"])
    return sorted(names)


def detect_env(manifest: dict, override=None) -> str:
    """The environment name: --env (a name the manifest knows, see known_envs), then the first
    [environments.<name>] matching this machine."""
    envs = manifest.get("environments", {})
    known = known_envs(manifest)
    if override:
        name = override
        if name not in known:  # exact match only: a status branch and a target's `env` are case-sensitive
            if not known:
                raise AgentEnvError(f"清单里没有环境「{name}」：清单还没有登记任何环境，也没有落点。"
                                    f"修复：在清单写 [environments.{name}] platform = \"{sys.platform}\"，"
                                    f"再给它加 [[targets]]（env = \"{name}\"）")
            near = [n for n in known if n.lower() == name.lower()]
            hint = f"是不是「{near[0]}」（环境名区分大小写）？" if near else ""
            raise AgentEnvError(f"清单里没有环境「{name}」。{hint}清单里有：{'、'.join(known)}。"
                                f"修复：把 --env 改成其中一个；要接新机器就先在清单写 [environments.{name}]")
    else:
        name = next((n for n, spec in envs.items() if _env_matches(spec)), None) if isinstance(envs, dict) else None
        if name is None:
            use = f"加 --env <环境名>（清单里有：{'、'.join(known)}），或" if known else ""
            raise AgentEnvError(f"清单里没有和本机（{sys.platform}）匹配的环境。"
                                f"修复：{use}在清单写 [environments.<名字>] platform = \"{sys.platform}\""
                                + ("" if known else "，再给它加 [[targets]]（env 写这个名字）"))
    if not ENV_NAME_RE.match(name):
        raise AgentEnvError(f"环境名「{name}」含不能用于分支名的字符。修复：只用字母、数字、点、下划线和连字符")
    return name


def env_targets(manifest: dict, env: str) -> list:
    """Targets that live in `env`: same env field, or an id starting with `<env>/`
    (a cloud-hosted target is driven from the environment that delivers to it)."""
    out = []
    for t in manifest.get("targets", []):
        if isinstance(t, dict) and (t.get("env") == env or str(t.get("id", "")).startswith(env + "/")):
            out.append(t)
    return out


def env_spec(manifest: dict, env: str) -> dict:
    envs = manifest.get("environments", {})
    spec = envs.get(env) if isinstance(envs, dict) else None
    return spec if isinstance(spec, dict) else {}


# ── expectations ──────────────────────────────────────────────────────────
# Each kind of [[targets.expect]] lives in its own module under engine/expect/ (registry: expect.REGISTRY).

check_broken_links = expect.broken_links.scan  # one broken-links cell on this machine


def delivery_path(target_id: str) -> Path:
    return remote.delivery_path(target_id)


def delivery_text(manifest: dict, target_id: str, rules_text) -> str:
    try:
        return expect.delivered.text(manifest, target_id, rules_text)
    except render.RenderError as exc:
        raise AgentEnvError(str(exc))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── host version, accounts, check ─────────────────────────────────────────

def probe_version(cmd) -> str | None:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=VERSION_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = VERSION_RE.search(proc.stdout or "") or VERSION_RE.search(proc.stderr or "")
    return m.group(0) if m else None


def host_version(target: dict):
    spec = target.get("host_version")
    if not isinstance(spec, dict):
        return None
    recorded = spec.get("recorded")
    current = probe_version(spec.get("cmd"))
    return {"recorded": recorded, "current": current,
            "changed": current is not None and current != recorded}


def accounts_cell(spec: dict) -> dict:
    """One cell summarising the account directories, delegated to sideby."""
    name = "账号目录（sideby）"
    if spec.get("accounts") != "sideby":
        return cell("rules", name, UNKNOWN, "清单没有为本环境登记账号目录工具")
    exe = shutil.which("sideby")
    if not exe:
        return cell("rules", name, UNKNOWN, "本机没有 sideby，账号目录无法核对")
    try:
        proc = subprocess.run([exe, "doctor", "--json"], capture_output=True, text=True,
                              timeout=ACCOUNTS_TIMEOUT, stdin=subprocess.DEVNULL)
        data = json.loads(proc.stdout)
        accounts = data["accounts"]
        status = data.get("status")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        return cell("rules", name, UNKNOWN, f"sideby doctor --json 没有给出可读结果（{type(exc).__name__}）")
    findings = [f for a in accounts if isinstance(a, dict) for f in (a.get("findings") or [])]
    findings += [f for f in (data.get("general") or [])]
    levels = [str(f.get("level", "")).lower() for f in findings if isinstance(f, dict)]
    fails = sum(lv in ("fail", "error") for lv in levels)
    warns = sum(lv == "warn" for lv in levels)
    base = f"{len(accounts)} 个账号目录"
    if fails or proc.returncode != 0 or status in ("fail", "error"):
        return cell("rules", name, DRIFT, f"{base}，sideby 报 {fails} 项失败、{warns} 项警告",
                    "sideby doctor --fix")
    if status in ("ok", "warn"):
        extra = f"，另有 {warns} 项警告（sideby doctor 查看）" if warns else ""
        return cell("rules", name, SYNCED, f"{base}与主目录一致{extra}")
    return cell("rules", name, UNKNOWN, f"sideby 返回了未知状态 {status!r}")


def check_counts(manifest_path) -> dict:
    rep = habits_check.run_checks(manifest_path)
    return {lv.lower(): sum(r["level"] == lv for r in rep.results)
            for lv in (habits_check.PASS, habits_check.WARN, habits_check.FAIL)}


def git(repo: Path, *args, input=None, check=True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AgentEnvError(f"git {' '.join(args)} 失败：{(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def engine_rev(repo: Path) -> str:
    try:
        return git(repo, "rev-parse", "--short", "HEAD").strip()
    except (AgentEnvError, OSError):
        return ""


# ── doctor / status ───────────────────────────────────────────────────────

def _rules_text(manifest: dict):
    return read_rules(manifest)


def target_status(manifest: dict, target: dict, rules_text) -> dict:
    habits = []
    for exp in target.get("expect", []) or []:
        habits += expect.check(manifest, target, exp, rules_text)
    try:
        habits += tag(mcp.compare(manifest, target), "mcp")
    except Exception as exc:
        habits += tag([cell("mcp", "mcp_config", UNKNOWN, f"MCP 比对出错：{type(exc).__name__}: {exc}")], "mcp")
    return {"id": target["id"], "host": target.get("host", ""), "env": target.get("env", ""),
            "roles": list(target.get("roles", [])), "verified": target.get("verified") is True,
            "host_version": host_version(target), "habits": habits}


def doctor(manifest_path, env=None, repo=None) -> dict:
    """The live status of one environment (schema 1, `source: live`)."""
    manifest = _load(manifest_path)
    env = detect_env(manifest, env)
    rules_text = _rules_text(manifest)
    spec = env_spec(manifest, env)
    if spec.get("transport") == render.REMOTE_EXEC:
        return _remote_doctor(manifest_path, manifest, env, spec, rules_text, repo)
    # Probes (host --version, sideby doctor) wait on subprocesses: run them side by side so the
    # worst case is the slowest single probe (ACCOUNTS_TIMEOUT), not their sum. Order is kept.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(target_status, manifest, t, rules_text) for t in env_targets(manifest, env)]
        accounts = pool.submit(accounts_cell, spec) if "accounts" in spec else None
        targets = [f.result() for f in futures]
        if accounts is not None:
            targets.append({"id": f"{env}/accounts", "host": str(spec["accounts"]), "env": env, "roles": [],
                            "verified": True, "host_version": None,
                            "habits": tag([accounts.result()], "accounts")})
    summary = {s: 0 for s in STATES}
    for t in targets:
        for h in t["habits"]:
            summary[h["state"]] += 1
    status = {"schema": SCHEMA, "env": env, "source": "live", "generated_at": iso(now_utc()),
              "engine_rev": engine_rev(repo_arg(repo)), "summary": summary,
              "check": check_counts(manifest_path), "targets": targets,
              "eval": evals.status_field(env)}
    if spec.get("transport") == plugins.CLOUD_HOSTED:
        # the checks read this machine's state (builds, pointer record, token date): say whose state it is
        status["checked_from"] = _local_env(manifest, spec)
    return issues.annotate_eval(status, manifest)


def _local_env(manifest: dict, spec: dict) -> str:
    """The environment this machine is, for a cloud-hosted status; its driven_from when it cannot be told."""
    try:
        return detect_env(manifest, None)
    except AgentEnvError:
        return str(spec.get("driven_from") or "")


def _remote_doctor(manifest_path, manifest, env, spec, rules_text, repo) -> dict:
    """A remote-exec environment: every check runs on the remote host in one round trip from here."""
    targets, error = remote.doctor_targets(manifest, env, spec, env_targets(manifest, env), rules_text)
    summary = {s: 0 for s in STATES}
    for t in targets:
        for h in t["habits"]:
            summary[h["state"]] += 1
    status = {"schema": SCHEMA, "env": env, "source": "live", "generated_at": iso(now_utc()),
              "engine_rev": engine_rev(repo_arg(repo)), "summary": summary,
              "check": check_counts(manifest_path), "targets": targets,
              "eval": evals.status_field(env)}
    if error is not None:
        status["error"] = error
    return issues.annotate_eval(status, manifest)


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _width(s))


def format_doctor(status: dict) -> str:
    lines = [f"环境 {status['env']} 体检（{status['generated_at']}，引擎 {status['engine_rev'] or '?'}）", ""]
    if status.get("error"):
        lines += [f"环境出错：{status['error']['message']}", f"修复：{status['error']['fix']}", ""]
    for t in status["targets"]:
        hv = t.get("host_version")
        head = f"落点 {t['id']}"
        if hv:
            cur = hv["current"] or "探测不到"
            head += f"（登记版本 {hv['recorded']}，当前 {cur}）"
            if hv["changed"]:
                head += "  ⚠ 宿主版本变了，重新核对分类表（skills/agent-env/SKILL.md 第 3 节）后更新清单 host_version.recorded"
        lines.append(head)
        if not t["habits"]:
            lines += ["  （没有登记期望）", ""]
            continue
        rows = [(STATE_LABELS[h["state"]], h["kind"], h["name"], h["detail"]) for h in t["habits"]]
        widths = [max(_width(r[i]) for r in rows + [("状态", "类别", "名称", "")]) for i in range(3)]
        lines.append("  " + "  ".join(_pad(x, w) for x, w in zip(("状态", "类别", "名称"), widths)) + "  说明")
        for h, r in zip(t["habits"], rows):
            lines.append("  " + "  ".join(_pad(x, w) for x, w in zip(r[:3], widths)) + "  " + r[3])
            if h["state"] in (DRIFT, MISSING):
                lines.append("  " + " " * (sum(widths) + 6) + "修复：" + (h["fix"] or "（缺少修法，请补）"))
            elif h.get("warn") and h.get("fix"):
                lines.append("  " + " " * (sum(widths) + 6) + "提醒：" + h["fix"])
        lines.append("")
    ignored = (status.get("eval") or {}).get("ignored_targets") or []
    if ignored:
        lines += [f"抽测记录里有 {len(ignored)} 个落点已不在清单的 {status['env']} 环境：{'、'.join(ignored)}。"
                  f"这些记录已忽略，不进体检和面板；记录文件没动（{_tilde(evals.eval_path(status['env']))}）", ""]
    s, c = status["summary"], status["check"]
    lines.append("合计：" + "，".join(f"{STATE_LABELS[k]} {s[k]}" for k in STATES))
    lines.append(f"清单检查：PASS {c['pass']}，WARN {c['warn']}，FAIL {c['fail']}"
                 + ("（FAIL 详情：bin/agent-env check）" if c["fail"] else ""))
    return "\n".join(lines) + "\n"


def has_problems(status: dict) -> bool:
    return bool(status.get("error")) or status["summary"][DRIFT] > 0 or status["summary"][MISSING] > 0


def _branch_ref(env: str) -> str:
    return f"refs/heads/{BRANCH_PREFIX}{env}"


def read_branches(repo: Path, manifest: dict, live_env: str, fetch=False, remote="origin", skip=()):
    """(environment statuses, errors) from local and `<remote>/` status branches; `skip` names
    environments already checked live here."""
    errors = []
    if fetch:
        try:
            git(repo, "fetch", remote, f"+refs/heads/{BRANCH_PREFIX}*:refs/remotes/{remote}/{BRANCH_PREFIX}*")
        except (AgentEnvError, OSError) as exc:
            errors.append({"env": "", "message": f"fetch 状态分支失败：{exc}",
                           "fix": f"确认 {remote} 可访问后重跑 bin/agent-env status --json --all --fetch"})
    try:
        out = git(repo, "for-each-ref", "--format=%(refname)",
                  f"refs/heads/{BRANCH_PREFIX}", f"refs/remotes/{remote}/{BRANCH_PREFIX}")
    except (AgentEnvError, OSError) as exc:
        return [], errors + [{"env": "", "message": f"读不了状态分支列表：{exc}", "fix": "确认仓库路径正确（--repo）"}]
    refs = {}
    for ref in out.split():
        env = ref.split(f"/{BRANCH_PREFIX}", 1)[1]
        refs.setdefault(env, []).append(ref)
    declared = set(manifest.get("environments", {}) or {}) if isinstance(manifest.get("environments"), dict) else set()
    envs = []
    for env in sorted((set(refs) | declared) - {live_env} - set(skip)):
        found, problems = [], []
        for ref in refs.get(env, []):
            short = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
            try:
                data = json.loads(git(repo, "show", f"{ref}:{STATUS_FILE}"))
                if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("env") != env:
                    raise ValueError(f"不是 schema {SCHEMA} 的 {env} 环境状态")
                found.append(data)
            except (AgentEnvError, ValueError, OSError) as exc:
                problems.append(f"{short}：{exc}")
        if found:
            best = max(found, key=lambda d: str(d.get("generated_at", "")))
            envs.append(dict(best, source="branch"))
        else:
            msg = "；".join(problems) if problems else f"读不到 {BRANCH_PREFIX}{env} 分支"
            spec = env_spec(manifest, env)
            if spec.get("transport") == plugins.CLOUD_HOSTED:
                where = spec.get("driven_from") or "存放插件生成结果的那台机器"
                fix = f"在 {where} 上跑 bin/agent-env status --env {env} --commit --push（云端托管落点只读那台机器的状态）"
            elif spec.get("transport") == render.REMOTE_EXEC:
                fix = f"在本机跑 bin/agent-env status --env {env} --commit（远程检查从本机发起）"
            else:
                fix = (f"在 {env} 环境跑 bin/agent-env status --commit --push；"
                       f"本机再跑 bin/agent-env status --json --all --fetch")
            errors.append({"env": env, "message": msg, "fix": fix})
    return envs, errors


def relations(manifest: dict, manifest_path, environments: list) -> dict:
    """Read-only data for the panel's relation graph: where each habit comes from and which agents each
    declared environment has (also those that never reported). Each habit cell of `environments` gets an
    `origin` key in place (a key of `sources`, or None for a fact of that machine itself)."""
    raw = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
    sources = {k: _tilde(os.path.normpath(raw[k])) for k in issues.ORIGIN_SOURCES if isinstance(raw.get(k), str)}
    if isinstance(raw.get("rules_src"), str):  # stage 4: the rules are edited there; [sources] rules is generated
        sources["rules"] = _tilde(os.path.normpath(raw["rules_src"]))
    registries = expect.portal.registries(manifest)
    if registries:
        sources["portal"] = "；".join(registries)
    sources["manifest"] = _tilde(Path(manifest_path).resolve())
    for e in environments:
        for t in e.get("targets", []) or []:
            for c in t.get("habits", []) or []:
                if isinstance(c, dict):
                    c["origin"] = issues.cell_origin(manifest, t, c)
    declared = list(manifest.get("environments", {}) or {}) if isinstance(manifest.get("environments"), dict) else []
    hosts = issues._table(manifest, "host_names")
    targets = []
    # a source whose path differs by machine (each machine's portal reads its own tool list): the path per machine
    env_sources = {}
    for env in declared:
        for t in env_targets(manifest, env):
            for exp in t.get("expect", []) or []:
                if isinstance(exp, dict) and exp.get("kind") == "portal" and isinstance(exp.get("registry"), str):
                    env_sources.setdefault(env, {}).setdefault("portal", _tilde(exp["registry"]))
            host = str(t.get("host", ""))
            name = t["display_name"] if isinstance(t.get("display_name"), str) and t["display_name"] else hosts.get(host, host)
            targets.append({"id": str(t.get("id", "")), "env": env, "host": host, "name": name,
                            "roles": [str(r) for r in t.get("roles", []) or []]})
    return {"sources": sources, "env_sources": env_sources,
            "env_names": {env: issues.env_name(manifest, env) for env in declared}, "targets": targets}


def cloud_envs(manifest: dict, live_env: str) -> list:
    """Cloud-hosted environments whose checks this machine runs live: they only read local state (plugin
    builds, pointer records, token dates), kept on their `driven_from` machine."""
    envs = manifest.get("environments") if isinstance(manifest.get("environments"), dict) else {}
    return [n for n, spec in envs.items() if isinstance(spec, dict) and spec.get("transport") == plugins.CLOUD_HOSTED
            and spec.get("driven_from", live_env) == live_env and n != live_env]


def all_status(manifest_path, env=None, repo=None, fetch=False, live=None) -> dict:
    manifest = _load(manifest_path)
    live = live or doctor(manifest_path, env, repo)
    cloud = [doctor(manifest_path, c, repo) for c in cloud_envs(manifest, live["env"])]
    for c in cloud:
        c["checked_from"] = live["env"]
        c["source"] = "live"
    branches, errors = read_branches(repo_arg(repo), manifest, live["env"], fetch, skip=[c["env"] for c in cloud])
    branches = cloud + branches
    for e in [live] + branches:  # a remote environment that could not be checked
        if isinstance(e.get("error"), dict):
            errors.append({"env": e["env"], "message": str(e["error"].get("message", "")),
                           "fix": str(e["error"].get("fix", ""))})
    # a copy: the live status may also be what --commit stores, and `origin` belongs to this view only
    envs = [issues.annotate_eval(copy.deepcopy(e), manifest) for e in [live] + branches]
    found = issues.build(envs, errors, manifest)
    for e in envs:
        e["issues"] = [i for i in found if i["env"] == e["env"]]
    return {"schema": SCHEMA, "generated_at": iso(now_utc()),
            "environments": envs, "errors": errors, "issues": found, "glossary": issues.glossary(),
            "relations": relations(manifest, manifest_path, envs),
            "plugins_view": plugins_view.view(manifest, manifest_path, envs, _rules_text(manifest))}


def with_issues(manifest_path, status: dict) -> dict:
    """One environment status plus its own `issues` (schema 1, additive)."""
    status["issues"] = issues.build([status], [], _load(manifest_path))
    return status


def commit_status(repo: Path, status: dict, push=False, remote="origin") -> str:
    """Store status.json on refs/heads/status/<env> with plumbing only:
    the work tree, the index and the current branch are not touched."""
    env = status["env"]
    ref = _branch_ref(env)
    data = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    blob = git(repo, "hash-object", "-w", "--stdin", input=data).strip()
    tree = git(repo, "mktree", input=f"100644 blob {blob}\t{STATUS_FILE}\n").strip()
    parent = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip()
    s = status["summary"]
    msg = (f"status({env}): {status['generated_at']} "
           + " ".join(f"{k}={s[k]}" for k in STATES))
    args = ["commit-tree", tree, "-m", msg] + (["-p", parent] if parent else [])
    try:
        commit = git(repo, *args).strip()
    except AgentEnvError as exc:
        raise AgentEnvError(f"{exc}。修复：git config user.name / user.email 设好提交身份后重试")
    git(repo, "update-ref", "-m", f"agent-env status --commit ({env})", ref, commit, parent or "0" * 40)
    if push:
        git(repo, "push", remote, f"{ref}:{ref}")
    return commit


# ── rollback ──────────────────────────────────────────────────────────────

def _stamp_key(name: str):
    base, _, n = name.partition("-")
    return base, int(n) if n.isdigit() else 0


def snapshot_entries(manifest=None):
    """[(stamp, target id, snapshot file, original path or None)], oldest first."""
    root = state_dir() / "snapshots"
    if not root.is_dir():
        return []
    entries = []
    for stamp_dir in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: _stamp_key(d.name)):
        for meta_or_file in sorted(stamp_dir.rglob("*")):
            if not meta_or_file.is_file() or meta_or_file.name == render.SNAPSHOT_META:
                continue
            target_id = meta_or_file.parent.relative_to(stamp_dir).as_posix()
            original = None
            meta = meta_or_file.parent / render.SNAPSHOT_META
            if meta.is_file():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    files = data.get("files")
                    # Multi-output snapshots map each copy to its path; older ones name a single path.
                    original = Path(files[meta_or_file.name] if isinstance(files, dict) else data["path"])
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    original = None
            if original is None and manifest is not None:
                try:
                    out = render.output_path(find_target(manifest, target_id))
                    original = out if out.name == meta_or_file.name else None
                except (KeyError, render.RenderError):
                    original = None
            entries.append((stamp_dir.name, target_id, meta_or_file, original))
    return entries


def _read_exact(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def rollback(manifest_path, target_id=None, out=sys.stdout) -> int:
    try:
        manifest = load_manifest(manifest_path)
    except Exception:
        manifest = None
    entries = [e for e in snapshot_entries(manifest) if target_id is None or e[1] == target_id]
    if not entries:
        which = f"落点「{target_id}」" if target_id else "任何落点"
        raise AgentEnvError(f"{_tilde(state_dir() / 'snapshots')} 里没有{which}的快照。"
                            f"修复：确认 --target 写对；快照只在 render --write 或 rollback 时产生")
    latest = entries[-1][0]
    todo = [e for e in entries if e[0] == latest]
    for _, tid, snap, original in todo:
        if original is None:
            raise AgentEnvError(f"不知道快照 {_tilde(snap)} 原来在哪。修复：手动把它复制回原位置")
        if original.is_symlink():
            raise AgentEnvError(f"{_tilde(original)} 现在是符号链接，恢复会把链接换成普通文件。修复：先确认链接指向，再手动恢复")
    befores = {}
    for tid in dict.fromkeys(e[1] for e in todo):  # one snapshot per target keeps the rollback reversible
        originals = [e[3] for e in todo if e[1] == tid and e[3].exists()]
        befores.update(zip(originals, render.snapshot_files(originals, tid)))
    for _, tid, snap, original in todo:
        before = befores.get(original)
        render.atomic_write(original, _read_exact(snap))
        undo = (f"恢复前的文件已快照到 {_tilde(before)}，再跑一次 bin/agent-env rollback --target {tid} 可撤销"
                if before else "原先没有该文件")
        print(f"已恢复 {_tilde(original)} ← 快照 {latest}；{undo}", file=out)
    return 0


# ── deliver ───────────────────────────────────────────────────────────────

def outbox_path(target_id: str) -> Path:
    return state_dir() / "outbox" / (target_id.replace("/", "-") + ".md")


def _paste_flow(target_id: str, text: str, record: bool, out, err, command: str) -> int:
    """Print what the user stores by hand (and keep it in the outbox), or record that it was stored."""
    box = outbox_path(target_id)
    if not record:
        render.atomic_write(box, text)
        out.write(text)
        print(f"已写入待投递内容 {_tilde(box)}（{len(text.encode('utf-8'))} B，哈希 {sha256(text)[:12]}）；"
              f"在 app 里确认存入后运行 bin/agent-env {command} --target {target_id} --record", file=err)
        return 0
    if box.is_file() and _read_exact(box) != text:
        raise AgentEnvError(f"{_tilde(box)} 和当前要投递的内容不一致（规则源在输出之后变了），"
                            f"记录下来会和云端实际内容对不上。修复：重跑 bin/agent-env {command} --target {target_id}，"
                            f"把新内容投递并确认后再 --record")
    path = delivery_path(target_id)
    rec = {"target": target_id, "sha256": sha256(text), "bytes": len(text.encode("utf-8")),
           "delivered_at": iso(now_utc())}
    render.atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    print(f"已记录投递：{target_id} 内容哈希 {rec['sha256'][:12]}（{_tilde(path)}）", file=out)
    return 0


def _target(manifest: dict, target_id: str) -> dict:
    try:
        return find_target(manifest, target_id)
    except KeyError:
        raise AgentEnvError(f"清单里没有落点「{target_id}」。修复：--target 改成清单里的 id")


def deliver(manifest_path, target_id, record=False, out=sys.stdout, err=sys.stderr, yes=False) -> int:
    manifest = _load(manifest_path)
    target = _target(manifest, target_id)
    if "plugins" in target and "pull" in target:
        # a remote host that pulls its plugins itself: deliver installs (or updates) the puller
        spec = render.remote_spec(manifest, target)
        if spec is None or record:
            raise AgentEnvError(f"落点「{target_id}」自己拉取插件，投递就是在远程装拉取脚本。"
                                f"修复：去掉 --record，用 --dry-run 看步骤，用户确认后加 --yes")
        plugin_gate(manifest_path)
        try:
            return pull.deliver(manifest, spec, target, _rules_text(manifest), yes, out, err)
        except (pull.PullError, remote.RemoteError) as exc:
            raise AgentEnvError(str(exc))
    if "plugins" in target:
        # a plugin target has no composed rules to deliver: its only stored text is the pointer rule,
        # and that has one entry point (with its own checks)
        raise AgentEnvError(f"落点「{target_id}」用插件，没有整份规则可投递。"
                            f"修复：指路规则用 bin/agent-env plugin pointer --target {target_id}"
                            f"{' --record' if record else ''}，插件内容用 bin/agent-env plugin build --all")
    spec = render.remote_spec(manifest, target)
    if spec is not None:
        if record:
            raise AgentEnvError(f"落点「{target_id}」是远程落点，投递成功时自动记录。"
                                f"修复：去掉 --record，用 --dry-run 看步骤，用户确认后加 --yes")
        text = delivery_text(manifest, target_id, _rules_text(manifest))
        try:
            return remote.deliver(manifest, spec, target, text, yes, out, err)
        except remote.RemoteError as exc:
            raise AgentEnvError(str(exc))
    if yes:
        raise AgentEnvError(f"--yes 只用于远程落点；「{target_id}」不是。修复：去掉 --yes")
    if not any(isinstance(e, dict) and e.get("kind") == "delivered" for e in target.get("expect", []) or []):
        raise AgentEnvError(f"落点「{target_id}」没有 kind = \"delivered\" 的期望，不是云端托管落点。"
                            f"修复：在清单该落点加 [[targets.expect]] kind = \"delivered\"")
    return _paste_flow(target_id, delivery_text(manifest, target_id, _rules_text(manifest)), record, out, err, "deliver")


def pointer(manifest_path, target_id, record=False, out=sys.stdout, err=sys.stderr) -> int:
    """The pointer rule (指路规则) of a plugin target: print it for the user to store, or record that it
    was stored. The only way a plugin target's stored text is produced or recorded."""
    manifest = _load(manifest_path)
    target = _target(manifest, target_id)
    if "plugins" not in target:
        raise AgentEnvError(f"落点「{target_id}」没有 plugins 列表，没有指路规则。修复：在清单该落点写 plugins = [...]；"
                            f"整份投递的落点用 bin/agent-env deliver --target {target_id}")
    if not any(isinstance(e, dict) and e.get("kind") == "delivered" for e in target.get("expect", []) or []):
        raise AgentEnvError(f"落点「{target_id}」没有 kind = \"delivered\" 的期望，指路规则的投递没处记录。"
                            f"修复：在清单该落点加 [[targets.expect]] kind = \"delivered\" name = \"指路规则\"")
    plugin_gate(manifest_path)
    try:
        missing = plugins.pointer_missing(manifest, target)
        if missing and record:
            raise AgentEnvError(f"指路规则里插件 {missing} 的仓库地址还是占位符，记录下来没有意义。"
                                f"修复：建好仓库并在清单 [plugins.<名字>] 写 repo 后，再输出、存入、--record")
        text = plugins.pointer_text(manifest, target)
    except plugins.PluginError as exc:
        raise AgentEnvError(str(exc))
    rc = _paste_flow(target_id, text, record, out, err, "plugin pointer")
    if missing:
        print(f"注意：插件 {missing} 的仓库还没建（清单没写 repo），上面用的是占位符；建仓后重新输出", file=err)
    return rc


# ── eval ──────────────────────────────────────────────────────────────────

EVAL_LABELS = {"pass": "通过", "fail": "失败", "err": "出错", "skip": "跳过"}


def run_eval_cli(manifest_path, args, out) -> int:
    manifest = _load(manifest_path)
    env = detect_env(manifest, args.env)
    if args.target:
        targets = [_target(manifest, args.target)]
        home = str(targets[0].get("env", ""))
        if args.env and args.env != home:
            raise AgentEnvError(f"落点「{args.target}」属于环境 {home}，和 --env {args.env} 对不上。"
                                f"修复：去掉 --env，或改成 --env {home}")
        if home != env and "eval_cases" in targets[0] and not plugins.is_cloud(manifest, home):
            raise AgentEnvError(f"落点「{args.target}」属于环境 {home}，这台机器是 {env}，在这里跑测不到它。"
                                f"修复：到 {home} 上跑 bin/agent-env eval --target {args.target}")
        env = home  # results are stored with the target's own environment, wherever the command runs
    else:
        targets = [t for t in env_targets(manifest, env) if "eval_cases" in t or "eval_skip" in t]
    if not targets:
        raise AgentEnvError(f"环境 {env} 没有登记 eval_cases 或 eval_skip 的落点。修复：在清单落点里加 eval_cases = [...]")
    try:
        rec = evals.run_eval(manifest, env, targets, args.repeat, args.out)
    except evals.EvalError as exc:
        raise AgentEnvError(str(exc))
    done = {t["id"] for t in targets}
    rows = [r for r in rec["results"] if r["target"] in done]
    if args.json:
        out.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    else:
        out.write(f"行为抽测 {env}（{rec['ran_at']}），回复保存在 {_tilde(rec['last_run']['out'])}\n")
        for r in rows:
            out.write(f"  {EVAL_LABELS.get(r['state'], r['state'])}  {r['target']}  {r['case']}  "
                      f"{r['passed']}/{r['runs']}  {r['detail']}\n")
        out.write(f"结果已写入 {_tilde(evals.eval_path(env))}；bin/agent-env status 会带上它\n")
    return evals.exit_code(rows)


# ── plugin ────────────────────────────────────────────────────────────────

def plugin_gate(manifest_path) -> None:
    """Refuse to generate anything from a manifest that `check` fails: a plugin is built only from a
    manifest and a rules source whose every section and visibility has been validated."""
    fails = [r for r in habits_check.run_checks(str(manifest_path)).results if r["level"] == habits_check.FAIL]
    if fails:
        first = fails[0]
        more = f"（共 {len(fails)} 项 FAIL，bin/agent-env check 看全部）" if len(fails) > 1 else ""
        raise AgentEnvError(f"清单没过校验，不生成也不输出任何插件内容：{first['message']}{more}。修复：{first['fix']}")


def plugin_cli(manifest_path, args, out, err) -> int:
    manifest = _load(manifest_path)
    try:
        if args.action == "repos":  # which plugin-system repositories are public; --refresh asks the hosts
            if args.refresh:
                return plugins_view.refresh(manifest, manifest_path, out)
            return plugins_view.show(manifest, manifest_path, out)
        if args.action == "pointer":
            if not args.target:
                raise AgentEnvError("plugin pointer 要指定落点。修复：加 --target <落点 id>，如 --target grok-bot/bot")
            return pointer(manifest_path, args.target, args.record, out, err)
        if args.action == "vendor-diff":  # a vendored skill's local copy against upstream HEAD (network)
            if not args.plugin or not args.skill:
                raise AgentEnvError("plugin vendor-diff 要指定插件和 skill。修复：加 --plugin <名字> --skill <skill 名>")
            return plugins.vendor_diff(manifest, args.plugin, args.skill, out, write_license=args.write_license)
        if args.skill or args.write_license:
            raise AgentEnvError("--skill 和 --write-license 只用于 plugin vendor-diff。修复：删掉它们")
        if args.target:
            raise AgentEnvError("--target 只用于 plugin pointer。修复：build / publish 用 --plugin <名字> 或 --all")
        names = plugins.resolve_names(manifest, args.plugin, args.all)
        plugin_gate(manifest_path)
        rules_text = _rules_text(manifest)
        if args.action == "publish":
            return plugins.publish(manifest, names, rules_text, out, yes=args.yes)
        for name in names:
            changed, dest, h, notes = plugins.build(manifest, name, rules_text)
            for note in notes:
                out.write(f"插件 {name}：{note}\n")
            state = "已生成" if changed else "已是最新，未写入"
            out.write(f"插件 {name}：{state} {_tilde(dest)}（哈希 {h[:12]}）\n")
        return 0
    except plugins.PluginError as exc:
        raise AgentEnvError(str(exc))


# ── CLI ───────────────────────────────────────────────────────────────────

def _common(p):
    p.add_argument("--manifest", help="清单路径（默认个人层仓库的 manifest/habits.toml，或 AGENT_ENV_MANIFEST）")
    p.add_argument("--repo", help="状态分支所在仓库（默认个人层仓库，或 AGENT_ENV_REPO）")
    p.add_argument("--env", help="环境名（默认按清单 [environments] 推断）")


def main(argv=None, out=sys.stdout, err=sys.stderr) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("check", "render"):
        rest = argv[1:]
        manifest = str(manifest_arg())
        if "--manifest" in rest:
            i = rest.index("--manifest")
            manifest = rest[i + 1] if i + 1 < len(rest) else manifest
            rest = rest[:i] + rest[i + 2:]
        if argv[0] == "check":
            return habits_check.main([manifest, *rest])
        return render.run([manifest, *rest], out=out, err=err)

    ap = argparse.ArgumentParser(prog="agent-env", description="agent 习惯分发：体检、状态、回滚")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="校验清单（转调 habits_check）")
    sub.add_parser("render", help="生成或核对规则文件（转调 render）")
    p = sub.add_parser("doctor", help="体检当前环境的每个落点")
    _common(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="输出环境状态（schema 1）")
    _common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="合并各 status/<环境> 分支")
    p.add_argument("--fetch", action="store_true", help="先 fetch 远端状态分支")
    p.add_argument("--commit", action="store_true", help="把本环境状态提交到本地 status/<环境> 分支")
    p.add_argument("--push", action="store_true", help="提交后推送状态分支（配合 --commit）")
    p.add_argument("--remote", default="origin")
    p = sub.add_parser("rollback", help="恢复最近一次快照（可再跑一次撤销）")
    p.add_argument("--manifest")
    p.add_argument("--target")
    p = sub.add_parser("deliver", help="输出或记录云端托管落点的投递内容")
    p.add_argument("--manifest")
    p.add_argument("--target", required=True)
    p.add_argument("--record", action="store_true", help="用户在 app 里确认存入后，记录内容哈希")
    p.add_argument("--dry-run", action="store_true", help="远程落点：只打印将执行的远程步骤（默认就是演练）")
    p.add_argument("--yes", action="store_true", help="远程落点：用户确认后真正执行投递")
    p = sub.add_parser("eval", help="行为抽测：按清单 eval_cases 跑各落点")
    _common(p)
    p.add_argument("--target", help="只跑这个落点（默认本环境全部落点）")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--out", help="保留回复的目录（默认新建临时目录）")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("skills", help="公司 skill：install 演练或 --yes 安装，rollback 恢复最近一次备份")
    p.add_argument("action", choices=["install", "rollback"])
    p.add_argument("--manifest")
    p.add_argument("--target", required=True)
    p.add_argument("--yes", action="store_true", help="install：备份后真正安装（默认演练）")
    p = sub.add_parser("plugin", help="插件：build 生成目录，publish 生成→扫描→提交→推送（可续跑，推送要加 --yes），pointer 指路规则，repos 各仓库是公开还是私有，vendor-diff 复制进插件的第三方 skill 和上游 HEAD 的差异")
    p.add_argument("action", choices=["build", "publish", "pointer", "repos", "vendor-diff"])
    p.add_argument("--refresh", action="store_true", help="repos：联网重查各仓库是公开还是私有，写进缓存（不加只读缓存）")
    p.add_argument("--manifest")
    p.add_argument("--plugin", help="build / publish：只处理这个插件")
    p.add_argument("--all", action="store_true", help="build / publish：清单里全部插件")
    p.add_argument("--target", help="pointer：云端托管落点 id")
    p.add_argument("--skill", help="vendor-diff：要比较的第三方 skill")
    p.add_argument("--write-license", action="store_true",
                   help="vendor-diff：许可目录里还没有时，把上游 LICENSE 写进去（已有的不替换）")
    p.add_argument("--record", action="store_true", help="pointer：用户在 app 里确认存入后，记录内容哈希")
    p.add_argument("--yes", action="store_true",
                   help="publish：真正推送（默认演练：只列出将推送的仓库、提交和文件，不推送）")
    p = sub.add_parser("promote", help="晋升：scan 只读列候选，propose 写提议草稿")
    p.add_argument("action", choices=["scan", "propose"])
    p.add_argument("--manifest")
    args = ap.parse_args(argv)
    try:
        manifest = manifest_arg(args.manifest)
        if args.cmd == "doctor":
            status = doctor(manifest, args.env, args.repo)
            out.write(json.dumps(status, ensure_ascii=False, indent=2) + "\n" if args.json
                      else format_doctor(status))
            return 1 if has_problems(status) else 0
        if args.cmd == "status":
            if args.push and not args.commit:
                raise AgentEnvError("--push 要和 --commit 一起用。修复：bin/agent-env status --commit --push")
            live = with_issues(manifest, doctor(manifest, args.env, args.repo))
            if args.commit:
                commit = commit_status(repo_arg(args.repo), live, push=args.push, remote=args.remote)
                pushed = f"，已推送到 {args.remote}" if args.push else "（本地，未推送）"
                print(f"已提交 {BRANCH_PREFIX}{live['env']}：{commit[:12]}{pushed}", file=err if args.json else out)
            if args.all:  # the panel's data step: re-ask the repositories the 插件 tab's cache lacks or has stale
                plugins_view.check_due(_load(manifest), manifest)
            result = all_status(manifest, args.env, args.repo, args.fetch, live) if args.all else live
            if args.json:
                out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            elif args.all:
                for e in result["environments"]:
                    s = e.get("summary", {})
                    out.write(f"{e['env']}（{e.get('source')}，{e.get('generated_at')}）："
                              + "，".join(f"{STATE_LABELS[k]} {s.get(k, 0)}" for k in STATES) + "\n")
                for e in result["errors"]:
                    out.write(f"读取失败 {e['env'] or '-'}：{e['message']}。修复：{e['fix']}\n")
            elif not args.commit:
                out.write(format_doctor(live))
            return 0
        if args.cmd == "rollback":
            return rollback(manifest, args.target, out)
        if args.cmd == "deliver":
            if args.dry_run and args.yes:
                raise AgentEnvError("--dry-run 和 --yes 不能一起用。修复：先 --dry-run 看步骤，用户确认后只加 --yes")
            return deliver(manifest, args.target, args.record, out, err, yes=args.yes)
        if args.cmd == "eval":
            return run_eval_cli(manifest, args, out)
        if args.cmd == "skills":
            try:
                return company_skills.main_cli(_load(manifest), args.action, args.target, args.yes, out, err)
            except company_skills.CompanySkillsError as exc:
                raise AgentEnvError(str(exc))
        if args.cmd == "plugin":
            return plugin_cli(manifest, args, out, err)
        if args.cmd == "promote":
            m = _load(manifest)
            try:
                if args.action == "scan":
                    out.write(json.dumps(promote.scan(m), ensure_ascii=False, indent=2) + "\n")
                else:
                    path = promote.propose(m)
                    print(f"已写入晋升提议草稿 {_tilde(path)}；由 AI 填写「提议」部分，用户确认后再合入", file=out)
            except ValueError as exc:
                raise AgentEnvError(str(exc))
            return 0
    except AgentEnvError as exc:
        print(f"FAIL {exc}", file=err)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
