"""kind = "service": a systemd unit (`unit`, `scope` user | system) must be active on a remote-exec host;
with `health_url` it must also answer HTTP 200, and with `health_json` that JSON key must be a non-empty list.
"""
import re
import shlex

import remote
import render
from cells import DRIFT, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import string_fields

KIND = "service"
STATES = {"active": SYNCED, "inactive": DRIFT, "failed": DRIFT}
HEALTH_TIMEOUT = 15  # seconds for one health request on the remote host


def validate(exp: dict, target: dict, label: str) -> list:
    errs = string_fields(exp, label, ("unit",))
    if exp.get("scope", "user") not in ("user", "system"):
        errs.append((f"{label}（service）的 scope={exp.get('scope')!r} 无效", "写成 \"user\"（默认）或 \"system\""))
    if "health_url" in exp and not re.match(r"^https?://\S+$", str(exp["health_url"])):
        errs.append((f"{label}（service）的 health_url={exp['health_url']!r} 不是 http(s) 地址",
                     "写成远程主机上能 curl 的地址，如 \"http://localhost:8080/healthz\""))
    if "health_json" in exp and (
            not isinstance(exp["health_json"], str) or not exp["health_json"] or "health_url" not in exp):
        errs.append((f"{label}（service）的 health_json 无效",
                     "写成返回 JSON 里必须是非空列表的键名，如 \"results\"，并同时写 health_url"))
    return errs


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is None:
        return [(f"落点「{target.get('id')}」有 service 期望，但环境 {target.get('env')!r} 不是 remote-exec",
                 f"service 只在远程环境核对：给环境写 transport = \"{render.REMOTE_EXEC}\"，或删掉这条期望")]
    return []


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    return [cell(habit_of(exp), name_of(exp, exp.get("unit", "service")), UNKNOWN,
                 "service 期望只在 remote-exec 环境核对（先跑 bin/agent-env check）")]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    unit = shlex.quote(exp["unit"])
    if exp.get("scope", "user") == "system":
        call = f"systemctl is-active {unit}"
    elif isinstance(spec.get("remote_user"), str):
        u = shlex.quote(spec["remote_user"])
        call = (f"runuser -u {u} -- env XDG_RUNTIME_DIR=/run/user/$(id -u {u}) "
                f"systemctl --user is-active {unit}")
    else:
        call = f'XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}" systemctl --user is-active {unit}'
    frag = f'out=$({call} 2>&1)\necho "rc=$?"\necho "active=$(printf %s "$out" | head -n 1)"\n'
    if exp.get("health_url"):
        frag += _frag_health(exp)
    return frag, lambda lines: _parse(exp, remote._kv(lines), env)


def _frag_health(exp: dict) -> str:
    """curl the service's health URL on the remote host: http=<status>, and with health_json the length of that
    JSON list (count=-1 when the body is not JSON or the key is not a list). The body stays in a shell variable."""
    url = shlex.quote(exp["health_url"])
    frag = (f'body=$(curl -s -m {HEALTH_TIMEOUT} -w "\\n%{{http_code}}" {url} 2>/dev/null)\n'
            'echo "http=$(printf %s "$body" | tail -n 1)"\n')
    key = exp.get("health_json")
    if key:
        py = ("import json,sys\nraw=sys.stdin.read().rsplit(chr(10),1)[0]\n"
              "try:\n v=json.loads(raw).get(sys.argv[1])\nexcept Exception:\n v=None\n"
              "print(len(v) if isinstance(v,list) else -1)")
        frag += (f'if command -v python3 >/dev/null 2>&1; then echo "count=$(printf %s "$body" | '
                 f'python3 -c {shlex.quote(py)} {shlex.quote(key)})"; else echo "count=?"; fi\n')
    return frag


def _parse(exp: dict, kv: dict, env: str) -> dict:
    kind, layer = habit_of(exp), exp.get("layer", "host")
    unit = exp["unit"]
    name = name_of(exp, unit)
    state = kv.get("active", "").strip()
    fix = exp.get("fix") or (f"先看远程日志找原因，用户确认后再重启 {unit}（环境 {env} 的运维 skill 有命令）")
    mapped = STATES.get(state)
    if mapped == SYNCED and exp.get("health_url"):
        return _parse_health(exp, kv, kind, name, unit, fix, layer)
    if mapped == SYNCED:
        return cell(kind, name, SYNCED, f"远程服务 {unit} 在运行（active）", layer=layer)
    if mapped == DRIFT:
        return cell(kind, name, DRIFT, f"远程服务 {unit} 没在运行（{state}）", fix, layer)
    return cell(kind, name, UNKNOWN, f"远程服务 {unit} 状态无法判断（{state or '无输出'}）", layer=layer)


def _parse_health(exp, kv, kind, name, unit, fix, layer) -> dict:
    """The unit is active; the health URL decides: HTTP 200, and with health_json a non-empty list under that key."""
    url, key = exp["health_url"], exp.get("health_json")
    code, count = kv.get("http", "").strip(), kv.get("count", "").strip()
    if code == "000" or not code:
        return cell(kind, name, DRIFT, f"远程服务 {unit} 在运行，但 {url} 连不上（curl 无响应）", fix, layer)
    if code != "200":
        return cell(kind, name, DRIFT, f"远程服务 {unit} 在运行，但 {url} 返回 HTTP {code}", fix, layer)
    if not key:
        return cell(kind, name, SYNCED, f"远程服务 {unit} 在运行，{url} 返回 200", layer=layer)
    if count == "?":
        return cell(kind, name, UNKNOWN, f"{url} 返回 200，但远程没有 python3，核对不了 {key}", layer=layer)
    if count.isdigit() and int(count) > 0:
        return cell(kind, name, SYNCED, f"远程服务 {unit} 在运行，{url} 返回 200，{key} 有 {count} 条", layer=layer)
    what = "为空" if count == "0" else "不是列表或返回的不是 JSON"
    return cell(kind, name, DRIFT, f"{url} 返回 200，但 {key} {what}", fix, layer)
