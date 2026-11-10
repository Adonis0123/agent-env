"""kind = "contains": the file at `path` (~/...) must contain the one-line `text`."""
import shlex
from pathlib import Path

import remote
from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import expand, string_fields, tilde

KIND = "contains"


def validate(exp: dict, target: dict, label: str) -> list:
    errs = string_fields(exp, label, ("path", "text"))
    if "\n" in str(exp.get("text", "")):
        errs.append((f"{label}（contains）的 text 跨行", "只写一行要找的文字"))
    return errs


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    path, text = expand(exp["path"]), exp["text"]
    kind, name = habit_of(exp), name_of(exp, path.name)
    layer = exp.get("layer", "host")
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [cell(kind, name, MISSING, f"{tilde(path)} 不存在，期望含 `{text}`",
                     f"创建 {tilde(path)} 并写入 `{text}`", layer)]
    except (OSError, UnicodeDecodeError) as exc:
        return [cell(kind, name, UNKNOWN, f"读不了 {tilde(path)}（{type(exc).__name__}），无法核对是否含 `{text}`",
                     layer=layer)]
    if text in content:
        return [cell(kind, name, SYNCED, f"{tilde(path)} 含 `{text}`", layer=layer)]
    return [cell(kind, name, DRIFT, f"{tilde(path)} 不含 `{text}`", f"在 {tilde(path)} 里加上 `{text}`", layer)]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    p = remote.shell_path(spec, exp["path"])
    frag = (f"p={p}\n"
            'if [ -f "$p" ]; then echo state=file; '
            f"grep -qF -- {shlex.quote(exp['text'])} \"$p\"; echo grep=$?\n"
            'elif [ -e "$p" ]; then echo state=other\n'
            "else echo state=absent; fi\n")
    return frag, lambda lines: _parse(exp, remote._kv(lines))


def _parse(exp: dict, kv: dict) -> dict:
    kind, layer = habit_of(exp), exp.get("layer", "host")
    name, path, text = name_of(exp, Path(exp["path"]).name), exp["path"], exp["text"]
    if kv.get("state") == "absent":
        return cell(kind, name, MISSING, f"远程 {path} 不存在，期望含 `{text}`", f"在远程创建 {path} 并写入 `{text}`", layer)
    if kv.get("state") == "other":
        return cell(kind, name, UNKNOWN, f"远程 {path} 不是普通文件，无法核对", layer=layer)
    g = kv.get("grep")
    if g == "0":
        return cell(kind, name, SYNCED, f"远程 {path} 含 `{text}`", layer=layer)
    if g == "1":
        return cell(kind, name, DRIFT, f"远程 {path} 不含 `{text}`", f"在远程 {path} 里加上 `{text}`", layer)
    return cell(kind, name, UNKNOWN, f"远程 {path} 读不了，无法核对是否含 `{text}`", layer=layer)
