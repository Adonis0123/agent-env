"""kind = "link": `path` must be a symlink to `to` (both ~/...), on this machine or the remote host."""
import os
import re
import shlex
from pathlib import Path

import remote
from cells import DRIFT, MISSING, SYNCED, cell, habit_of, name_of

from .base import expand, string_fields, tilde

KIND = "link"


def validate(exp: dict, target: dict, label: str) -> list:
    return string_fields(exp, label, ("path", "to"))


def _shpath(path) -> str:
    """A path for a fix command: `~/...` when safe to leave unquoted, else quoted."""
    shown = tilde(path)
    if re.fullmatch(r"[\w./~+-]+", shown):
        return shown
    return shlex.quote(str(path))


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    path, to = expand(exp["path"]), expand(exp["to"])
    kind, name = habit_of(exp), name_of(exp, path.name)
    layer = exp.get("layer", "host")
    fix_new = f"ln -s {_shpath(to)} {_shpath(path)}"
    if path.is_symlink():
        raw = os.readlink(path)
        if os.path.realpath(path) == os.path.realpath(to):
            if path.exists():
                return [cell(kind, name, SYNCED, f"{tilde(path)} 链接到 {tilde(to)}", layer=layer)]
            return [cell(kind, name, MISSING, f"{tilde(path)} 指向的 {tilde(to)} 不存在",
                         f"确认源 {tilde(to)} 存在（源仓库已克隆、文件没被移走）", layer)]
        return [cell(kind, name, DRIFT, f"{tilde(path)} 链接到 {raw}，期望 {tilde(to)}",
                     f"ln -sfn {_shpath(to)} {_shpath(path)}", layer)]
    if path.exists():
        what = "目录" if path.is_dir() else "普通文件"
        return [cell(kind, name, DRIFT, f"{tilde(path)} 是{what}，不是指向 {tilde(to)} 的链接",
                     f"先确认里面没有要保留的改动，再 trash {_shpath(path)} && {fix_new}", layer)]
    return [cell(kind, name, MISSING, f"{tilde(path)} 不存在，期望链接到 {tilde(to)}", fix_new, layer)]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    frag = (f"p={remote.shell_path(spec, exp['path'])}\nt={remote.shell_path(spec, exp['to'])}\n"
            'if [ -L "$p" ]; then echo link=1; echo "raw=$(readlink "$p")"; '
            'echo "real=$(readlink -f "$p" 2>/dev/null)"; echo "realto=$(readlink -f "$t" 2>/dev/null)"; fi\n'
            '[ -e "$p" ] && echo exists=1\n[ -d "$p" ] && echo dir=1\ntrue\n')
    return frag, lambda lines: _parse(exp, remote._kv(lines))


def _parse(exp: dict, kv: dict) -> dict:
    kind, layer = habit_of(exp), exp.get("layer", "host")
    path, to = exp["path"], exp["to"]
    name = name_of(exp, Path(path).name)
    fix_new = f"在远程运行 ln -s {to} {path}"
    if kv.get("link") == "1":
        if kv.get("real") and kv.get("real") == kv.get("realto"):
            if kv.get("exists") == "1":
                return cell(kind, name, SYNCED, f"远程 {path} 链接到 {to}", layer=layer)
            return cell(kind, name, MISSING, f"远程 {path} 指向的 {to} 不存在", f"确认远程源 {to} 存在", layer)
        return cell(kind, name, DRIFT, f"远程 {path} 链接到 {kv.get('raw', '?')}，期望 {to}",
                    f"在远程运行 ln -sfn {to} {path}", layer)
    if kv.get("exists") == "1":
        what = "目录" if kv.get("dir") == "1" else "普通文件"
        return cell(kind, name, DRIFT, f"远程 {path} 是{what}，不是指向 {to} 的链接",
                    f"先确认里面没有要保留的改动，再在远程换成链接（{fix_new}）", layer)
    return cell(kind, name, MISSING, f"远程 {path} 不存在，期望链接到 {to}", fix_new, layer)
