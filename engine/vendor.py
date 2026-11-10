"""Vendored third-party skills: a plugin may carry a third-party skill as files instead of a
`skills-lock.json` entry (plugin `vendor = ["name"]`, each name also in `skill_sources`).

Why: `npx skills experimental_install` without a ref installs the upstream's latest version and rewrites
the lock when the hash differs, and a local edit of the skill is lost either way. A vendored skill is copied
byte for byte from the local copy under [sources] skills into `skills/<name>/`, next to two generated files
that travel with the folder wherever it is copied:

  skills/<name>/UPSTREAM.md        where it came from: source, URL, path, upstream tree, local tree, modified
  skills/<name>/UPSTREAM-LICENSE   the upstream repository's license text, unchanged

and one machine-readable index at the plugin root, `vendor.json`. Both trees are git tree object ids
(what `git write-tree` gives for the folder), so the local tree equals the global lock's `skillFolderHash`
exactly when the local copy is the upstream folder as installed; `modified` is that comparison.

The license text is never fetched during a build (a build needs no network and must be byte-stable). It is
read from the licenses directory ([sources] licenses, default <state dir>/licenses), one file per upstream
repository named `<owner>-<repo>.LICENSE`. A missing file fails the build with the command that writes it:
`bin/agent-env plugin vendor-diff --plugin <p> --skill <s> --write-license` (network), which also shows
what changed upstream since the local copy was installed. Pure functions here; plugins.py wires them in.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

UPSTREAM_MD = "UPSTREAM.md"
UPSTREAM_LICENSE = "UPSTREAM-LICENSE"
INDEX = "vendor.json"
GENERATED = (UPSTREAM_MD, UPSTREAM_LICENSE)
SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
TREE_RE = re.compile(r"^[0-9a-f]{40}$")
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING")
GIT_TIMEOUT = 300


def license_file(source: str) -> str:
    """`owner/repo` → `owner-repo.LICENSE`, the file name in the licenses directory."""
    return source.replace("/", "-") + ".LICENSE"


def skill_dir_of(skill_path: str) -> str:
    """The folder of a lock entry's `skillPath` (`skills/x/SKILL.md` → `skills/x`; a bare SKILL.md → "")."""
    parts = skill_path.split("/")
    return "/".join(parts[:-1]) if parts and parts[-1] == "SKILL.md" else skill_path.rstrip("/")


def _object(kind: str, data: bytes) -> bytes:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).digest()


def tree_sha(files: dict) -> str:
    """The git tree object id of {relative POSIX path: (bytes, executable)}: blobs are 100644 or 100755,
    folders 40000, entries in git's order (a folder sorts as its name plus "/"). Equal to `git write-tree`
    of a folder holding exactly these files, and to GitHub's tree sha of the same folder upstream."""
    root: dict = {}
    for rel, (data, exe) in files.items():
        node = root
        *dirs, leaf = rel.split("/")
        for d in dirs:
            node = node.setdefault(d + "/", {})
        node[leaf] = (data, exe)

    def build(node: dict) -> bytes:
        body = b""
        for key in sorted(node, key=lambda k: k.encode("utf-8")):
            val = node[key]
            if isinstance(val, dict):
                body += b"40000 " + key[:-1].encode("utf-8") + b"\0" + build(val)
            else:
                data, exe = val
                body += (b"100755 " if exe else b"100644 ") + key.encode("utf-8") + b"\0" + _object("blob", data)
        return _object("tree", body)

    return build(root).hex()


def provenance(name: str, entry: dict, local_tree: str) -> dict:
    """One skill's record in vendor.json (keys in a fixed order)."""
    out = {"source": entry["source"]}
    if entry.get("sourceUrl"):
        out["sourceUrl"] = entry["sourceUrl"]
    out["skillPath"] = entry["skillPath"]
    out["upstreamTree"] = entry["skillFolderHash"]
    out["localTree"] = local_tree
    out["modified"] = local_tree != entry["skillFolderHash"]
    out["license"] = f"skills/{name}/{UPSTREAM_LICENSE}"
    return out


def upstream_md(name: str, rec: dict) -> bytes:
    url = rec.get("sourceUrl") or f"https://github.com/{rec['source']}"
    # no code span: a public plugin may not point at a file it does not carry (plugins.outside_paths)
    changed = ("yes: the local copy differs from the upstream folder (the plugin owner compares them with "
               f"agent-env's plugin vendor-diff command, skill {name})" if rec["modified"] else "no")
    text = (f"# Upstream of {name}\n\n"
            "This third-party skill is vendored: agent-env copied it byte for byte from the plugin owner's local\n"
            "copy, so the plugin carries it without installing anything. This file and UPSTREAM-LICENSE are\n"
            "generated; keep both with the folder when you copy it.\n\n"
            f"- Source: {rec['source']}\n"
            f"- URL: {url}\n"
            f"- Path: {skill_dir_of(rec['skillPath']) or '.'}\n"
            f"- Upstream tree: {rec['upstreamTree']} (git tree of the folder when it was installed)\n"
            f"- Local tree: {rec['localTree']}\n"
            f"- Modified locally: {changed}\n"
            f"- License: {UPSTREAM_LICENSE} (the upstream repository's license text, unchanged)\n")
    return text.encode("utf-8")


# ── upgrade path: compare the local copy with upstream HEAD (network) ─────

class VendorError(Exception):
    pass


def _git(*args, cwd=None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VendorError(f"git {args[0]} 没跑成（{type(exc).__name__}）。修复：确认装了 git、网络能连上游") from exc
    if r.returncode != 0:
        first = (r.stderr.strip().splitlines() or ["(没有输出)"])[0]
        raise VendorError(f"git {args[0]} 失败：{first}。修复：确认上游地址能访问（私有仓先配好凭证）")
    return r.stdout


def fetch_upstream(url: str, dest: Path, folder: str = "") -> Path:
    """A shallow, partial clone of the upstream's default branch into dest (a new directory) that checks
    out only the root files (the license) and the skill's folder: skill repositories can be large."""
    _git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet", url, str(dest))
    if folder:
        _git("sparse-checkout", "set", folder, cwd=dest)  # cone mode: root files are always included
    _git("checkout", "--quiet", cwd=dest)
    return dest


def upstream_license(clone: Path):
    for n in LICENSE_NAMES:
        p = clone / n
        if p.is_file():
            return p
    return None


def diff_report(name: str, entry: dict, local_dir: Path, local_tree: str, licenses: Path,
                write_license: bool, out) -> int:
    """Print how the local copy of a vendored skill compares with the lock's upstream tree and with
    upstream HEAD, then `git diff --no-index` from upstream HEAD to the local copy. With write_license,
    save the upstream license into the licenses directory when it is not there yet. 0 when the local copy
    equals upstream HEAD, 1 when they differ (the owner decides whether to update), raises on errors."""
    url = entry.get("sourceUrl") or f"https://github.com/{entry['source']}.git"
    folder = skill_dir_of(entry["skillPath"])
    with tempfile.TemporaryDirectory(prefix="agent-env-vendor-") as tmp:
        clone = fetch_upstream(url, Path(tmp) / "upstream", folder)
        head = _git("rev-parse", "HEAD", cwd=clone).strip()
        try:
            head_tree = _git("rev-parse", f"HEAD:{folder}" if folder else "HEAD^{tree}", cwd=clone).strip()
        except VendorError:
            raise VendorError(f"上游 HEAD 里没有 {folder or '.'}（skill 可能改名或挪了位置）。"
                              f"修复：看上游仓库，用 npx skills add 重新装它，再重新生成插件")
        out.write(f"skill {name}：上游 {url} 路径 {folder or '.'}\n")
        out.write(f"  上游 HEAD 提交 {head}\n")
        out.write(f"  安装时上游树 {entry.get('skillFolderHash', '(锁文件没记)')}\n")
        out.write(f"  上游 HEAD 树 {head_tree}\n")
        out.write(f"  本机副本树   {local_tree}\n")
        upstream_moved = head_tree != entry.get("skillFolderHash")
        out.write("  上游自安装后" + ("有更新" if upstream_moved else "没有变化") + "；本机副本"
                  + ("改过" if local_tree != entry.get("skillFolderHash") else "没改过") + "\n")
        lic = upstream_license(clone)
        target = licenses / license_file(entry["source"])
        if lic is None:
            out.write(f"  上游根目录没有 LICENSE 文件：许可不明，不要分发这个 skill，先问上游作者\n")
        elif write_license:
            data = lic.read_bytes()
            if target.is_file() and target.read_bytes() != data:
                out.write(f"  许可：{target.name} 已存在且和上游不同，没有替换。修复：核对后手动替换它\n")
            elif target.is_file():
                out.write(f"  许可：{target.name} 已是上游的文本\n")
            else:
                licenses.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                out.write(f"  许可：已从上游 {lic.name} 写入许可目录的 {target.name}\n")
        elif not target.is_file():
            out.write(f"  许可：许可目录还没有 {target.name}。修复：加 --write-license 从上游写入\n")
        if head_tree == local_tree:
            out.write("  本机副本和上游 HEAD 一致，无需更新\n")
            return 0
        up_dir = clone / folder if folder else clone
        shutil.copytree(up_dir, Path(tmp) / "upstream-skill", ignore=shutil.ignore_patterns(".git"), symlinks=True)
        shutil.copytree(local_dir, Path(tmp) / "local", symlinks=True)
        r = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", "upstream-skill", "local"],
                           cwd=tmp, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        out.write("  上游 HEAD → 本机副本的差异（- 只在上游，+ 只在本机）：\n")
        out.write(r.stdout if r.stdout.endswith("\n") or not r.stdout else r.stdout + "\n")
        out.write("  要更新：先备份本机副本，再把上游改动合进去（npx skills update 会整个覆盖本机的改动），"
                  "然后 bin/agent-env plugin build 重新生成；不更新就什么都不用做\n")
        return 1
