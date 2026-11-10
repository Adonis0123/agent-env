#!/usr/bin/env python3
"""Compare the MCP servers a target should have (manifest [mcp] + target `mcp`)
with what its host config actually enables, plus what the host imports from
other hosts' configs (compat). Read-only: nothing is written.

Usage: python3 mcp.py <manifest> --target <id> [--json]
       python3 mcp.py <manifest> --plan [--target <id>]
       python3 mcp.py <manifest> --not-allowed-in <env>
  default  print one line per server with its state; exit 1 on drift or missing
  --plan   print every fix as one script for review; nothing is executed
  --not-allowed-in  print, one per line, the servers whose [mcp.<name>] envs exclude <env>

Readers keep only the server name, transport, command basename, URL host and
env variable names, so no secret value can reach the output.
No user-specific paths live here; every path comes from the manifest.
Requires Python 3.11+ (tomllib).
"""
import sys

if sys.version_info < (3, 11):
    print("mcp 需要 Python 3.11+（依赖 tomllib），当前是 %d.%d；"
          "请改用 `uv run --python 3.12 python3 mcp.py ...` 运行。"
          % sys.version_info[:2], file=sys.stderr)
    sys.exit(2)

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import tomllib  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from habits import find_target, load_manifest, normalize  # noqa: E402

ADD_MCP = "npx -y add-mcp@2.6.0"
# host -> add-mcp --agent value; hosts missing here have no add-mcp support.
ADD_MCP_AGENT = {"claude-code": "claude-code", "codex": "codex", "grok": "grok-build",
                 "pi": "pi", "cursor": "cursor", "hermes": "hermes"}
TRANSPORTS = ("stdio", "http")
SYNCED, DRIFT, MISSING, UNKNOWN = "synced", "drift", "missing", "unknown"
LAYER = "host"


class Unreadable(Exception):
    """A host config that cannot be read reliably; the caller reports unknown."""


# ── placement: which environment may register which server ─────────────────
# [mcp.<name>] envs = ["mac", ...] lists the [environments] where the server may be registered;
# no envs means everywhere. A server the manifest does not define is not restricted here.

def allowed_envs(spec) -> list | None:
    """The server's envs list, or None when it may run everywhere."""
    envs = spec.get("envs") if isinstance(spec, dict) else None
    return list(envs) if isinstance(envs, list) else None


def allowed_in(manifest: dict, name: str, env: str) -> bool:
    envs = allowed_envs((manifest.get("mcp") or {}).get(name))
    return envs is None or env in envs


def not_allowed_in(manifest: dict, env: str) -> list:
    """Sorted names of the manifest's servers that must not be registered in `env`."""
    return sorted(n for n in (manifest.get("mcp") or {}) if not allowed_in(manifest, n, env))


def _tilde(path) -> str:
    path = str(path)
    home = os.path.expanduser("~").rstrip(os.sep)
    if home and home != "~" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


# ── readers: host config -> [entry], set of names disabled host-wide ──────────
# entry = {"name", "transport", "target", "env", "enabled"}; "target" is the URL
# host for http and the command basename for stdio. Nothing else is kept.

def _transport(kind, url, command):
    kind = str(kind or "").lower()
    if kind in ("http", "streamable-http", "streamablehttp"):
        return "http"
    if kind in ("sse", "stdio"):
        return kind
    if url:
        return "http"
    if command:
        return "stdio"
    return None


def _entry(name, kind, url, command, env_names, enabled):
    transport = _transport(kind, url, command)
    if transport is None:
        raise Unreadable(f"server「{name}」既没有 url 也没有 command")
    if url:
        target = urlparse(str(url)).hostname or "?"
    else:
        target = os.path.basename(str(command)) or "?"
    return {"name": name, "transport": transport, "target": target,
            "env": sorted(str(k) for k in env_names), "enabled": bool(enabled)}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Unreadable(f"{_tilde(path)} 不存在")
    except (OSError, UnicodeDecodeError) as exc:
        raise Unreadable(f"读不了 {_tilde(path)}（{type(exc).__name__}）")


def _servers_table(data, key, path):
    table = data.get(key, {}) if isinstance(data, dict) else None
    if not isinstance(table, dict):
        raise Unreadable(f"{_tilde(path)} 的 {key} 不是表")
    for name, raw in table.items():
        if not isinstance(raw, dict):
            raise Unreadable(f"{_tilde(path)} 里的 server「{name}」不是表")
    return table


def _load_json(path: Path):
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise Unreadable(f"{_tilde(path)} 不是合法 JSON（第 {exc.lineno} 行）")


def _load_toml(path: Path):
    try:
        return tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise Unreadable(f"{_tilde(path)} 不是合法 TOML（{exc}）")


def read_json_servers(path: Path):
    """`mcpServers` in a JSON file: Claude (`~/.claude.json`, top level only),
    Cursor, pi and WorkBuddy. `disabled: true` or `enabled: false` turns one off."""
    table = _servers_table(_load_json(path), "mcpServers", path)
    entries = []
    for name, raw in table.items():
        enabled = raw.get("enabled", True) is not False and raw.get("disabled", False) is not True
        entries.append(_entry(name, raw.get("type") or raw.get("transport"),
                              raw.get("url") or raw.get("httpUrl") or raw.get("serverUrl"),
                              raw.get("command"), (raw.get("env") or {}).keys(), enabled))
    return entries, set()


def read_codex(path: Path):
    """`[mcp_servers.<name>]` in Codex config.toml; `enabled = false` turns one off."""
    table = _servers_table(_load_toml(path), "mcp_servers", path)
    entries = []
    for name, raw in table.items():
        env = list((raw.get("env") or {}).keys()) + list(raw.get("env_vars") or [])
        if raw.get("bearer_token_env_var"):
            env.append(raw["bearer_token_env_var"])
        entries.append(_entry(name, raw.get("type"), raw.get("url"), raw.get("command"),
                              env, raw.get("enabled", True) is not False))
    return entries, set()


def read_grok(path: Path):
    """Grok config.toml: `[mcp_servers.<name>]` plus top-level `disabled_mcp_servers`."""
    data = _load_toml(path)
    table = _servers_table(data, "mcp_servers", path)
    off = data.get("disabled_mcp_servers", [])
    if not isinstance(off, list) or not all(isinstance(x, str) for x in off):
        raise Unreadable(f"{_tilde(path)} 的 disabled_mcp_servers 不是字符串列表")
    entries = []
    for name, raw in table.items():
        enabled = raw.get("enabled", True) is not False and name not in off
        entries.append(_entry(name, raw.get("type"), raw.get("url"), raw.get("command"),
                              (raw.get("env") or {}).keys(), enabled))
    return entries, set(off)


_YAML_KEY_RE = re.compile(r"""^(\s*)("[^"]*"|'[^']*'|[A-Za-z0-9_.@-]+)\s*:(?:\s+(.*))?$""")


def _yaml_plain(raw):
    raw = (raw or "").strip()
    if raw.startswith("#"):
        return ""
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def read_hermes(path: Path):
    """`mcp_servers:` in Hermes config.yaml, by simple line rules.

    Handles a block mapping of servers, each a block mapping with scalar
    `command` / `url` / `type` / `enabled` and an optional `env:` block whose
    keys are taken as variable names. Anything else (flow mappings, anchors,
    tabs, odd indentation) raises Unreadable instead of guessing.
    """
    lines = normalize(_read_text(path)).split("\n")
    start = None
    for i, line in enumerate(lines):
        m = _YAML_KEY_RE.match(line)
        if m and not m.group(1) and _yaml_plain(m.group(2)) == "mcp_servers":
            if start is not None:
                raise Unreadable(f"{_tilde(path)} 里 mcp_servers 出现两次")
            rest = _yaml_plain(m.group(3))
            if rest == "{}":
                return [], set()
            if rest:
                raise Unreadable(f"{_tilde(path)} 的 mcp_servers 不是逐行写法，简单解析读不了")
            start = i + 1
    if start is None:
        return [], set()
    servers = {}
    current = None
    server_indent = prop_indent = sub_indent = None
    sub_key = None
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            break
        lead = line[: len(line) - len(line.lstrip())]
        if "\t" in lead:
            raise Unreadable(f"{_tilde(path)} 的 mcp_servers 用了 tab 缩进")
        indent = len(lead)
        if server_indent is None:
            server_indent = indent
        m = _YAML_KEY_RE.match(line)
        if indent == server_indent:
            if not m or _yaml_plain(m.group(3)):
                raise Unreadable(f"{_tilde(path)} 的 mcp_servers 下有无法识别的行")
            current = _yaml_plain(m.group(2))
            if current in servers:
                raise Unreadable(f"{_tilde(path)} 里 server「{current}」出现两次")
            servers[current] = {"env": []}
            prop_indent = sub_indent = sub_key = None
            continue
        if indent < server_indent or current is None:
            raise Unreadable(f"{_tilde(path)} 的 mcp_servers 缩进不一致")
        if prop_indent is None:
            prop_indent = indent
        if indent == prop_indent:
            if not m:
                raise Unreadable(f"{_tilde(path)} 里 server「{current}」有无法识别的行")
            key, value = _yaml_plain(m.group(2)), _yaml_plain(m.group(3))
            sub_key, sub_indent = (key, None) if not value else (None, None)
            if value and any(value.startswith(c) for c in "&*!|>{"):
                if not (key == "env" and value == "{}"):
                    raise Unreadable(f"{_tilde(path)} 里 server「{current}」的 {key} 写法超出简单解析")
            if key in ("command", "url", "type", "enabled", "disabled"):
                servers[current][key] = value
            continue
        if indent < prop_indent:
            raise Unreadable(f"{_tilde(path)} 里 server「{current}」缩进不一致")
        if sub_key == "env":
            if sub_indent is None:
                sub_indent = indent
            if indent == sub_indent:
                if not m:
                    raise Unreadable(f"{_tilde(path)} 里 server「{current}」的 env 写法超出简单解析")
                servers[current]["env"].append(_yaml_plain(m.group(2)))
        # deeper lines under args / headers / other keys are not needed
    entries = []
    for name, raw in servers.items():
        enabled = True
        for key, on_value in (("enabled", True), ("disabled", False)):
            if key in raw:
                flag = raw[key].lower()
                if flag in ("true", "yes", "on"):
                    enabled = enabled and on_value
                elif flag in ("false", "no", "off"):
                    enabled = enabled and not on_value
                else:
                    raise Unreadable(f"{_tilde(path)} 里 server「{name}」的 {key} 不是布尔值")
        entries.append(_entry(name, raw.get("type"), raw.get("url"), raw.get("command"),
                              raw["env"], enabled))
    return entries, set()


READERS = {"claude-code": read_json_servers, "cursor": read_json_servers, "pi": read_json_servers,
           "workbuddy": read_json_servers, "codex": read_codex, "grok": read_grok,
           "hermes": read_hermes}


def config_path(target: dict) -> Path | None:
    raw = target.get("mcp_config")
    return Path(os.path.expanduser(raw)) if isinstance(raw, str) and raw else None


def read_target(target: dict):
    """(entries, host-wide disabled names) from a target's own MCP config."""
    reader = READERS.get(target.get("host"))
    if reader is None:
        raise Unreadable(f"宿主 {target.get('host')} 没有 MCP 读取器")
    path = config_path(target)
    if path is None:
        raise Unreadable(f"落点「{target.get('id')}」没有 mcp_config")
    return reader(path)


def compat_source(manifest: dict, target: dict, host: str) -> dict:
    """The target in the same env whose host is `host` and that has an mcp_config."""
    for t in manifest.get("targets", []):
        if (isinstance(t, dict) and t.get("host") == host and t.get("env") == target.get("env")
                and isinstance(t.get("mcp_config"), str)):
            return t
    raise Unreadable(f"同环境（{target.get('env')}）里没有带 mcp_config 的 {host} 落点")


def effective(manifest: dict, target: dict):
    """Own config plus compat imports, as the host would see them.

    Returns (entries, notes). Each entry gets "source" (a host name) and
    "via" ("own" or "compat"). A compat entry is dropped when an entry with
    the exact same name is already present (hosts dedupe case-sensitively),
    so `figma` and `Figma` both survive; compare() groups names
    case-insensitively to catch that. notes lists compat sources that could
    not be read. Raises Unreadable when the target's own config is unreadable.
    """
    own, off = read_target(target)
    entries = [dict(e, source=target["host"], via="own") for e in own]
    names = {e["name"] for e in entries}
    notes = []
    for host in target.get("mcp_compat_from", []) or []:
        try:
            imported, _ = read_target(compat_source(manifest, target, host))
        except Unreadable as exc:
            notes.append((host, str(exc)))
            continue
        for e in imported:
            if e["name"] in names:
                continue
            names.add(e["name"])
            entries.append(dict(e, source=host, via="compat",
                                enabled=e["enabled"] and e["name"] not in off))
    return entries, notes


# ── fixes ─────────────────────────────────────────────────────────────────

def _add_fix(host: str, name: str, spec: dict, config: str) -> str:
    """One add-mcp command, or a Codex TOML snippet for Bearer auth."""
    auth = spec.get("auth_env")
    if host == "codex" and auth:
        return (f"把下面片段写进 {config}（有同名段就替换）：\n"
                f"[mcp_servers.{name}]\n"
                f"url = {json.dumps(spec.get('url', ''))}\n"
                f"bearer_token_env_var = {json.dumps(auth)}")
    agent = ADD_MCP_AGENT.get(host)
    if agent is None:
        where = spec.get("url") or spec.get("command")
        return f"{host} 不受 add-mcp 支持：在 {host} 的 MCP 设置里手动添加「{name}」（{spec.get('transport')} {where}）"
    if spec.get("transport") == "http":
        what = shlex.quote(spec.get("url", ""))
    else:
        what = shlex.quote(shlex.join([spec.get("command", ""), *spec.get("args", [])]))
    parts = [ADD_MCP, what, "--name", shlex.quote(name), "-g", "-a", agent, "-y"]
    for var in spec.get("env", []):
        parts += ["--env", f"'{var}=${{{var}}}'"]
    if auth:
        parts += ["--header", f"'Authorization: Bearer ${{{auth}}}'"]
    return " ".join(parts)


def _remove_fix(host: str, name: str, config: str) -> str:
    agent = ADD_MCP_AGENT.get(host)
    if agent is None:
        return f"{host} 不受 add-mcp 支持：在 {host} 的 MCP 设置里删掉或禁用「{name}」"
    if host == "codex":
        return f"在 {config} 删掉 [mcp_servers.{name}] 段，或写 enabled = false"
    return f"{ADD_MCP} remove {shlex.quote(name)} -g -a {agent} -y"


def _config_of(manifest: dict, target: dict, host: str) -> str:
    if host == target.get("host"):
        return target.get("mcp_config", "")
    try:
        return compat_source(manifest, target, host).get("mcp_config", "")
    except Unreadable:
        return f"<{host} 配置>"


def _replace_fix(manifest, target, entry, name, spec):
    """Re-create `entry` (from its source host) under the manifest name and spec."""
    host = entry["source"]
    config = _config_of(manifest, target, host)
    add = _add_fix(host, name, spec, config)
    remove = _remove_fix(host, entry["name"], config)
    if remove.startswith(ADD_MCP) and add.startswith(ADD_MCP):
        return f"{remove} && {add}"
    return f"{remove}；然后{add}"


def _where(e: dict) -> str:
    via = "本宿主" if e["via"] == "own" else f"compat 自 {e['source']}"
    return f"{e['name']}（{e['transport']} {e['target']}，{via}）"


def _cell(name, state, detail, fix=""):
    return {"kind": "mcp", "name": name, "state": state, "layer": LAYER, "detail": detail, "fix": fix}


def compare(manifest: dict, target: dict) -> list:
    """Habit cells (kind "mcp") for one target; [] when it declares no mcp_config."""
    if not isinstance(target.get("mcp_config"), str):
        return []
    try:
        entries, notes = effective(manifest, target)
    except Unreadable as exc:
        return [_cell("mcp_config", UNKNOWN, f"无法核对 MCP：{exc}")]
    cells = [_cell(f"compat:{host}", UNKNOWN, f"compat 导入无法核对：{msg}") for host, msg in notes]
    specs = manifest.get("mcp", {}) or {}
    host = target["host"]
    own_config = target["mcp_config"]
    skip = {n.lower() for n in target.get("mcp_unmanaged", []) or []}
    groups, disabled = {}, {}
    for e in entries:
        key = e["name"].lower()
        if key in skip:
            continue
        (groups if e["enabled"] else disabled).setdefault(key, []).append(e)
    for name in target.get("mcp", []) or []:
        spec = specs.get(name, {})
        group = groups.pop(name.lower(), [])
        if not group:
            off = disabled.get(name.lower())
            detail = (f"配置里有 {', '.join(_where(e) for e in off)} 但未启用" if off
                      else "宿主配置和 compat 导入里都没有")
            cells.append(_cell(name, MISSING, detail, _add_fix(host, name, spec, own_config)))
            continue
        if len(group) > 1:
            fixes = [_replace_fix(manifest, target, e, name, spec) for e in group if e["name"] != name]
            fix = "；".join(fixes) or f"只保留一处「{name}」，删掉其余来源里的同名项"
            cells.append(_cell(name, DRIFT, "重复：名字只差大小写，宿主会注册两次："
                               + "、".join(_where(e) for e in group), fix))
            continue
        e = group[0]
        if e["transport"] != spec.get("transport"):
            cells.append(_cell(name, DRIFT, f"传输不符：清单是 {spec.get('transport')}，实际 {_where(e)}",
                               _replace_fix(manifest, target, e, name, spec)))
        elif e["name"] != name:
            cells.append(_cell(name, DRIFT, f"名字大小写与清单不同：{_where(e)}；被其他宿主 compat 导入时会和「{name}」重复",
                               _replace_fix(manifest, target, e, name, spec)))
        else:
            cells.append(_cell(name, SYNCED, f"已启用：{_where(e)}"))
    for key, group in groups.items():
        detail = "清单没有要求，但已启用：" + "、".join(_where(e) for e in group)
        if len(group) > 1:
            detail += "（且名字只差大小写，重复注册）"
        fix = "；".join(_remove_fix(e["source"], e["name"], _config_of(manifest, target, e["source"]))
                       + ("" if e["via"] == "own" else f"（会影响 {e['source']} 自己；也可以把它加进本落点 mcp）")
                       for e in group)
        cells.append(_cell(group[0]["name"], DRIFT, detail, fix))
    return cells


def mcp_targets(manifest: dict) -> list:
    return [t for t in manifest.get("targets", []) if isinstance(t, dict) and isinstance(t.get("mcp_config"), str)]


def plan(manifest: dict, target_ids=None) -> str:
    """All drift / missing fixes as one script to review; nothing is executed.
    Multi-line fixes (Codex TOML snippets) are written as comments."""
    targets = ([find_target(manifest, tid) for tid in target_ids] if target_ids else mcp_targets(manifest))
    lines = ["#!/bin/sh", "# agent-env MCP 修复计划：只生成，未执行。逐条确认后再运行。", "set -e"]
    seen = set()
    count = 0
    for t in targets:
        for c in compare(manifest, t):
            if c["state"] not in (DRIFT, MISSING) or not c["fix"]:
                continue
            lines.append(f"# [{t['id']}] {c['name']} {c['state']}：{c['detail']}")
            if c["fix"] in seen:
                lines.append("#   同一修复已在上面列出")
                continue
            seen.add(c["fix"])
            count += 1
            if c["fix"].startswith(ADD_MCP) and "\n" not in c["fix"]:
                lines.append(c["fix"])
            else:
                lines += ["# 手动：" + ln if i == 0 else "#   " + ln for i, ln in enumerate(c["fix"].split("\n"))]
    lines.append(f"# 共 {count} 条修复")
    return "\n".join(lines) + "\n"


def run(argv=None, out=sys.stdout, err=sys.stderr) -> int:
    ap = argparse.ArgumentParser(description="比对落点的 MCP 配置与清单（只读）")
    ap.add_argument("manifest")
    ap.add_argument("--target")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--plan", action="store_true", help="汇总所有修复成一份计划，不执行")
    ap.add_argument("--not-allowed-in", metavar="ENV", help="逐行列出清单 envs 不允许在该环境注册的 server")
    args = ap.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except Exception as exc:
        print(f"FAIL 清单读不了：{exc}。修复：确认路径存在且是合法的 UTF-8 TOML", file=err)
        return 1
    if args.not_allowed_in:
        envs = manifest.get("environments") if isinstance(manifest.get("environments"), dict) else {}
        if args.not_allowed_in not in envs:
            print(f"FAIL 清单里没有环境「{args.not_allowed_in}」。修复：改成 {sorted(envs)} 之一", file=err)
            return 1
        for name in not_allowed_in(manifest, args.not_allowed_in):
            print(name, file=out)
        return 0
    ids = [t.get("id") for t in manifest.get("targets", []) if isinstance(t, dict)]
    if args.target and args.target not in ids:
        print(f"FAIL 清单里没有落点「{args.target}」。修复：--target 改成 {ids} 之一", file=err)
        return 1
    if args.plan:
        out.write(plan(manifest, [args.target] if args.target else None))
        return 0
    if not args.target:
        print("FAIL 缺少 --target。修复：加 --target <落点 id>，或用 --plan 看全部落点", file=err)
        return 1
    target = find_target(manifest, args.target)
    cells = compare(manifest, target)
    if args.json:
        out.write(json.dumps({"id": target["id"], "host": target.get("host"), "env": target.get("env"),
                              "habits": cells}, ensure_ascii=False, indent=2) + "\n")
    elif not isinstance(target.get("mcp_config"), str):
        print(f"落点「{args.target}」没有登记 mcp_config，不比对 MCP", file=out)
    else:
        for c in cells:
            line = f"{c['state']} {c['name']}：{c['detail']}"
            if c["fix"]:
                line += f"。修复：{c['fix']}"
            print(line, file=out)
        counts = {s: sum(c["state"] == s for c in cells) for s in (SYNCED, DRIFT, MISSING, UNKNOWN)}
        print("合计：" + "，".join(f"{k} {v}" for k, v in counts.items()), file=out)
    return 1 if any(c["state"] in (DRIFT, MISSING) for c in cells) else 0


if __name__ == "__main__":
    sys.exit(run())
