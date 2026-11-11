"""The data behind the panel's 插件 tab (docs/status-schema.md `plugins_view`): which repositories are public
or private, how far each plugin is built and published, and which plugins each environment gets.

Everything in `view` comes from the manifest, repos.json and this machine's state, so `status` stays as fast
offline as online. Whether a repository is public is the one fact that needs the network: `refresh` asks the
host (gh for github.com; any other host with the command the manifest declares in [git_hosts."<host>"]) and
writes a cache; `view` only reads that cache. Two steps keep the cache from going stale between refreshes:
`check_due` (run by `status --all`, the panel's data step, before `view`) re-asks only the repositories the
cache lacks or last answered over STALE_AFTER ago, within a few seconds; and `mark_pushed` (run by publish
after a push) records that the repository is no longer empty. A repository that was never asked about, or
whose host could not be reached, is `unknown` and carries how to fix it.

Building and publishing may belong to one machine only (the manifest's `publish_from`). On every other machine
(plugins.published_away) a build that is not there and a push that was never recorded are not problems: the
plugin's states turn neutral (`elsewhere`, `not_needed`) with no command, the plugin repositories lose their
not-built and not-pushed notes, and the view names the publishing machine in `publisher`. What the host said
about a repository (public or private, empty) shows the same on every machine.

Only repositories of the plugin system are looked at: the ones [sources] point into, this repository, and the
plugin repositories ([plugins.<name>].repo, or [plugin_repos].<visibility> while no plugin of that visibility
records one). The dozens of work repositories in repos.json are left alone.
"""
import concurrent.futures
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import overlay
import plugins
import render
from expect import plugins as plugin_check
from state import state_dir

REPOS_FILE = overlay.overlay_root() / overlay.REPOS  # the personal repository's repos.json, if it has one
REFRESH_COMMAND = "bin/agent-env plugin repos --refresh"
SOURCE_KEYS = ("rules", "docs", "hooks", "skills", "company_skills")  # what a habit is made from
ORDER = ("public", "personal", "company", plugins.LOCAL)              # the columns of the environment table
PUSHED = tuple(v for v in ORDER if v != plugins.LOCAL)                # visibilities that have a repository
PUBLIC, PRIVATE, UNKNOWN = "public", "private", "unknown"
PROBE_TIMEOUT = 20
# `status --all` re-asks a repository whose cached answer is older than this. A day: visibility rarely changes,
# and one status a day paying a few seconds online is cheap; a refresh by hand still asks everything at once.
STALE_AFTER = timedelta(hours=24)
# the most `status --all` waits for those re-asks (also each probe's timeout); past it the panel shows the cache
DUE_BUDGET = 4
LOCAL_TRANSPORT = "local"
# docs/plugins-design.md "Migration stages": the stage that moves an environment without plugins onto them
LEGACY_PHASE = {plugins.REMOTE_EXEC: 3, LOCAL_TRANSPORT: 4}
LEGACY_NOW = {plugins.REMOTE_EXEC: "规则在本机生成后投递过去", LOCAL_TRANSPORT: "直接读来源仓"}
STEP_ZH = {"build": "生成", "scan": "扫描", "commit": "提交", "push": "推送"}

_URL_RE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/\s]+@)?([^/:\s]+)(?::\d+)?/(.+)$")
_SCP_RE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$", re.ASCII)
_PATH_RE = re.compile(r"^[A-Za-z0-9_][\w.-]*(?:/[A-Za-z0-9_][\w.-]*)+$", re.ASCII)
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


# ── repositories ──────────────────────────────────────────────────────────

def repo_key(remote):
    """`host/owner/repo` for a git remote or a web address (ssh, scp-like or https; `.git` dropped), else None.
    The key is what the cache is indexed by, and `https://<key>` is the page to open."""
    if not isinstance(remote, str):
        return None
    m = _URL_RE.match(remote.strip()) or _SCP_RE.match(remote.strip())
    if not m:
        return None
    host, path = m.group(1).lower(), m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not _HOST_RE.match(host) or not _PATH_RE.match(path):
        return None
    return f"{host}/{path}"


def cache_path() -> Path:
    return state_dir() / "repo-visibility.json"


def read_cache() -> dict:
    """{key: record} of the last refresh; {} when there is none or it cannot be read."""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    return {k: v for k, v in repos.items() if isinstance(v, dict)} if isinstance(repos, dict) else {}


def _write_cache(repos: dict, stamp) -> None:
    render.atomic_write(cache_path(), json.dumps({"schema": 1, "checked_at": stamp, "repos": repos},
                                                 ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _merge_cache(updates: dict) -> None:
    """Write `updates` ({key: record}) over the cache, keeping every other entry and when it was last refreshed."""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    stamp = data.get("checked_at") if isinstance(data, dict) and isinstance(data.get("checked_at"), str) else None
    _write_cache(dict(read_cache(), **updates), stamp)


def _stamp(now=None) -> str:
    return now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def mark_pushed(url, seen=None, now=None) -> None:
    """After publish pushed to `url` (or found its content already there): the repository is not empty any more.
    `seen` is what the host said just before the push; without a usable answer, only the cached entry's `empty`
    flips. Never raises: a cache that cannot be written is fixed by the next refresh."""
    try:
        key = repo_key(url)
        if not key:
            return
        seen = seen if isinstance(seen, dict) else {}
        if seen.get("visibility") in (PUBLIC, PRIVATE):
            rec = {"visibility": seen["visibility"], "empty": False, "via": seen.get("via"), "error": None,
                   "fix": None, "checked_at": _stamp(now)}
        else:
            prev = read_cache().get(key)
            if not isinstance(prev, dict) or prev.get("visibility") not in (PUBLIC, PRIVATE) or prev.get("empty") is False:
                return
            rec = dict(prev, empty=False)
        _merge_cache({key: rec})
    except Exception:
        return


def due(keys, cache: dict, now=None) -> list:
    """The keys `check_due` re-asks: not in the cache, without a public/private answer, or answered too long ago."""
    at = _parse_stamp(_stamp(now))
    out = []
    for k in keys:
        rec = cache.get(k) if isinstance(cache.get(k), dict) else {}
        checked = _parse_stamp(rec.get("checked_at"))
        if rec.get("visibility") not in (PUBLIC, PRIVATE) or checked is None or at - checked > STALE_AFTER:
            out.append(k)
    return out


def check_due(manifest: dict, manifest_path, repos_file=None, run=subprocess.run, now=None, budget=DUE_BUDGET) -> list:
    """Re-ask only the due repositories (see `due`), all at once, waiting at most `budget` seconds. Answers are
    merged into the cache; a repository that could not be asked keeps its cached entry (or stays `unknown`), and
    a refresh by hand still says why. Never raises. The keys that got an answer."""
    try:
        keys = due(layout(manifest, manifest_path, repos_file)["keys"], read_cache(), now)
        if not keys:
            return []
        hosts = git_hosts(manifest)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        futures = {pool.submit(probe, k, run, hosts, budget): k for k in keys}
        done, _ = concurrent.futures.wait(futures, timeout=budget + 1)
        pool.shutdown(wait=False, cancel_futures=True)
        stamp = _stamp(now)
        found = {}
        for f in done:
            res = f.result() if not f.exception() else None
            if isinstance(res, dict) and res.get("visibility") in (PUBLIC, PRIVATE):
                found[futures[f]] = dict(res, checked_at=stamp)
        if found:
            _merge_cache(found)
        return sorted(found)
    except Exception:
        return []


def _tilde(path) -> str:
    return render._tilde(path)


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def repo_info(key: str, cache: dict) -> dict:
    """One repository as the panel shows it; `unknown` says why and what to run."""
    rec = cache.get(key) if isinstance(cache.get(key), dict) else {}
    vis = rec.get("visibility") if rec.get("visibility") in (PUBLIC, PRIVATE) else UNKNOWN
    detail = fix = ""
    if vis == UNKNOWN:
        detail = _str(rec.get("error")) or "还没查过这个仓库是公开还是私有"
        fix = _str(rec.get("fix")) or REFRESH_COMMAND
    return {"key": key, "name": key.rsplit("/", 1)[-1], "url": f"https://{key}", "host": key.split("/", 1)[0],
            "visibility": vis, "empty": rec.get("empty") if isinstance(rec.get("empty"), bool) else None,
            "checked_at": _str(rec.get("checked_at")) or None, "detail": detail, "fix": fix}


def _repo_entries(repos_file) -> list:
    """[(checkout path, key)] of repos.json, longest path first; [] when the file cannot be read."""
    try:
        data = json.loads(Path(repos_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in data.get("repos", []) if isinstance(data, dict) and isinstance(data.get("repos"), list) else []:
        key = repo_key(r.get("remote")) if isinstance(r, dict) else None
        if key and isinstance(r.get("path"), str):
            out.append((os.path.normpath(os.path.expanduser(r["path"])), key))
    return sorted(out, key=lambda e: -len(e[0]))


def _owner(entries: list, path):
    """(checkout path, key) of the repos.json repository `path` is inside, else None."""
    for resolve in (os.path.normpath, os.path.realpath):
        p = resolve(str(path))
        for root, key in entries:
            root = resolve(root)
            if p == root or p.startswith(root + os.sep):
                return root, key
    return None


def layout(manifest: dict, manifest_path, repos_file=None) -> dict:
    """Where things live, before any state: the source repositories, this repository (the generator) and
    the plugin repositories by visibility. `keys` is every repository worth asking about, in that order."""
    entries = _repo_entries(repos_file or REPOS_FILE)
    gen = _owner(entries, Path(manifest_path).resolve())
    gen_root = gen[0] if gen else str(Path(manifest_path).resolve().parent.parent)
    generator = {"name": gen[1].rsplit("/", 1)[-1] if gen else "agent-env", "repo": gen[1] if gen else None,
                 "path": _tilde(gen_root), "provides": [], "output": _tilde(state_dir() / "plugins")}
    raw = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
    found = {}
    for k in SOURCE_KEYS:
        if not isinstance(raw.get(k), str):
            continue
        own = _owner(entries, raw[k])
        if own and gen and own[1] == gen[1]:
            generator["provides"].append(k)  # kept in this repository (company skills)
            continue
        root, key = own if own else (os.path.normpath(raw[k]), None)
        card = found.setdefault(root, {"name": key.rsplit("/", 1)[-1] if key else _tilde(root), "repo": key,
                                       "path": _tilde(root), "provides": []})
        card["provides"].append(k)
    table = plugins.table(manifest)
    fallback = manifest.get("plugin_repos") if isinstance(manifest.get("plugin_repos"), dict) else {}
    plugin_repos = []
    for vis in PUSHED:
        groups = {}
        for name, spec in table.items():
            if plugins.visibility(spec) == vis:
                groups.setdefault(repo_key(spec.get("repo")) or repo_key(fallback.get(vis)), []).append(name)
        if not groups and repo_key(fallback.get(vis)):
            groups[repo_key(fallback.get(vis))] = []  # the repository is there, the plugin is not defined yet
        for key, names in groups.items():
            plugin_repos.append({"visibility": vis, "visibility_zh": plugins.label(vis), "repo": key, "plugins": names})
    keys = [c["repo"] for c in found.values()] + [generator["repo"]] + [c["repo"] for c in plugin_repos]
    return {"sources": list(found.values()), "generator": generator, "plugin_repos": plugin_repos,
            "keys": list(dict.fromkeys(k for k in keys if k))}


# ── asking the hosts (the only part that goes on the network) ─────────────

def _unknown(error: str, fix: str) -> dict:
    return {"visibility": UNKNOWN, "empty": None, "via": None, "error": error, "fix": fix}


def git_host_errors(host, spec) -> list:
    """(message, fix) pairs for one [git_hosts."<host>"] table: how to ask a host other than github.com whether
    a repository is public.

      [git_hosts."git.example.com"]
      command = ["tool", "repo", "show", "{repo}"]       # {repo} is the path after the host (owner/name)
      visibility = '^\\s*visibility:\\s*"?([A-Za-z]+)'  # regex on stdout; group 1 "public" (any case) = public,
                                                         # any other value = private, no match = unknown
      fail = '^status:\\s*(?!200\\b)\\d+'              # optional: a match on stdout means the lookup failed
      login = "tool login"                               # optional: shown in the fix when the lookup fails
    """
    label = f'[git_hosts."{host}"]'
    if not isinstance(spec, dict):
        return [(f"{label} 不是表", "写成带 command 和 visibility 的表")]
    errs = []
    cmd = spec.get("command")
    if (not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) and a for a in cmd)
            or not any("{repo}" in a for a in cmd)):
        errs.append((f"{label} 的 command 无效", "写成命令和参数的列表，某个参数里写 {repo}（主机后面的仓库路径）"))
    for key in ("visibility", "fail"):
        v = spec.get(key)
        if v is None and key == "fail":
            continue
        try:
            groups = re.compile(v).groups if isinstance(v, str) and v else -1
        except re.error:
            groups = -1
        if groups < 0 or (key == "visibility" and groups < 1):
            errs.append((f"{label} 的 {key} 不是合法的正则" + ("（要有一个捕获组）" if key == "visibility" else ""),
                         "visibility 写成取出可见性的正则，第 1 组是 public 或其他值" if key == "visibility"
                         else "fail 写成查询失败时输出里会有的内容的正则，或删掉"))
    if "login" in spec and (not isinstance(spec["login"], str) or not spec["login"]):
        errs.append((f"{label} 的 login 无效", "写成登录命令，如 \"tool login\"，或删掉"))
    return errs


def git_hosts(manifest: dict) -> dict:
    """The valid [git_hosts] tables of the manifest, by host."""
    table = manifest.get("git_hosts") if isinstance(manifest.get("git_hosts"), dict) else {}
    return {h: s for h, s in table.items() if git_host_errors(h, s) == []}


def probe(key: str, run=subprocess.run, hosts=None, timeout=PROBE_TIMEOUT) -> dict:
    """Ask the host whether one repository is public. Never raises: what went wrong comes back as `unknown`
    with the fix. Private also covers GitHub's internal and any other level that is not public. github.com is
    asked with gh; another host only when `hosts` (git_hosts(manifest)) says how."""
    host, _, path = key.partition("/")
    again = f"再跑 {REFRESH_COMMAND}"
    hosts = hosts if isinstance(hosts, dict) else {}
    spec = hosts.get(host)
    if host == "github.com":
        cmd, login = ["gh", "repo", "view", path, "--json", "visibility,isEmpty"], "gh auth login"
    elif isinstance(spec, dict):
        cmd = [a.replace("{repo}", path) for a in spec["command"]]
        login = spec.get("login") or f"{cmd[0]} 的登录命令"
    else:
        known = "、".join(["github.com", *sorted(hosts)])
        return _unknown(f"不会查 {host} 上的仓库（只会查 {known}）",
                        f"在清单写 [git_hosts.\"{host}\"]（command、visibility），或到这个仓库的设置页自己确认是公开还是私有")
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return _unknown(f"这台机器没装 {cmd[0]} 命令", f"装好 {cmd[0]} 并登录（{login}），{again}")
    except subprocess.TimeoutExpired:
        return _unknown(f"{cmd[0]} {timeout} 秒没有返回", f"检查网络，{again}")
    except OSError as exc:
        return _unknown(f"{cmd[0]} 跑不起来（{type(exc).__name__}）", f"确认 {cmd[0]} 能运行，{again}")
    out = p.stdout or ""
    said = next((ln.strip() for ln in ((p.stderr or "") + "\n" + out).splitlines() if ln.strip()), "没有输出")[:160]
    fix = f"确认能联网并已登录（{login}），{again}"
    if p.returncode != 0:
        return _unknown(f"{cmd[0]} 没查到：{said}", fix)
    if host == "github.com":
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        vis = data.get("visibility") if isinstance(data, dict) else None
        if not isinstance(vis, str) or not vis:
            return _unknown("gh 的输出里没有 visibility", fix)
        empty = data.get("isEmpty") if isinstance(data.get("isEmpty"), bool) else None
        return {"visibility": PUBLIC if vis.upper() == "PUBLIC" else PRIVATE, "empty": empty, "via": "gh",
                "error": None, "fix": None}
    failed = spec.get("fail") and re.search(spec["fail"], out, re.M)
    level = None if failed else re.search(spec["visibility"], out, re.M)
    if not level:
        return _unknown(f"{cmd[0]} 没查到：{said}", fix)
    return {"visibility": PUBLIC if level.group(1).lower() == "public" else PRIVATE, "empty": None, "via": cmd[0],
            "error": None, "fix": None}


def _label(vis: str) -> str:
    return {PUBLIC: "公开", PRIVATE: "私有"}.get(vis, "未知")


def refresh(manifest: dict, manifest_path, out, repos_file=None, run=subprocess.run, now=None) -> int:
    """Ask about every repository of the plugin system and write the cache `view` reads. A repository that
    could not be asked about this time keeps its last answer. 0 when every one answered, 1 otherwise."""
    keys = layout(manifest, manifest_path, repos_file)["keys"]
    old = read_cache()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        hosts = git_hosts(manifest)
        results = list(pool.map(lambda k: probe(k, run, hosts), keys))
    stamp = _stamp(now)
    repos, failed = {}, 0
    for key, res in zip(keys, results):
        if res["visibility"] != UNKNOWN:
            repos[key] = dict(res, checked_at=stamp)
            out.write(f"{_label(res['visibility'])}  {key}{'（空仓）' if res['empty'] else ''}\n")
            continue
        failed += 1
        prev = old.get(key)
        if isinstance(prev, dict) and prev.get("visibility") in (PUBLIC, PRIVATE):
            repos[key] = prev
            out.write(f"未知  {key}：{res['error']}；保留上次查到的「{_label(prev['visibility'])}」"
                      f"（{_str(prev.get('checked_at')) or '时间不明'}）。修复：{res['fix']}\n")
        else:
            repos[key] = dict(res, checked_at=stamp)
            out.write(f"未知  {key}：{res['error']}。修复：{res['fix']}\n")
    _write_cache(repos, stamp)
    out.write(f"已写入 {_tilde(cache_path())}（{len(keys)} 个仓库，{failed} 个没查到）\n")
    return 1 if failed else 0


def show(manifest: dict, manifest_path, out, repos_file=None) -> int:
    """Print what the cache says, without asking anyone."""
    cache = read_cache()
    for key in layout(manifest, manifest_path, repos_file)["keys"]:
        info = repo_info(key, cache)
        if info["visibility"] == UNKNOWN:
            out.write(f"未知  {key}：{info['detail']}。修复：{info['fix']}\n")
        else:
            out.write(f"{_label(info['visibility'])}  {key}{'（空仓）' if info['empty'] else ''}"
                      f"（查于 {info['checked_at'] or '时间不明'}）\n")
    out.write(f"重新查一遍：{REFRESH_COMMAND}\n")
    return 0


# ── plugins: built? pushed? ───────────────────────────────────────────────

def _note(code: str, state: str, text: str, fix: str = "", at=None) -> dict:
    """`at`: the environment the fix is done on when that is not this machine; the fix is then words that
    name the machine, never a command to run here."""
    note = {"code": code, "state": state, "text_zh": text, "fix": fix}
    return dict(note, at=at) if at else note


def plugin_state(manifest: dict, name: str, rules_text, ledger: dict, away=None) -> dict:
    """`away`: plugins.published_away, when another machine builds and publishes."""
    spec = plugins.table(manifest).get(name)
    spec = spec if isinstance(spec, dict) else {}
    vis = plugins.visibility(spec)
    try:
        c, built = plugin_check.judge(manifest, name, rules_text)
        build = {"state": c["state"], "detail": c.get("detail", ""), "fix": c.get("fix", "")}
    except Exception as exc:  # a half-written manifest or source: the tab still shows, this plugin is unknown
        built = None
        build = {"state": "unknown", "detail": f"生成状态查不了（{type(exc).__name__}: {exc}）",
                 "fix": "bin/agent-env check"}
    if away and built in ("missing", "stale"):  # not this machine's to build: nothing is missing here
        build = {"state": "not_needed" if vis == plugins.LOCAL else "elsewhere",
                 "detail": plugin_check.away_detail(manifest, name, away), "fix": ""}
    rec = ledger.get("plugins", {}).get(name) if isinstance(ledger.get("plugins"), dict) else None
    steps = rec.get("steps") if isinstance(rec, dict) and isinstance(rec.get("steps"), dict) else {}
    steps = {s: steps[s] for s in plugins.LEDGER_STEPS if isinstance(steps.get(s), str)}
    again = f"bin/agent-env plugin publish --plugin {name}"
    pushed_at = steps.get("push")
    if vis == plugins.LOCAL:
        publish = {"state": "local", "detail": "本机插件（补丁层）不推送", "fix": ""}
    elif pushed_at:
        current = False
        if build["state"] == "synced":  # the built directory equals the sources: its hash is the sources' hash
            try:
                built = plugins.tree_files(plugins.output_dir(manifest, name))
                current = built is not None and plugins.files_hash(built) == rec.get("input")
            except Exception:
                current = False
        publish = ({"state": "pushed", "detail": f"已推送（{pushed_at}）", "fix": ""} if current else
                   {"state": "stale", "detail": f"上次推送在 {pushed_at}，之后源变了，仓库里是旧版", "fix": again})
    else:
        done = "、".join(STEP_ZH[s] for s in steps)
        left = "、".join(STEP_ZH[s] for s in plugins.LEDGER_STEPS if s not in steps)
        publish = {"state": "not_pushed", "detail": f"已完成：{done}；还没做：{left}" if done else "还没开始发布", "fix": again}
    if away and publish["state"] in ("stale", "not_pushed"):  # the publish records are on the other machine
        publish = {"state": "elsewhere", "detail": f"由 {away['name']} 推送，这台机器不推送", "fix": ""}
    repo = spec.get("repo") if isinstance(spec.get("repo"), str) and spec.get("repo") else None
    return {"name": name, "visibility": vis, "visibility_zh": plugins.label(vis),
            "description": _str(spec.get("description")), "repo": repo, "repo_key": repo_key(repo),
            "build": build, "publish": dict(publish, pushed_at=pushed_at, steps=steps)}


def _repo_notes(card: dict, states: dict, info, away=None) -> list:
    """What is still open for one plugin repository, worst first. With `away` (another machine builds and
    publishes) nothing about builds and pushes is open here; an empty repository still shows, and says where
    to publish."""
    notes = []
    vis, zh = card["visibility"], card["visibility_zh"]
    mine = [states[n] for n in card["plugins"] if n in states]
    if info and info["visibility"] == PUBLIC and vis != PUBLIC:
        notes.append(_note("exposed", "fail", f"仓库是公开的，不能放{zh}插件", "把仓库改成私有，或把这个插件的 repo 换成私有仓"))
    if not card["plugins"]:
        notes.append(_note("no_plugin", "drift", "插件未定义", f"在清单加一个 visibility = \"{vis}\" 的 [plugins.<名字>]"))
    if not card["repo"]:
        notes.append(_note("no_repo", "drift", "仓库地址未填", "在清单这个插件里写 repo = \"<仓库地址>\""))
    if not away and any(p["build"]["state"] == "missing" for p in mine):
        notes.append(_note("not_built", "missing", "未生成", "bin/agent-env plugin build --all"))
    if not away and any(p["build"]["state"] == "drift" for p in mine):
        notes.append(_note("stale_build", "drift", "生成结果过期", "bin/agent-env plugin build --all"))
    if info and info["empty"]:
        notes.append(_note("empty_repo", "drift", "空仓", f"到 {away['name']} 上让 AI 发布", away["env"]) if away
                     else _note("empty_repo", "drift", "空仓"))
    if not away and any(p["publish"]["state"] == "stale" for p in mine):
        notes.append(_note("stale_push", "drift", "仓库里是旧版", "bin/agent-env plugin publish --all"))
    if not away and any(p["publish"]["state"] == "not_pushed" for p in mine):
        notes.append(_note("not_pushed", "drift", "未推送", "bin/agent-env plugin publish --all"))
    if info and info["visibility"] == PRIVATE and vis == PUBLIC:
        notes.append(_note("repo_private", "drift", "仓库还是私有，别人拿不到公开插件", "把仓库改成公开，或换一个公开仓"))
    return notes


# ── environments: who gets what ───────────────────────────────────────────

def _names(manifest: dict, key: str) -> dict:
    t = manifest.get(key)
    return t if isinstance(t, dict) else {}


def _sent_away(note, away):
    """On a machine that does not build or publish (`away`), a note whose fix starts there (the pointer rule of
    a bot, a remote host that pulls) says which machine to go to and carries no command."""
    if not note or not away:
        return note
    return _note(note["code"], note["state"], note["text_zh"], f"到 {away['name']} 上让 AI 处理", away["env"])


def _pointer_note(statuses, env: str):
    """The pointer rule of a cloud-hosted environment, as its own doctor cell already judged it."""
    for e in statuses or []:
        if not isinstance(e, dict) or e.get("env") != env:
            continue
        for t in e.get("targets", []) or []:
            for h in (t.get("habits", []) or []) if isinstance(t, dict) else []:
                if isinstance(h, dict) and h.get("expect_kind") == "delivered" and h.get("state") in ("missing", "drift"):
                    text = "指路规则未投递" if h["state"] == "missing" else "指路规则过期"
                    return _note(f"pointer_{h['state']}", h["state"], text, _str(h.get("fix")))
    return None


def _pull_note(statuses, env: str):
    """How a remote host that pulls its plugins is doing, as its own doctor cells (kind pulled) judged it."""
    for e in statuses or []:
        if not isinstance(e, dict) or e.get("env") != env:
            continue
        for t in e.get("targets", []) or []:
            for h in (t.get("habits", []) or []) if isinstance(t, dict) else []:
                if isinstance(h, dict) and h.get("expect_kind") == "pulled" and h.get("state") in ("missing", "drift"):
                    timer = h.get("name") == "定时拉取"
                    text = ("还没装定时拉取" if timer else "还在用旧规则") if h["state"] == "missing" else \
                        ("定时拉取出了问题" if timer else "拉到的规则不是最新")
                    return _note(f"pull_{h['state']}", h["state"], text, _str(h.get("fix")))
    return None


def _compose_note(statuses, env: str):
    """How one of the user's machines that composes its rules file is doing, as its doctor cells (kind
    composed) judged it."""
    for e in statuses or []:
        if not isinstance(e, dict) or e.get("env") != env:
            continue
        for t in e.get("targets", []) or []:
            for h in (t.get("habits", []) or []) if isinstance(t, dict) else []:
                if isinstance(h, dict) and h.get("expect_kind") == "composed" and h.get("state") in ("missing", "drift"):
                    text = "规则文件还没生成" if h["state"] == "missing" else "规则文件和应该生成的内容不一致"
                    return _note(f"compose_{h['state']}", h["state"], text, _str(h.get("fix")))
    return None


def _credential(manifest: dict, transport: str, attached: list) -> tuple:
    """(credential, notes) for the targets of one environment that pull plugins."""
    if transport == plugins.REMOTE_EXEC:
        # a remote host pulls with the git login it already has (its own git credentials)
        return {"state": "own", "name": None, "expires": None, "detail": "用那台机器已有的 git 凭证"}, []
    if transport != plugins.CLOUD_HOSTED:
        # one of the user's own machines composes the plugins from the sources it already has (stage 4):
        # it reads no plugin repository, so it needs no credential
        return {"state": "none", "name": None, "expires": None, "detail": "本机从规则源直接合成，不拉插件仓"}, []
    rank = ("none", "token", "unknown", "expiring", "expired", "missing")  # the row shows the worst one
    worst, notes = {"state": "none", "name": None, "expires": None, "detail": "不需要"}, []
    for t in attached:
        try:
            state, detail, fix, warn = plugins.token_state(t, manifest)
        except Exception as exc:
            state, detail, fix, warn = "unknown", f"token 状态查不了（{type(exc).__name__}）", "bin/agent-env check", False
        tok = t.get("token") if isinstance(t.get("token"), dict) else None
        if tok is None and state == "synced":
            continue  # only public plugins: nothing to hold
        expires = plugins.parse_date(tok.get("expires")) if tok else None
        cred = {"name": plugins.token_name(t) if tok else None, "expires": expires.isoformat() if expires else None,
                "detail": detail}
        if state == "missing":
            cred["state"] = "missing"
            notes.append(_note("token_missing", "missing", "还没登记 token", fix))
        elif state == "synced":
            cred["state"] = "expiring" if warn else "token"
            if warn:
                notes.append(_note("token_expiring", "drift", "token 快到期", fix))
        elif state == "unknown":
            cred["state"] = "unknown"
        else:
            cred["state"] = "expired"
            notes.append(_note("token_expired", "drift", "token 已到期或登记有误", fix))
        if rank.index(cred["state"]) > rank.index(worst["state"]):
            worst = cred
    return worst, notes


def environments(manifest: dict, statuses, table=None, away=None) -> list:
    """One row per declared environment, in the manifest's order, then one for a machine that is not the
    user's at all (it can only ever read what is public). `away`: plugins.published_away."""
    table = plugins.table(manifest) if table is None else table
    envs = manifest.get("environments") if isinstance(manifest.get("environments"), dict) else {}
    hosts, env_names = _names(manifest, "host_names"), _names(manifest, "env_names")
    all_targets = [t for t in manifest.get("targets", []) or [] if isinstance(t, dict)]
    rows = []
    for env, spec in envs.items():
        spec = spec if isinstance(spec, dict) else {}
        transport = spec.get("transport") if spec.get("transport") in (plugins.REMOTE_EXEC, plugins.CLOUD_HOSTED) \
            else LOCAL_TRANSPORT
        deny = [v for v in spec.get("deny_visibility", []) or [] if isinstance(v, str)] \
            if isinstance(spec.get("deny_visibility"), list) else []
        targets = [t for t in all_targets if t.get("env") == env]
        attached = [t for t in targets if plugins.target_plugins(t)]
        mode = "plugins" if attached else "legacy"
        cards = []
        for t in targets:
            host = _str(t.get("host"))
            cards.append({"id": _str(t.get("id")), "name": _str(t.get("display_name")) or _str(hosts.get(host)) or host,
                          "plugins": plugins.target_plugins(t)})
        gets, notes = {}, []
        for vis in ORDER:
            names = list(dict.fromkeys(n for t in attached for n in plugins.target_plugins(t)
                                       if isinstance(table.get(n), dict) and plugins.visibility(table[n]) == vis))
            if vis in deny:
                gets[vis] = {"state": "denied", "plugins": names}
                if names:  # habits_check refuses this too; the tab must not show it as fine
                    notes.append(_note("denied_attached", "fail", f"挂了禁止的{plugins.label(vis)}插件 {'、'.join(names)}",
                                       "从清单该落点的 plugins 里删掉它"))
            elif mode == "legacy":
                gets[vis] = {"state": "legacy", "plugins": []}
            else:
                gets[vis] = {"state": "gets" if names else "none", "plugins": names}
        row = {"env": env, "name": _str(env_names.get(env)) or env, "transport": transport, "mode": mode,
               "phase": None, "now_zh": None, "deny": deny, "targets": cards, "gets": gets}
        if mode == "legacy":
            row["phase"], row["now_zh"] = LEGACY_PHASE[transport], LEGACY_NOW[transport]
            row["credential"] = {"state": "legacy", "name": None, "expires": None, "detail": "改成插件后再定"}
            notes.append(_note("legacy", "planned", f"还没改成插件（阶段 {row['phase']}）"))
        else:
            row["credential"], more = _credential(manifest, transport, attached)
            notes += more
            pointer = _sent_away(_pointer_note(statuses, env), away) if transport == plugins.CLOUD_HOSTED else \
                _sent_away(_pull_note(statuses, env), away) if transport == plugins.REMOTE_EXEC else \
                _compose_note(statuses, env)
            if pointer:
                notes.append(pointer)
        row["notes"] = notes
        rows.append(row)
    public = [n for n, s in table.items() if plugins.visibility(s) == PUBLIC]
    others = {v: {"state": "none", "plugins": []} for v in ORDER}
    others[PUBLIC] = {"state": "gets" if public else "none", "plugins": public}
    others[plugins.LOCAL] = {"state": "own", "plugins": []}
    rows.append({"env": None, "name": "别人的机器", "transport": None, "mode": "others", "phase": None, "now_zh": None,
                 "deny": [], "targets": [], "gets": others, "notes": [],
                 "credential": {"state": "none", "name": None, "expires": None, "detail": "不需要"}})
    return rows


# ── the field ─────────────────────────────────────────────────────────────

def _view(manifest: dict, manifest_path, statuses, rules_text, repos_file=None, env=None) -> dict:
    lay = layout(manifest, manifest_path, repos_file)
    cache = read_cache()
    repos = {k: repo_info(k, cache) for k in lay["keys"]}
    ledger = plugins.read_ledger()
    table = plugins.table(manifest)
    away = plugins.published_away(manifest, env)
    states = {n: plugin_state(manifest, n, rules_text, ledger, away) for n in table}
    for card in lay["plugin_repos"]:
        card["notes"] = _repo_notes(card, states, repos.get(card["repo"]), away)
    stamps = sorted(r["checked_at"] for r in repos.values() if r["checked_at"])
    out = {"refresh_command": REFRESH_COMMAND, "checked_at": stamps[-1] if stamps else None,
           "repos": list(repos.values()), "sources": lay["sources"], "generator": lay["generator"],
           "plugin_repos": lay["plugin_repos"], "plugins": list(states.values()),
           "environments": environments(manifest, statuses, table, away)}
    return dict(out, publisher=away) if away else out


def view(manifest: dict, manifest_path, statuses, rules_text, repos_file=None, env=None) -> dict:
    """The `plugins_view` field of `status --json --all`. It reads only the manifest and local files, and a
    failure in here never fails the status: the field then carries `error` and the tab shows it. `env` is the
    environment the status was taken as (--env, or the one this machine matches): it tells whether this
    machine is the one that builds and publishes (plugins.published_away)."""
    try:
        return _view(manifest, manifest_path, statuses, rules_text, repos_file, env)
    except Exception as exc:
        return {"refresh_command": REFRESH_COMMAND,
                "error": {"message": f"插件视图算不出来（{type(exc).__name__}: {exc}）",
                          "fix": "bin/agent-env check 看清单哪里不对"}}
