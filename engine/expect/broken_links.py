"""kind = "broken-links": no dangling symlinks under `dirs` (~/...), one filesystem each, skipping the
directory names in `exclude` (default node_modules). Same cell locally and on a remote-exec host.
"""
import os
import shlex
from pathlib import Path

import remote
from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import expand, is_str_list, tilde

KIND = "broken-links"
EXCLUDE = ("node_modules",)  # directory names a scan skips by default
SHOW = 50  # dangling links listed per cell; the rest are only counted


def validate(exp: dict, target: dict, label: str) -> list:
    errs = []
    dirs, ex = exp.get("dirs"), exp.get("exclude", [])
    if not is_str_list(dirs) or not all(d.startswith("~/") for d in dirs):
        errs.append((f"{label}（broken-links）的 dirs 不是 ~/ 开头的路径列表",
                     "写成 dirs = [\"~/.cache/tool\", ...]（整个家目录写 \"~/\"）"))
    if not isinstance(ex, list) or not all(isinstance(x, str) and x and "/" not in x for x in ex):
        errs.append((f"{label}（broken-links）的 exclude 无效", "写成要跳过的目录名列表，如 [\"node_modules\"]"))
    return errs


def exclude(exp: dict) -> list:
    ex = exp.get("exclude")
    return list(ex) if isinstance(ex, list) else list(EXCLUDE)


def make_cell(exp: dict, scanned: list, absent: list, broken: list, total: int, where: str = "远程") -> dict:
    """The cell for one broken-links expectation, shared by the remote and the local check."""
    kind, layer = habit_of(exp), exp.get("layer", "host")
    name = name_of(exp, "断链")
    skipped = f"；不存在、已跳过：{', '.join(absent)}" if absent else ""
    if not scanned:
        return cell(kind, name, MISSING, f"{where}要查的目录都不存在：{', '.join(absent) or '（清单没写 dirs）'}",
                    "确认清单这条期望的 dirs 写对了（~/ 开头），或删掉这条期望", layer)
    if total:
        more = f"（只列前 {len(broken)} 个）" if total > len(broken) else ""
        fix = exp.get("fix") or ("逐个看链接原本指向哪里（readlink <链接>），找到文件的新位置后，"
                                 "用户确认再 ln -sfn <新目标> <链接>；确实没用了就删掉链接。"
                                 "复查：find <目录> -xdev -xtype l")
        return cell(kind, name, DRIFT, f"{where} {total} 个悬空符号链接{more}：{'; '.join(broken)}{skipped}",
                    fix, layer)
    return cell(kind, name, SYNCED, f"{where} {', '.join(scanned)} 下没有悬空符号链接{skipped}", layer=layer)


# ── this machine ──────────────────────────────────────────────────────────

def scan(exp: dict) -> dict:
    """Dangling symlinks under exp["dirs"] on this machine (same filesystem, excluded names pruned)."""
    skip = set(exclude(exp))
    scanned, absent, broken, total = [], [], [], 0
    for d in exp.get("dirs", []):
        root = expand(d)
        if not root.is_dir():
            absent.append(tilde(root))
            continue
        scanned.append(tilde(root))
        dev = root.stat().st_dev
        for top, dirs, files in os.walk(root):  # does not descend into symlinked dirs
            for x in files:  # a dangling link is never a dir, so os.walk lists it here
                f = os.path.join(top, x)
                if os.path.islink(f) and not os.path.exists(f):
                    total += 1
                    if len(broken) < SHOW:
                        broken.append(f"{tilde(Path(f))} -> {os.readlink(f)}")
            dirs[:] = [x for x in dirs if x not in skip and os.lstat(os.path.join(top, x)).st_dev == dev]
    return make_cell(exp, scanned, absent, broken, total, where="本机")


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    return [scan(exp)]


# ── remote-exec host ──────────────────────────────────────────────────────

FRAG = r"""if find / -maxdepth 0 -xtype l >/dev/null 2>&1; then _ae_gnu=1; else _ae_gnu=; fi
for d in @DIRS@; do
  if [ ! -d "$d" ]; then echo "absent=$d"; continue; fi
  echo "scanned=$d"
  if [ -n "$_ae_gnu" ]; then find "$d" -xdev @PRUNE@-xtype l -print 2>/dev/null
  else find "$d" -xdev @PRUNE@-type l ! -exec test -e {} \; -print 2>/dev/null; fi
done | while IFS= read -r p; do
  case "$p" in absent=*|scanned=*) echo "$p" ;; *) printf 'broken=%s -> %s\n' "$p" "$(readlink "$p")" ;; esac
done | awk '/^broken=/ { n++; if (n > @SHOW@) next } { print } END { print "total=" n+0 }'
"""


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    """GNU `find -xtype l` when available, else a portable `test -e` per link."""
    names = exclude(exp)
    prune = (r"\( " + " -o ".join(f"-name {shlex.quote(n)}" for n in names) + r" \) -prune -o ") if names else ""
    dirs = " ".join(remote.shell_path(spec, d) for d in exp["dirs"])
    frag = FRAG.replace("@DIRS@", dirs).replace("@PRUNE@", prune).replace("@SHOW@", str(SHOW))
    return frag, lambda lines: _parse(exp, lines)


def _parse(exp: dict, lines: list) -> dict:
    scanned, absent, broken, total = [], [], [], None
    for line in lines:
        k, sep, v = line.partition("=")
        if k == "scanned" and sep:
            scanned.append(v)
        elif k == "absent" and sep:
            absent.append(v)
        elif k == "broken" and sep:
            broken.append(v)
        elif k == "total" and v.isdigit():
            total = int(v)
    if total is None:
        return cell(habit_of(exp), name_of(exp, "断链"), UNKNOWN, "远程断链检查没有给出结果（输出不完整）",
                    layer=exp.get("layer", "host"))
    return make_cell(exp, scanned, absent, broken, total)
