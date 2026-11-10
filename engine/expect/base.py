"""What the kind modules share: manifest paths on this machine, and field checks for validate()."""
import os
from pathlib import Path

import render


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def tilde(path) -> str:
    return render._tilde(path)


def is_str_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) for x in value)


def string_fields(exp: dict, label: str, fields) -> list:
    """(message, fix) for each of `fields` that is not a non-empty string; `path` and `to` must start with ~/."""
    errs = []
    for field in fields:
        v = exp.get(field)
        if not isinstance(v, str) or not v:
            errs.append((f"{label}（{exp['kind']}）缺少字符串字段 {field}", f"补上 {field} = \"...\""))
        elif field in ("path", "to") and not v.startswith("~/"):
            errs.append((f"{label} 的 {field}={v!r} 不是 ~ 开头的路径",
                         "写成 \"~/...\"，引擎运行时展开；清单里不写绝对路径"))
    return errs
