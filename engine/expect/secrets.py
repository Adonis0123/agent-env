"""kind = "secrets": the secrets registry (`registry`, a profile file of names, never values) matches the host.

The registry lists one entry per line, `#` starts a comment:
  NAME          a variable that must be set in `env_file` (~/...); only `grep -q` runs, no value is read
  ~/path        a credential file that must exist (symlinks followed) with no group/other access (600)
  ~/dir/        a credential directory: itself and every directory under it 700-like, every file 600-like
`owner` (optional) is the user every registered path must belong to. Nothing here reads or returns a value.
"""
import os
import pwd
import re
import stat
from pathlib import Path

import remote
from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import expand, string_fields

KIND = "secrets"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHOW = 20  # problems listed per cell; the rest are only counted


def parse_registry(text: str):
    """(variable names, paths, bad lines) from a registry file."""
    names, paths, bad = [], [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tok = line.split()[0]
        if tok.startswith("~/"):
            paths.append(tok)
        elif NAME_RE.match(tok):
            names.append(tok)
        else:
            bad.append(tok)
    return names, paths, bad


def _read(exp: dict):
    """The registry, already resolved against the manifest's directory by habits.load_manifest."""
    return parse_registry(Path(os.path.expanduser(exp["registry"])).read_text(encoding="utf-8"))


def validate(exp: dict, target: dict, label: str) -> list:
    errs = string_fields(exp, label, ("registry", "env_file"))
    if isinstance(exp.get("env_file"), str) and not exp["env_file"].startswith("~/"):
        errs.append((f"{label}（secrets）的 env_file 不是 ~/ 开头的路径", "写成 env_file = \"~/.hermes/.env\""))
    if "owner" in exp and (not isinstance(exp["owner"], str) or not NAME_RE.match(exp["owner"].replace("-", "_"))):
        errs.append((f"{label}（secrets）的 owner 不是用户名", "写成 owner = \"hermes\"，或删掉不查属主"))
    if isinstance(exp.get("registry"), str) and exp["registry"]:
        try:
            _, _, bad = _read(exp)
        except (OSError, UnicodeDecodeError) as exc:
            errs.append((f"{label}（secrets）的 registry 读不了：{exp['registry']}（{type(exc).__name__}）",
                         "创建该登记文件，或把 registry 改成存在的路径（相对清单目录）"))
        else:
            if bad:
                errs.append((f"{label}（secrets）的登记文件有无法识别的行：{', '.join(bad[:5])}",
                             "每行写一个变量名（如 FEISHU_APP_SECRET）或 ~/ 开头的路径（目录以 / 结尾）"))
    return errs


# ── judging what was found ────────────────────────────────────────────────

def _open_bits(mode: int) -> bool:
    return bool(mode & 0o077)


def judge(exp: dict, env_file: str, env_present, names_found: dict, found: dict, children: dict,
          where: str = "远程") -> dict:
    """One cell from what the host reported.

    names_found  NAME -> bool          (empty when the env file is absent; env_present says which)
    found        registered path -> (mode, owner) or None when absent
    children     registered dir -> [(relative path, mode, owner)]
    """
    kind, layer, name = habit_of(exp), exp.get("layer", "host"), name_of(exp, "密钥")
    owner = exp.get("owner")
    absent, perms, fixes = [], [], []
    if not env_present:
        absent.append(f"{env_file} 不存在")
        fixes.append(f"在{where}恢复 {env_file}（chmod 600）")
    else:
        lost = [n for n, ok in names_found.items() if not ok]
        if lost:
            absent.append(f"{env_file} 里没有 {', '.join(lost)}")
            fixes.append(f"在 {env_file} 里补上 {lost[0]}=…（值找原发放处），或从登记文件删掉不再用的名字")

    def check_one(path: str, mode: int, who: str):
        want = 0o700 if stat.S_ISDIR(mode) else 0o600
        if _open_bits(mode):
            perms.append(f"{path} 权限 {stat.S_IMODE(mode):o}")
            fixes.append(f"chmod {want:o} {path}")
        if owner and who != owner:
            perms.append(f"{path} 属主 {who}")
            fixes.append(f"chown {owner} {path}")

    for p, info in found.items():
        if info is None:
            absent.append(f"{p} 不存在")
            fixes.append(f"把文件放回 {p}（符号链接就修指向），或从登记文件删掉这一行")
            continue
        check_one(p.rstrip("/"), *info)
        for rel, mode, who in children.get(p, []):
            check_one(f"{p.rstrip('/')}/{rel}", mode, who)

    problems = absent + perms
    if not problems:
        n_vars, n_paths = len(names_found), len(found)
        who = f"、属主 {owner}" if owner else ""
        return cell(kind, name, SYNCED, f"{where} {n_vars} 个变量名都在 {env_file}；{n_paths} 个登记路径都在，"
                                        f"权限不对组和其他人开放{who}（只查名字和权限，没读值）", layer=layer)
    more = f"（只列前 {SHOW} 个）" if len(problems) > SHOW else ""
    seen, fix = set(), []
    for f in fixes:
        if f not in seen:
            seen.add(f)
            fix.append(f)
    return cell(kind, name, MISSING if absent else DRIFT,
                f"{where} {len(problems)} 处不对{more}：{'; '.join(problems[:SHOW])}",
                "；".join(fix[:SHOW]) + "。值只在那台机器上，本仓只记名字", layer)


def _mode_from_ls(s: str):
    """`drwx------` -> st_mode bits (type + permission), or None."""
    if len(s) < 10:
        return None
    t = {"d": stat.S_IFDIR, "-": stat.S_IFREG, "l": stat.S_IFLNK}.get(s[0], stat.S_IFREG)
    bits = 0
    for i, ch in enumerate(s[1:10]):
        if ch not in "-":
            bits |= 1 << (8 - i)
    return t | bits


# ── this machine ──────────────────────────────────────────────────────────

def _owner(st) -> str:
    try:
        return pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        return str(st.st_uid)


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    names, paths, _ = _read(exp)
    envp = expand(exp["env_file"])
    env_present = envp.is_file()
    names_found = {}
    if env_present:
        lines = envp.read_text(encoding="utf-8", errors="replace").splitlines()
        for n in names:
            names_found[n] = any(re.match(rf"^(export\s+)?{n}=", ln) for ln in lines)
    found, children = {}, {}
    for p in paths:
        fp = expand(p)
        try:
            st = fp.stat()
        except OSError:
            found[p] = None
            continue
        found[p] = (st.st_mode, _owner(st))
        if p.endswith("/") and stat.S_ISDIR(st.st_mode):
            kids = []
            for top, dirs, files in os.walk(fp):
                for x in sorted(dirs) + sorted(files):
                    f = Path(top) / x
                    try:
                        cst = f.stat()
                    except OSError:
                        continue
                    kids.append((str(f.relative_to(fp)), cst.st_mode, _owner(cst)))
            children[p] = kids
    return [judge(exp, exp["env_file"], env_present, names_found, found, children, where="本机")]


# ── remote-exec host ──────────────────────────────────────────────────────

HEAD = r"""_ae_ls() { ls -ldL -- "$1" 2>/dev/null | awk '{print $1, $3}'; }
f=@ENV@
_ae_names='@NAMES@'
if [ -f "$f" ]; then echo "envfile=ok"
  for n in $_ae_names; do if grep -Eq "^(export[[:space:]]+)?$n=" "$f"; then echo "var=$n 1"; else echo "var=$n 0"; fi; done
else echo "envfile=absent"; fi
"""
PATH_FRAG = r"""p=@P@
if [ -e "$p" ]; then echo "path=@I@ $(_ae_ls "$p")"
@KIDS@else echo "path=@I@ absent"; fi
"""
KIDS = r"""  if [ -d "$p" ]; then find -L "$p" -mindepth 1 2>/dev/null | while IFS= read -r c; do
    echo "child=@I@ $(_ae_ls "$c") ${c#"$p"/}"; done; fi
"""


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    """Only `grep -q` touches the env file and only `ls -ldL` the credentials: names and modes come back."""
    names, paths, _ = _read(exp)
    frag = HEAD.replace("@ENV@", remote.shell_path(spec, exp["env_file"])).replace("@NAMES@", " ".join(names))
    for i, p in enumerate(paths):
        kids = KIDS if p.endswith("/") else ""
        frag += (PATH_FRAG.replace("@KIDS@", kids).replace("@P@", remote.shell_path(spec, p.rstrip("/")))
                 .replace("@I@", str(i)))
    frag += 'echo "done=1"\n'
    return frag, lambda lines: _parse(exp, names, paths, lines)


def _parse(exp: dict, names: list, paths: list, lines: list) -> dict:
    env_present, names_found, found, children, done = None, {}, {}, {}, False
    for line in lines:
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if k == "envfile":
            env_present = v == "ok"
        elif k == "var":
            n, _, ok = v.rpartition(" ")
            if n in names:
                names_found[n] = ok == "1"
        elif k in ("path", "child"):
            parts = v.split(" ", 3)
            if not parts[0].isdigit() or int(parts[0]) >= len(paths):
                continue
            p = paths[int(parts[0])]
            if k == "path":
                found[p] = None if parts[1:2] == ["absent"] else (
                    (_mode_from_ls(parts[1]), parts[2]) if len(parts) >= 3 and _mode_from_ls(parts[1]) else None)
            elif len(parts) == 4 and _mode_from_ls(parts[1]):
                children.setdefault(p, []).append((parts[3], _mode_from_ls(parts[1]), parts[2]))
        elif k == "done":
            done = True
    if not done or env_present is None or len(found) != len(paths):
        return cell(habit_of(exp), name_of(exp, "密钥"), UNKNOWN, "远程密钥检查没有给出结果（输出不完整）",
                    layer=exp.get("layer", "host"))
    return judge(exp, exp["env_file"], env_present, names_found, found, children)
