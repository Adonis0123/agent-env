"""kind = "portal": the Local Tools portal on this machine. `registry` (~/...) is the portal's tool registry (portal.json);
every tool in it with a `module` file gets one cell (named <name>·<tool>), `skip` lists tool ids to leave out,
and with `health_url` the portal itself must answer HTTP 200 (cell <name>·服务). Only checked on this machine.

The cell naming lives here only: part() and tool_label() read a cell name back, so issues.habit_source can
tell the three cells apart (portal-registry, portal-service, portal) without knowing how they are named.
"""
import json
import re
import urllib.error
import urllib.request

from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import expand, string_fields, tilde

KIND = "portal"
SEP = "·"  # cell names are 门户·<tool>; part() and tool_label() read them back
SERVICE = "服务"
HEALTH_TIMEOUT = 5


def validate(exp: dict, target: dict, label: str) -> list:
    errs = string_fields(exp, label, ("registry",))
    if isinstance(exp.get("registry"), str) and exp["registry"] and not exp["registry"].startswith("~/"):
        errs.append((f"{label}（portal）的 registry 不是 ~ 开头的路径",
                     "写成 \"~/...\"，如 \"~/.config/local-tools/portal.json\""))
    if not isinstance(exp.get("skip", []), list) or not all(isinstance(x, str) and x for x in exp.get("skip", [])):
        errs.append((f"{label}（portal）的 skip 无效", "写成要跳过的工具 id 列表，如 [\"some-tool\"]"))
    if "health_url" in exp and not re.match(r"^https?://\S+$", str(exp["health_url"])):
        errs.append((f"{label}（portal）的 health_url={exp['health_url']!r} 不是 http(s) 地址",
                     "写成本机能访问的地址，如 \"http://localhost:8080/\""))
    return errs


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is not None:
        return [(f"落点「{target.get('id')}」有 portal 期望，但环境 {target.get('env')!r} 是 remote-exec",
                 "portal 只在本机核对：删掉这条期望，或把它放到本机环境的落点上")]
    return []


def base_name(exp: dict) -> str:
    return name_of(exp, "门户")


def part(exp, name: str):
    """portal-registry | portal-service | portal (one tool) for a cell this expectation made, else None.
    With exp None (the expectation has left the manifest) it is guessed from the name's shape, which only
    knows the default name: a custom name containing SEP (本机·门户) is then read as a tool cell."""
    if exp is None:
        if SEP not in name:
            return "portal-registry"
        return "portal-service" if name.endswith(f"{SEP}{SERVICE}") else "portal"
    base = base_name(exp)
    if name == base:
        return "portal-registry"
    if name == f"{base}{SEP}{SERVICE}":
        return "portal-service"
    return "portal" if name.startswith(f"{base}{SEP}") else None


def tool_label(exp, name: str) -> str:
    """The tool's label in a tool cell's name (<name>·<label>); a custom name may itself contain SEP."""
    prefix = f"{base_name(exp)}{SEP}" if exp is not None else None
    if prefix and name.startswith(prefix):
        return name[len(prefix):]
    return name.split(SEP, 1)[1] if SEP in name else name


def registries(manifest: dict) -> list:
    """Every portal expectation's `registry` in the manifest, first occurrence order (relation graph source)."""
    out = []
    for t in manifest.get("targets", []) or []:
        for exp in (t.get("expect", []) or []) if isinstance(t, dict) else []:
            if isinstance(exp, dict) and exp.get("kind") == KIND and isinstance(exp.get("registry"), str):
                if exp["registry"] not in out:
                    out.append(exp["registry"])
    return out


def _health(exp: dict, kind: str, base: str, layer: str) -> dict:
    url = exp["health_url"]
    name = f"{base}{SEP}{SERVICE}"
    fix = exp.get("fix") or f"门户服务没响应：先看服务日志，再重启门户，重启后打开 {url} 确认"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a local URL never goes via a proxy
    try:
        with opener.open(url, timeout=HEALTH_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (OSError, ValueError) as exc:
        return cell(kind, name, DRIFT, f"{url} 连不上（{type(exc).__name__}）", fix, layer)
    if code == 200:
        return cell(kind, name, SYNCED, f"{url} 返回 200", layer=layer)
    return cell(kind, name, DRIFT, f"{url} 返回 HTTP {code}", fix, layer)


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    """One cell per registered tool with a module file, plus the health URL."""
    kind, base, layer = habit_of(exp), base_name(exp), exp.get("layer", "host")
    registry = expand(exp["registry"])
    skip = set(exp.get("skip", []) or [])
    cells = []
    try:
        tools = json.loads(registry.read_text(encoding="utf-8"))["tools"]
        if not isinstance(tools, list):
            raise TypeError("tools 不是数组")
    except FileNotFoundError:
        cells.append(cell(kind, base, MISSING, f"门户工具清单 {tilde(registry)} 不存在",
                          exp.get("fix") or f"确认门户已装好并生成过 {tilde(registry)}", layer))
        tools = []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cells.append(cell(kind, base, UNKNOWN, f"门户工具清单 {tilde(registry)} 读不了（{type(exc).__name__}）",
                          layer=layer))
        tools = []
    for t in tools:
        if not isinstance(t, dict) or not isinstance(t.get("module"), str) or not t["module"]:
            continue  # built-in page: nothing on disk to check
        tid = str(t.get("id", ""))
        if tid in skip:
            continue
        name = f"{base}{SEP}{t.get('label') or tid}"
        module = expand(t["module"])
        if module.is_file():
            cells.append(cell(kind, name, SYNCED, f"门户工具 {tid} 的文件 {tilde(module)} 在", layer=layer))
        else:
            cells.append(cell(kind, name, MISSING, f"门户工具 {tid} 的文件 {tilde(module)} 不存在",
                              f"确认 {tid} 的仓库已克隆、文件没被移走；不再用就从 {tilde(registry)} 删掉这一项，"
                              f"或在清单 skip 里写上 {tid}", layer))
    if exp.get("health_url"):
        cells.append(_health(exp, kind, base, layer))
    return cells


check_remote = None  # portal is only checked on this machine (env_errors rejects it in remote-exec)
