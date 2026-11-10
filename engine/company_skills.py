"""Private skill sets: one source directory, copied (never linked) into each target's skill folder.

Manifest contract:

  [sources]
  company_skills = "../company-skills"   # one directory per skill (may be nested: "<group>/<name>")

  [[targets]]
  company_skills = ["<rel>", ...]        # which source skills this target gets
  company_skills_dir = "~/..."           # the host's skill folder (remote path for a remote-exec env)
  company_skills_backup = "~/..."        # optional; default: <skills dir>/../.agent-env-backups/skills
  [[targets.expect]]
  kind = "company-skills"                # doctor: one cell per skill, content hash vs source

A skill's content hash covers every regular file except runtime folders (.venv, __pycache__,
node_modules) and .DS_Store, so a venv built on the machine survives installs and never drifts.
Install keeps every skill that is not in the source. Each install or rollback first copies the
skills it will touch into <backup>/<UTC stamp>/ (backup.list says which existed), so
`skills rollback` restores the latest backup and a second rollback undoes the first.
The same POSIX sh does the work locally and on a remote-exec host. Requires Python 3.11+.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import remote
from cells import cell

SYNCED, DRIFT, MISSING, UNKNOWN = "synced", "drift", "missing", "unknown"
RUNTIME_DIRS = (".venv", "__pycache__", "node_modules")
SKIP_FILES = (".DS_Store",)
REL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
LOCAL_TIMEOUT = 300

# Values that look like real credentials. A skill names where a secret lives, never the secret.
SECRET_PATTERNS = (
    ("Alibaba Cloud AccessKey ID", re.compile(r"\bLTAI[0-9A-Za-z]{12,30}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("secret assignment", re.compile(
        r"""(?i)\b(?:access_?key_?secret|app_?secret|client_?secret|api_?key|secret_?key|password|token)\b"""
        r"""["']?\s*[:=]\s*["']([A-Za-z0-9/+_=.-]{20,})["']""")),
)


class CompanySkillsError(Exception):
    """A failure whose message already says how to fix it (修复：...)."""


# ── source side ───────────────────────────────────────────────────────────

def source_root(manifest: dict) -> Path:
    root = manifest.get("sources", {}).get("company_skills")
    if not isinstance(root, str) or not Path(root).is_dir():
        raise CompanySkillsError(f"[sources] company_skills={root!r} 不是存在的目录。"
                                 f"修复：在清单 [sources] 写 company_skills = \"../company-skills\"（相对清单目录）")
    return Path(root)


def managed_files(path: Path) -> list:
    """Relative POSIX paths of the files a skill's content hash covers, C-locale order of './<rel>'."""
    out = []
    for top, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in RUNTIME_DIRS]
        for f in files:
            full = os.path.join(top, f)
            if f in SKIP_FILES or os.path.islink(full) or not os.path.isfile(full):
                continue
            out.append(os.path.relpath(full, path).replace(os.sep, "/"))
    return sorted(out, key=lambda r: ("./" + r).encode("utf-8"))


def tree_hash(path: Path):
    """sha256 over '<file sha256>  ./<rel>' lines; same value as the sh `_cs_hash`. None if absent."""
    if not path.is_dir() or path.is_symlink():
        return None
    listing = []
    for rel in managed_files(path):
        h = hashlib.sha256((path / rel).read_bytes()).hexdigest()
        listing.append(f"{h}  ./{rel}\n")
    return hashlib.sha256("".join(listing).encode("utf-8")).hexdigest()


def scan_secrets(path: Path) -> list:
    """['<rel>:<line> <kind>'] for values that look like credentials (text files only)."""
    hits = []
    for rel in managed_files(path):
        try:
            text = (path / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for kind, rx in SECRET_PATTERNS:
                if rx.search(line):
                    hits.append(f"{rel}:{n} {kind}")
    return hits


def source_errors(manifest: dict, rels) -> list:
    """(message, fix) pairs for a target's company_skills list against the source."""
    try:
        root = source_root(manifest)
    except CompanySkillsError as exc:
        msg = str(exc)
        return [(msg.split("修复：")[0].rstrip("。"), msg.split("修复：", 1)[1])]
    errs = []
    for rel in rels:
        if not isinstance(rel, str) or not REL_RE.match(rel) or ".." in rel.split("/"):
            errs.append((f"company_skills 里的 {rel!r} 不是合法的相对目录名",
                         "写成源目录下的相对路径，如 \"metrics-query\" 或 \"research/foo\"，不以 . 开头、不含 .."))
            continue
        d = root / rel
        if not (d / "SKILL.md").is_file() or d.is_symlink():
            errs.append((f"源 {d} 没有 SKILL.md（或是符号链接）", f"在 {root} 下建 {rel}/SKILL.md，或从列表删掉它"))
            continue
        links = [r for r, _, fs in os.walk(d) for f in fs if os.path.islink(os.path.join(r, f))]
        if links:
            errs.append((f"源 {rel} 里有符号链接，安装时不会复制", "把链接换成普通文件，或删掉"))
        hits = scan_secrets(d)
        if hits:
            errs.append((f"源 {rel} 里有疑似密钥：{'; '.join(hits[:5])}",
                         "删掉密钥值，skill 里只写凭证文件路径或环境变量名，密钥放机器本地"))
    return errs


# ── target side ───────────────────────────────────────────────────────────

def target_rels(target: dict) -> list:
    rels = target.get("company_skills")
    if not isinstance(rels, list):
        return []
    return [r for r in rels if isinstance(r, str) and REL_RE.match(r) and ".." not in r.split("/")]


def _paths(spec, target: dict):
    """(skills dir, backup dir) as absolute paths on the machine that holds them."""
    root = target.get("company_skills_dir")
    if not isinstance(root, str) or not root.startswith("~/"):
        raise CompanySkillsError(f"落点「{target.get('id')}」没有 company_skills_dir。"
                                 f"修复：在清单该落点写 company_skills_dir = \"~/.hermes/skills\"")
    backup = target.get("company_skills_backup")
    if not isinstance(backup, str):
        backup = os.path.dirname(root.rstrip("/")) + "/.agent-env-backups/skills"
    if spec is None:
        return os.path.expanduser(root), os.path.expanduser(backup)
    return remote.remote_path(spec, root), remote.remote_path(spec, backup)


SH_LIB = r"""
_cs_sha_in() { if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi; }
_cs_files() { ( cd "$1" && find . \( -name .venv -o -name __pycache__ -o -name node_modules \) -prune -o -type f ! -name .DS_Store -print ) | LC_ALL=C sort; }
_cs_hash() { ( cd "$1" && _cs_files . | while IFS= read -r f; do printf '%s  %s\n' "$(_cs_sha_in < "$f")" "$f"; done ) | _cs_sha_in; }
_cs_state() {
  if [ -L "$1" ]; then echo symlink; elif [ -d "$1" ]; then echo dir; elif [ -e "$1" ]; then echo other; else echo absent; fi
}
# _cs_sync SRC DST: DST's managed files become SRC's; runtime folders in DST stay.
_cs_sync() {
  mkdir -p "$2" || return 1
  _cs_files "$2" | while IFS= read -r f; do [ -f "$1/$f" ] || rm -f "$2/$f"; done
  _cs_files "$1" | while IFS= read -r f; do mkdir -p "$(dirname "$2/$f")" && cp -p "$1/$f" "$2/$f" || exit 1; done || return 1
  for _ in 1 2 3 4 5 6; do
    find "$2" -mindepth 1 \( -name .venv -o -name node_modules -o -name __pycache__ \) -prune -o -type d -empty -print |
      while IFS= read -r d; do rmdir "$d" 2>/dev/null; done
  done
  return 0
}
_cs_stamp() {
  ST=$(date -u +%Y%m%dT%H%M%SZ); n=1; base=$ST
  while [ -e "$BB/$ST" ]; do ST="$base-$n"; n=$((n + 1)); done
}
# _cs_backup REL: copy the managed files of $R/REL into $B, record present/absent.
_cs_backup() {
  case $(_cs_state "$R/$1") in
    dir) _cs_sync "$R/$1" "$B/$1" || { echo "ERR backup $1"; exit 4; }; echo "present $1" >> "$B/backup.list" ;;
    absent) echo "absent $1" >> "$B/backup.list" ;;
    *) echo "ERR $R/$1 不是普通目录，不覆盖"; exit 3 ;;
  esac
}
"""


def _own(owner) -> str:
    return f"chown -R {shlex.quote(owner)}: " if isinstance(owner, str) and owner else ""


def state_script(root: str, rels: list) -> str:
    """Read-only: per skill `skill <rel> <state> <hash|->`, then `found <rel>` for every installed skill."""
    q = shlex.quote
    lines = [SH_LIB, f"R={q(root)}"]
    for rel in rels:
        lines.append(f'p="$R"/{q(rel)}; s=$(_cs_state "$p"); '
                     f'if [ "$s" = dir ]; then echo "skill {rel} dir $(_cs_hash "$p")"; else echo "skill {rel} $s -"; fi')
    lines.append(r"""if [ -d "$R" ]; then
  ( cd "$R" && find . -name '.?*' -prune -o -name node_modules -prune -o -name SKILL.md -print ) | sed 's|^\./||; s|/SKILL.md$||' | while IFS= read -r d; do echo "found $d"; done
  for l in "$R"/* "$R"/*/*; do [ -L "$l" ] && [ -f "$l/SKILL.md" ] && echo "found ${l#"$R"/}"; done
fi
true""")
    return "\n".join(lines) + "\n"


def parse_state(lines) -> tuple:
    """({rel: (state, hash)}, [found rel ...])"""
    states, found = {}, []
    for line in lines:
        parts = line.split(" ")
        if parts[0] == "skill" and len(parts) == 4:
            states[parts[1]] = (parts[2], None if parts[3] == "-" else parts[3])
        elif parts[0] == "found" and len(parts) >= 2:
            found.append(line[len("found "):])
    return states, found


def conflicts(rel: str, found: list) -> list:
    """Installed skills with the same folder name elsewhere (Hermes loads both and clashes)."""
    name = rel.rsplit("/", 1)[-1]
    return sorted({f for f in found if f != rel and f.rsplit("/", 1)[-1] == name})


def _run_local(script: str, timeout=LOCAL_TIMEOUT):
    proc = subprocess.run(["sh"], input=remote.PROLOGUE + script, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run(manifest, spec, env, script):
    """(exit code, stdout) on the machine that holds the target."""
    if spec is None:
        rc, out, err = _run_local(script)
        return rc, out + (err or "")
    return remote.exec_script(manifest, spec, env, script)


def _source_hashes(manifest, rels) -> dict:
    root = source_root(manifest)
    return {rel: tree_hash(root / rel) for rel in rels}


def _install_fix(tid) -> str:
    return f"bin/agent-env skills install --target {tid}（演练，看清新增/替换哪些目录），确认后加 --yes"


def cells_from_state(target: dict, exp: dict, src: dict, states: dict, found: list, where: str) -> list:
    kind, layer = "skills", exp.get("layer", "host")
    tid = target["id"]
    out = []
    for rel, want in src.items():
        name = f"{rel}"
        if want is None:
            out.append(cell(kind, name, UNKNOWN, f"源里没有 {rel}（先跑 bin/agent-env check）", layer=layer))
            continue
        state, got = states.get(rel, (None, None))
        clash = conflicts(rel, found)
        extra = f"；另有同名 skill {', '.join(clash)}，Hermes 会冲突" if clash else ""
        if state == "absent":
            out.append(cell(kind, name, MISSING, f"{where}没有 {rel}{extra}", _install_fix(tid), layer))
        elif state == "dir" and got == want and not clash:
            out.append(cell(kind, name, SYNCED, f"{where} {rel} 与源一致，哈希 {got[:12]}", layer=layer))
        elif state == "dir" and got:
            detail = (f"{where} {rel} 与源不一致：{where} {got[:12]}，源 {want[:12]}" if got != want
                      else f"{where} {rel} 与源一致")
            fix = (f"先处理同名 skill（{', '.join(clash)}）：确认后删掉或归档其中一个；" if clash else "") + _install_fix(tid)
            out.append(cell(kind, name, DRIFT, detail + extra, fix, layer))
        elif state in ("symlink", "other"):
            out.append(cell(kind, name, DRIFT, f"{where} {rel} 是{'符号链接' if state == 'symlink' else '非目录'}，"
                            f"不是源的副本", f"确认它指向哪里，用户同意后换成目录，再 {_install_fix(tid)}", layer))
        else:
            out.append(cell(kind, name, UNKNOWN, f"读不到{where} {rel} 的状态（输出不完整）", layer=layer))
    return out


def local_cells(manifest: dict, target: dict, exp: dict) -> list:
    rels = target_rels(target)
    try:
        src = _source_hashes(manifest, rels)
        root, _ = _paths(None, target)
        rc, out = _run(manifest, None, None, state_script(root, rels))
    except (CompanySkillsError, OSError, subprocess.SubprocessError) as exc:
        return [cell("skills", "company_skills", UNKNOWN, f"核对不了：{exc}")]
    states, found = parse_state(out.splitlines())
    return cells_from_state(target, exp, src, states, found, "本机")


def remote_fragment(manifest: dict, spec: dict, target: dict, exp: dict):
    """(sh fragment, parse function) for remote.doctor_targets."""
    rels = target_rels(target)
    src = _source_hashes(manifest, rels)
    root, _ = _paths(spec, target)
    script = state_script(root, rels)

    def parse(lines):
        states, found = parse_state(lines)
        return cells_from_state(target, exp, src, states, found, "远程")
    return script, parse


# ── install / rollback ────────────────────────────────────────────────────

def plan(src: dict, states: dict, found: list) -> dict:
    p = {"add": [], "replace": [], "same": [], "blocked": []}
    for rel, want in src.items():
        state, got = states.get(rel, (None, None))
        clash = conflicts(rel, found)
        if clash:
            p["blocked"].append((rel, f"已有同名 skill {', '.join(clash)}"))
        elif state == "absent":
            p["add"].append(rel)
        elif state == "dir" and got == want:
            p["same"].append(rel)
        elif state == "dir":
            p["replace"].append(rel)
        elif state in ("symlink", "other"):
            p["blocked"].append((rel, "目标是符号链接或非目录，不覆盖"))
        else:
            p["blocked"].append((rel, "读不到目标状态"))
    return p


def apply_script(root: str, backup: str, new_dir: str, rels: list, owner=None) -> str:
    q = shlex.quote
    own = _own(owner)
    lines = [SH_LIB, f"R={q(root)}", f"BB={q(backup)}", f"N={q(new_dir)}", "_cs_stamp", 'B="$BB/$ST"',
             'mkdir -p "$B" || exit 4', ': > "$B/backup.list"', 'echo "install" > "$B/backup.kind"']
    lines += [f"_cs_backup {q(rel)}" for rel in rels]
    if own:
        lines.append(f'{own}"$BB"')
    lines.append('echo "backup=$B"')
    for rel in rels:
        lines.append(f'_cs_sync "$N"/{q(rel)} "$R"/{q(rel)} || {{ echo "ERR install {rel}"; exit 6; }}')
        if own:
            lines.append(f'{own}"$R"/{q(rel)}')
        lines.append(f'echo "installed {rel} $(_cs_hash "$R"/{q(rel)})"')
    return "\n".join(lines) + "\n"


def rollback_script(root: str, backup: str, owner=None) -> str:
    q = shlex.quote
    own = _own(owner)
    return "\n".join([SH_LIB, f"R={q(root)}", f"BB={q(backup)}", r"""
L=$(ls -1 "$BB" 2>/dev/null | grep -E '^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$' | LC_ALL=C sort | tail -n 1)
if [ -z "$L" ] || [ ! -f "$BB/$L/backup.list" ]; then echo "nobackup"; exit 7; fi
OLD="$BB/$L"
_cs_stamp
B="$BB/$ST"
mkdir -p "$B" || exit 4
: > "$B/backup.list"
echo "rollback of $L" > "$B/backup.kind"
while read -r kind rel; do _cs_backup "$rel"; done < "$OLD/backup.list"
while read -r kind rel; do
  case $kind in
    present) _cs_sync "$OLD/$rel" "$R/$rel" || { echo "ERR restore $rel"; exit 6; }
             """ + (f'{own}"$R/$rel"' if own else ":") + r"""
             echo "restored $rel $(_cs_hash "$R/$rel")" ;;
    absent) if [ -d "$R/$rel" ]; then mkdir -p "$(dirname "$B/.removed/$rel")" && mv "$R/$rel" "$B/.removed/$rel" || exit 6; fi
            echo "removed $rel" ;;
  esac
done < "$OLD/backup.list"
""" + (f'{own}"$BB"\n' if own else "") + 'echo "from=$OLD"\necho "backup=$B"\n'])


def _load_target(manifest: dict, target_id: str):
    from habits import find_target
    try:
        target = find_target(manifest, target_id)
    except KeyError:
        raise CompanySkillsError(f"清单里没有落点「{target_id}」。修复：--target 改成清单里的 id")
    if not target_rels(target):
        raise CompanySkillsError(f"落点「{target_id}」没有 company_skills。修复：在清单该落点写 company_skills = [...]")
    import render
    spec = render.remote_spec(manifest, target)
    return target, spec


def _bundle(src_root: Path, rels: list) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for rel in rels:
            for f in managed_files(src_root / rel):
                info = tar.gettarinfo(str(src_root / rel / f), arcname=f"{rel}/{f}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(src_root / rel / f, "rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


def install(manifest: dict, target_id: str, yes: bool, out, err) -> int:
    target, spec = _load_target(manifest, target_id)
    env = target.get("env")
    rels = target_rels(target)
    bad = source_errors(manifest, rels)
    if bad:
        raise CompanySkillsError("源不合格，不安装：" + "；".join(m for m, _ in bad) + "。修复：" + bad[0][1])
    src_root = source_root(manifest)
    src = _source_hashes(manifest, rels)
    root, backup = _paths(spec, target)
    try:
        rc, res = _run(manifest, spec, env, state_script(root, rels))
    except remote.RemoteError as exc:
        raise CompanySkillsError(str(exc))
    if rc != 0:
        raise CompanySkillsError(f"读目标状态失败（退出码 {rc}）：{remote._tail(res)}。修复：按输出排查后重跑")
    states, found = parse_state(res.splitlines())
    p = plan(src, states, found)
    where = f"远程 {root}" if spec else root
    print(f"公司 skill 安装 {target_id} → {where}（源 {src_root}）", file=out)
    for label, key in (("新增", "add"), ("替换", "replace"), ("保留（已一致）", "same")):
        print(f"  {label} {len(p[key])}：{', '.join(p[key]) or '-'}", file=out)
    for rel, why in p["blocked"]:
        print(f"  跳过 {rel}：{why}", file=out)
    others = sorted({f for f in found if f not in src})
    print(f"  目标上其余 {len(others)} 个 skill 不动", file=out)
    todo = p["add"] + p["replace"]
    if not yes:
        print(f"未执行（演练）。--yes 时先把要替换的目录备份到 {backup}/<时间>/，再复制源；"
              f"运行：bin/agent-env skills install --target {target_id} --yes", file=err)
        return 0
    if p["blocked"]:
        raise CompanySkillsError("有目录被跳过：" + "；".join(f"{r}（{w}）" for r, w in p["blocked"])
                                 + "。修复：先处理同名或非目录的冲突，再重跑")
    if not todo:
        print("没有要改的目录，已一致", file=out)
        return 0
    owner = spec.get("remote_user") if spec else None
    if spec is None:  # _cs_sync copies only managed files, so the source is used as is
        rc, res = _run(manifest, None, None, apply_script(root, backup, str(src_root), todo))
    else:
        rc, res = _remote_apply(manifest, spec, env, target, src_root, todo, root, backup, owner, out)
    kv = remote._kv(res.splitlines())
    installed = {ln.split(" ")[1]: ln.split(" ")[2] for ln in res.splitlines()
                 if ln.startswith("installed ") and len(ln.split(" ")) == 3}
    if rc != 0:
        raise CompanySkillsError(f"安装失败（退出码 {rc}）：{remote._tail(res)}。修复：备份在 {kv.get('backup', '?')}，"
                                 f"bin/agent-env skills rollback --target {target_id} 可恢复")
    print(f"已备份：{kv.get('backup')}", file=out)
    wrong = [r for r in todo if installed.get(r) != src[r]]
    for r in todo:
        print(f"  {'已安装' if r not in wrong else '哈希不符'} {r} {str(installed.get(r))[:12]}", file=out)
    if wrong:
        raise CompanySkillsError(f"装后哈希与源不符：{', '.join(wrong)}。修复：bin/agent-env skills rollback --target {target_id}")
    print(f"完成：{len(todo)} 个目录与源一致；撤销用 bin/agent-env skills rollback --target {target_id}", file=out)
    return 0


def _remote_apply(manifest, spec, env, target, src_root, todo, root, backup, owner, out):
    data = _bundle(src_root, todo)
    want = hashlib.sha256(data).hexdigest()
    stamp = remote.now_stamp()
    home = remote.remote_path(spec, "~/x")[:-2]
    staging = f"{home}/.cache/agent-env/incoming/skills-{stamp}"
    q = shlex.quote
    rc, res = remote.exec_script(manifest, spec, env, f"mkdir -p {q(staging)} && echo ok=1\n")
    if rc != 0 or "ok=1" not in res:
        raise CompanySkillsError(f"建暂存目录失败：{remote._tail(res)}。修复：确认登录后重跑；目标没有改动")
    size = spec.get("chunk_bytes", remote.DEFAULT_CHUNK)
    chunks = [data[i:i + size] for i in range(0, len(data), size)] or [b""]
    send = remote.resolve_cmd(manifest, spec.get("send"))
    with tempfile.TemporaryDirectory(prefix="agent-env-skills.") as tmp:
        for i, chunk in enumerate(chunks):
            name = f"part.{i:04d}"
            local = Path(tmp) / name
            local.write_bytes(chunk)
            try:
                code, _, e = remote.run(send + [str(local), staging, name], "", remote._timeout(spec))
            except (TimeoutError, OSError) as exc:
                raise CompanySkillsError(f"发送 {name} 失败：{exc}。修复：确认登录和网络后重跑；目标没有改动")
            if code != 0:
                raise CompanySkillsError(f"发送 {name} 失败（退出码 {code}）：{remote._tail(e)}。修复：确认登录后重跑；目标没有改动")
            print(f"已发送 {name}（{len(chunk)} B）", file=out)
    unpack = (f"S={q(staging)}\n"
              'cat "$S"/part.* > "$S/bundle.tgz" || exit 4\n'
              f'got=$(_ae_sha "$S/bundle.tgz"); [ "$got" = {q(want)} ] || {{ echo "sha_mismatch=$got"; exit 5; }}\n'
              'mkdir -p "$S/new" && tar -xzf "$S/bundle.tgz" -C "$S/new" || exit 4\n')
    script = unpack + apply_script(root, backup, f"{staging}/new", todo, owner) + 'rm -rf -- "$S"\n'
    return remote.exec_script(manifest, spec, env, script)


def rollback(manifest: dict, target_id: str, out) -> int:
    target, spec = _load_target(manifest, target_id)
    root, backup = _paths(spec, target)
    owner = spec.get("remote_user") if spec else None
    try:
        rc, res = _run(manifest, spec, target.get("env"), rollback_script(root, backup, owner))
    except remote.RemoteError as exc:
        raise CompanySkillsError(str(exc))
    if "nobackup" in res.split():
        raise CompanySkillsError(f"{backup} 里没有备份。修复：备份只在 skills install --yes 或 rollback 时产生")
    if rc != 0:
        raise CompanySkillsError(f"回滚失败（退出码 {rc}）：{remote._tail(res)}。修复：按输出排查；最新备份没有删")
    kv = remote._kv(res.splitlines())
    for line in res.splitlines():
        if line.startswith(("restored ", "removed ")):
            print("  " + line.replace("restored ", "已恢复 ").replace("removed ", "已移走（新增的） "), file=out)
    print(f"已从 {kv.get('from')} 恢复；恢复前的状态备份在 {kv.get('backup')}，再跑一次 rollback 可撤销", file=out)
    return 0


def main_cli(manifest: dict, action: str, target_id: str, yes: bool, out=sys.stdout, err=sys.stderr) -> int:
    if action == "install":
        return install(manifest, target_id, yes, out, err)
    if yes:
        raise CompanySkillsError("rollback 不带 --yes。修复：去掉 --yes")
    return rollback(manifest, target_id, out)
