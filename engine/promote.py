"""Promotion (P6): find what grew on the targets and may belong back in the sources.

  scan     read-only; print a deterministic candidate list (JSON)
  propose  write a Markdown draft to $XDG_STATE_HOME/agent-env/promotions/<time>.md
           with every candidate; an AI then picks the ones worth proposing,
           says where each would go and why. Nothing is merged here: merging
           waits for the user's confirmation and only edits the source repos.

Sources come from the manifest's [promotion] table:
  [[promotion.memory]]       glob, split = file | section-sign | bullets, sections, exclude
  promotion.settings_allow   settings.json globs; `permissions.allow` minus the git HEAD version
  [[promotion.rules_files]]  path (Codex prefix_rule file), optional baseline file
  [[promotion.skill_dirs]]   path, installed = [manifest files listing bundled / hub skills]
  promotion.shared_skills    the shared skill directory; skills found there are not candidates

Lines that look like secrets are never printed: the candidate keeps its source
and kind, and its text becomes 「已跳过：疑似密钥」.
No user-specific path lives here; paths come from the manifest.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import render
from state import state_dir

SKIPPED_SECRET = "已跳过：疑似密钥"
TEXT_MAX = 600
SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|private[_-]?key|cookie|auth)\w*[\"']?\s*[:=]\s*[\"']?[^\s\"']{6,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/-]{12,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b[A-Fa-f0-9]{40,}\b"
    r"|\b(?=[A-Za-z0-9+]*\d)(?=[A-Za-z0-9+]*[A-Za-z])[A-Za-z0-9+]{32,}={0,2}")


def looks_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text))


def _tilde(path) -> str:
    return render._tilde(path)


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _date(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def candidate(source, kind, text, first_seen, host="") -> dict:
    secret = looks_secret(text)
    shown = SKIPPED_SECRET if secret else _short(text)
    cid = hashlib.sha256(f"{source}\0{kind}\0{text}".encode("utf-8")).hexdigest()[:10]
    return {"id": cid, "source": source, "kind": kind, "host": host, "text": shown,
            "first_seen": first_seen, "secret_skipped": secret}


def _host_of(path: str) -> str:
    p = _tilde(path)
    m = re.match(r"~/\.([A-Za-z]+)", p)
    return m.group(1) if m else ""


# ── memory ────────────────────────────────────────────────────────────────

def _frontmatter(text: str):
    """(fields, body) from a `---` front matter block; ({}, text) when there is none."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fields = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip().strip("\"'")
    return fields, text[end + 4:].lstrip("\n")


def split_memory(text: str, mode: str, sections=None) -> list:
    """Memory entries of one file: the whole file, `§`-separated entries, or top-level bullets
    (optionally only under headings that contain one of `sections`)."""
    if mode == "file":
        fields, body = _frontmatter(text)
        head = " — ".join(x for x in (fields.get("name", ""), fields.get("description", "")) if x)
        body = " ".join(body.split())
        return [f"{head}：{body}" if head else body] if (head or body) else []
    if mode == "section-sign":
        # Entries are separated by a line holding only `§`; a `§` inside an entry is text.
        return [" ".join(e.split()) for e in re.split(r"(?m)^[ \t]*§[ \t]*$", text) if e.strip()]
    entries, current, heading = [], None, ""
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line
            if current:
                entries.append(current)
            current = None
            continue
        wanted = not sections or any(s in heading for s in sections)
        if line.startswith(("- ", "* ")) and wanted:
            if current:
                entries.append(current)
            current = line[2:].strip()
        elif current is not None and line.startswith(("  ", "\t")) and line.strip():
            current += " " + line.strip()
        elif current is not None and not line.strip():
            entries.append(current)
            current = None
    if current:
        entries.append(current)
    return entries


def scan_memory(specs) -> list:
    out = []
    for spec in specs or []:
        pattern = _expand(spec.get("glob", ""))
        exclude = set(spec.get("exclude", []))
        for path in sorted(glob.glob(pattern, recursive=True)):
            p = Path(path)
            if p.name in exclude or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for entry in split_memory(text, spec.get("split", "bullets"), spec.get("sections")):
                out.append(candidate(_tilde(p), "memory", entry, _date(p), _host_of(path)))
    return out


# ── permission allow lists ────────────────────────────────────────────────

def _git_head_version(path: Path):
    """(text, None) of the file at HEAD of its repo, or (None, why)."""
    real = Path(os.path.realpath(path))
    try:
        top = subprocess.run(["git", "-C", str(real.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=20)
        if top.returncode != 0:
            return None, "不在 git 仓库里，没有基线"
        root = Path(top.stdout.strip())
        rel = real.relative_to(root).as_posix()
        shown = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{rel}"],
                               capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, f"读不了 git 基线（{type(exc).__name__}）"
    if shown.returncode != 0:
        return None, "HEAD 里没有这个文件，没有基线"
    return shown.stdout, None


def _allow(text: str) -> list:
    data = json.loads(text)
    allow = data.get("permissions", {}).get("allow", []) if isinstance(data, dict) else []
    return [x for x in allow if isinstance(x, str)]


def scan_settings_allow(globs, notes: list) -> list:
    out, seen = [], set()
    for pattern in globs or []:
        for path in sorted(glob.glob(_expand(pattern))):
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            p = Path(path)
            try:
                current = _allow(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                notes.append(f"{_tilde(p)}：读不了（{type(exc).__name__}）")
                continue
            base_text, why = _git_head_version(p)
            if base_text is None:
                notes.append(f"{_tilde(p)}：{why}，跳过")
                continue
            try:
                base = set(_allow(base_text))
            except ValueError:
                notes.append(f"{_tilde(p)}：HEAD 版本不是合法 JSON，跳过")
                continue
            for item in current:
                if item not in base:
                    out.append(candidate(_tilde(real), "permission_allow", item, _date(p), _host_of(real)))
    return out


def rule_blocks(text: str) -> list:
    """Top-level `prefix_rule(...)` calls of a Codex rules file, comments dropped, one line each."""
    blocks, buf, depth = [], [], 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip() and depth == 0:
            continue
        buf.append(line.strip())
        depth += line.count("(") + line.count("[") - line.count(")") - line.count("]")
        if depth <= 0 and buf:
            blocks.append(" ".join(x for x in buf if x))
            buf, depth = [], 0
    if buf:
        blocks.append(" ".join(x for x in buf if x))
    return blocks


def scan_rules_files(specs, notes: list) -> list:
    out = []
    for spec in specs or []:
        p = Path(_expand(spec.get("path", "")))
        if not p.is_file():
            continue
        try:
            blocks = rule_blocks(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            notes.append(f"{_tilde(p)}：读不了，跳过")
            continue
        base = set()
        if spec.get("baseline"):
            bp = Path(_expand(spec["baseline"]))
            try:
                base = set(rule_blocks(bp.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                notes.append(f"{_tilde(p)}：基线 {_tilde(bp)} 读不了，全部按新增报")
        else:
            notes.append(f"{_tilde(p)}：没有基线，全部规则都列为候选（整个文件由宿主运行时写入）")
        for b in blocks:
            if b not in base:
                out.append(candidate(_tilde(p), "codex_rule", b, _date(p), _host_of(str(p))))
    return out


# ── host-made skills ──────────────────────────────────────────────────────

def installed_names(path: Path) -> set:
    """Skill names a host's install manifest lists (bundled, hub or app-installed)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return set()
        if isinstance(data, dict):
            for key in ("entries", "installed", "skills"):
                if isinstance(data.get(key), dict):
                    return set(data[key])
            return set(data)
        return set()
    return {line.split(":", 1)[0].strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def _skill_dirs(root: Path):
    """(name, dir) for each skill under root: a dir with SKILL.md, or one level inside a category dir.
    Symlinks are skipped: they point at a shared source."""
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            yield entry.name, entry
            continue
        for sub in sorted(entry.iterdir()):
            if not sub.name.startswith(".") and not sub.is_symlink() and (sub / "SKILL.md").is_file():
                yield sub.name, sub


def scan_skills(specs, shared, notes: list) -> list:
    out = []
    shared_dir = Path(_expand(shared)) if isinstance(shared, str) else None
    for spec in specs or []:
        root = Path(_expand(spec.get("path", "")))
        if not root.is_dir():
            continue
        skip = set()
        for name in spec.get("installed", []):
            skip |= installed_names(root / name)
        for name, d in _skill_dirs(root):
            if name in skip or (shared_dir and (shared_dir / name).exists()):
                continue
            skill_md = d / "SKILL.md"
            try:
                fields, _ = _frontmatter(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                fields = {}
            desc = fields.get("description", "")
            text = f"{name}（{_tilde(d)}）：{desc}" if desc else f"{name}（{_tilde(d)}）"
            out.append(candidate(_tilde(root), "skill", text, _date(skill_md), _host_of(str(root))))
    return out


# ── entry points ──────────────────────────────────────────────────────────

KIND_ORDER = ("memory", "permission_allow", "codex_rule", "skill")


def scan(manifest: dict) -> dict:
    cfg = manifest.get("promotion")
    if not isinstance(cfg, dict):
        raise ValueError("清单没有 [promotion] 段。修复：按 engine/promote.py 开头的说明写扫描来源")
    notes = []
    cands = (scan_memory(cfg.get("memory"))
             + scan_settings_allow(cfg.get("settings_allow"), notes)
             + scan_rules_files(cfg.get("rules_files"), notes)
             + scan_skills(cfg.get("skill_dirs"), cfg.get("shared_skills"), notes))
    unique = {}
    for c in cands:
        unique.setdefault(c["id"], c)
    ordered = sorted(unique.values(), key=lambda c: (KIND_ORDER.index(c["kind"]), c["source"], c["text"]))
    counts = {k: sum(c["kind"] == k for c in ordered) for k in KIND_ORDER}
    return {"schema": 1, "counts": counts, "secret_skipped": sum(c["secret_skipped"] for c in ordered),
            "notes": notes, "candidates": ordered}


KIND_LABELS = {"memory": "memory 条目", "permission_allow": "新增权限 allow", "codex_rule": "Codex 规则",
               "skill": "宿主自建 skill"}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def draft(result: dict, when: str) -> str:
    """The proposal draft: an empty 'proposals' part for the AI, then every candidate."""
    c = result["counts"]
    lines = [f"# 晋升提议 {when}", "",
             f"扫描到 {sum(c.values())} 条候选：" + "，".join(f"{KIND_LABELS[k]} {c[k]}" for k in KIND_ORDER)
             + f"；其中 {result['secret_skipped']} 条疑似密钥，只记来源不记原文。", "",
             "下面「提议」部分由 AI 填写：每条写提议合进哪里（AGENTS.md 哪一节 / 某个 docs / 共享 settings / "
             "共享 skill 仓库）、原文、理由。合入要等用户逐条确认；合入只改源仓库，然后走正常同步。", "",
             "## 提议", "", "（AI 填写）", "",
             "## 不提议的类别", "", "（AI 填写：哪些候选整体不提、为什么）", ""]
    if result["notes"]:
        lines += ["## 扫描说明", ""] + [f"- {n}" for n in result["notes"]] + [""]
    lines += ["## 全部候选（scan 原样）", ""]
    for kind in KIND_ORDER:
        rows = [x for x in result["candidates"] if x["kind"] == kind]
        if not rows:
            continue
        lines += [f"### {KIND_LABELS[kind]}（{len(rows)}）", "", "| id | 来源 | 首见 | 内容 |", "| --- | --- | --- | --- |"]
        lines += [f"| {x['id']} | {_cell(x['source'])} | {x['first_seen']} | {_cell(x['text'])} |" for x in rows]
        lines.append("")
    return "\n".join(lines)


def proposal_path(now=None) -> Path:
    now = now or datetime.now(timezone.utc)
    return state_dir() / "promotions" / f"{now.strftime('%Y%m%dT%H%M%SZ')}.md"


def propose(manifest: dict, now=None) -> Path:
    now = now or datetime.now(timezone.utc)
    path = proposal_path(now)
    render.atomic_write(path, draft(scan(manifest), now.strftime("%Y-%m-%d %H:%M UTC")))
    return path
