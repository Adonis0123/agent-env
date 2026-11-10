"""kind = "login": a light command (`cmd`) that succeeds only while the login to a remote host is valid.

It always runs on this machine. In a remote-exec environment it runs first and gates the round trip:
when it is not synced, nothing runs remotely and the environment error carries its `fix`.
With `relogin`, a failed probe first runs that automatic login command once and probes again (never more than once).
"""
from pathlib import Path

import remote
from cells import SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import is_str_list, string_fields

KIND = "login"
GATES_REMOTE = True
TIMEOUT = 30
RELOGIN_TIMEOUT = 420  # device-code login: CLI wait + browser confirmation


def validate(exp: dict, target: dict, label: str) -> list:
    errs = string_fields(exp, label, ("fix",))
    if not is_str_list(exp.get("cmd")):
        errs.append((f"{label}（login）的 cmd 不是非空字符串列表",
                     "写成 cmd = [\"<程序>\", \"<参数>\", ...]：一条成功即表示登录有效的轻量命令"))
    if "relogin" in exp and not is_str_list(exp.get("relogin")):
        errs.append((f"{label}（login）的 relogin 不是非空字符串列表",
                     "写成 relogin = [\"<程序>\", \"<参数>\", ...]：检查失败时自动登录一次的命令，或删掉"))
    if "relogin_timeout" in exp and (not isinstance(exp["relogin_timeout"], int) or exp["relogin_timeout"] <= 0):
        errs.append((f"{label}（login）的 relogin_timeout 不是正整数", "写成秒数，如 relogin_timeout = 420"))
    return errs


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    """A failure is `unknown` (we cannot see the host), with the command the user runs themselves as the fix.
    With `relogin`, a failed probe first runs that automatic login command once and probes again."""
    kind, layer = habit_of(exp), exp.get("layer", "host")
    name, fix = name_of(exp, "登录"), exp.get("fix", "")

    def probe():
        cmd = remote.resolve_cmd(manifest, exp.get("cmd"))
        return cmd, remote.run(cmd, "", exp.get("timeout", TIMEOUT))[0]

    try:
        cmd, rc = probe()
    except TimeoutError as exc:
        return [cell(kind, name, UNKNOWN, f"登录检查超时（{exc}），无法确认登录是否有效", fix, layer)]
    except (OSError, remote.RemoteError) as exc:
        return [cell(kind, name, UNKNOWN, f"登录检查命令起不来（{type(exc).__name__}）", fix, layer)]
    if rc == 0:
        return [cell(kind, name, SYNCED, f"登录有效（{Path(cmd[0]).name} 检查通过）", layer=layer)]
    if not exp.get("relogin"):
        return [cell(kind, name, UNKNOWN, f"登录检查失败（退出码 {rc}），多半是登录过期；请你本人重新登录", fix, layer)]
    why = ""
    try:
        lrc, _, lerr = remote.run(remote.resolve_cmd(manifest, exp["relogin"]), "",
                                  exp.get("relogin_timeout", RELOGIN_TIMEOUT))
        if lrc == 0:
            cmd, rc = probe()
        # the login command's own one-line reason, e.g. where the browser needed a person
        why = next((ln.strip() for ln in reversed(lerr.splitlines()) if "自动登录没成功" in ln), "")[:200]
    except TimeoutError:
        lrc = "超时"
    except (OSError, remote.RemoteError) as exc:
        lrc = type(exc).__name__
    if lrc == 0 and rc == 0:
        return [cell(kind, name, SYNCED, f"登录过期，已自动登录恢复（{Path(cmd[0]).name} 检查通过）", layer=layer)]
    return [cell(kind, name, UNKNOWN,
                 f"登录检查失败（退出码 {rc}），自动登录也没成功（{why or lrc}）；请你本人重新登录", fix, layer)]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text) -> list:
    return check_local(manifest, target, exp, rules_text)  # cells, not a fragment: the probe runs here
