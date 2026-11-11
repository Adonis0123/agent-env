"""Personal-layer manifest helpers: load the manifest, read the rules source, split rules into
`## ` sections, and assemble the rule text a target should receive.

The rules source (read_rules) is either one file, [sources] rules, or (stage 4, docs/plugins-design.md)
a directory, [sources] rules_src, that people edit: one Markdown file per owner (public.md, personal.md,
local.md). Every `## ` section lives in exactly one file; the full rules text is the sections taken as
written (trailing blank lines included) and put in [rule_sections] order, the preamble first. That
text is what every consumer reads, so splitting a file into the directory changes no output.

No user-specific paths live here; every path comes from the manifest.
Requires Python 3.11+ (tomllib).
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path


def load_manifest(path) -> dict:
    """Read a manifest and resolve [sources] paths to absolute strings.

    `~` is expanded; relative paths resolve against the manifest's directory.
    """
    manifest_path = Path(path).resolve()
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)
    sources = dict(data.get("sources", {}))
    for key, value in sources.items():
        if not isinstance(value, str):
            continue
        p = Path(os.path.expanduser(value))
        if not p.is_absolute():
            p = manifest_path.parent / p
        sources[key] = str(p)
    data["sources"] = sources
    # A target's `supplement` (profile file) resolves like a source path;
    # `output` and `skills_config` stay raw and are expanded where used.
    for target in data.get("targets", []) if isinstance(data.get("targets"), list) else []:
        if isinstance(target, dict) and isinstance(target.get("supplement"), str):
            p = Path(os.path.expanduser(target["supplement"]))
            if not p.is_absolute():
                p = manifest_path.parent / p
            target["supplement"] = os.path.normpath(p)
        # A secrets expectation's `registry` (profile file of names) resolves the same way.
        for exp in target.get("expect", []) if isinstance(target, dict) and isinstance(target.get("expect"), list) else []:
            if isinstance(exp, dict) and exp.get("kind") == "secrets" and isinstance(exp.get("registry"), str):
                p = Path(os.path.expanduser(exp["registry"]))
                if not p.is_absolute():
                    p = manifest_path.parent / p
                exp["registry"] = os.path.normpath(p)
    data[MANIFEST_PATH_KEY] = str(manifest_path)
    return data


# Key under which load_manifest records the manifest's own resolved path.
MANIFEST_PATH_KEY = "__manifest_path__"


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _env_matches(spec) -> bool:
    if not isinstance(spec, dict) or not isinstance(spec.get("platform"), str):
        return False
    if not sys.platform.startswith(spec["platform"]):
        return False
    if "wsl" in spec and bool(spec["wsl"]) != _is_wsl():
        return False
    return True


def machine_env(manifest: dict) -> str | None:
    """The first [environments.<name>] this machine matches (platform, wsl); None when none does."""
    envs = manifest.get("environments")
    return next((n for n, spec in envs.items() if _env_matches(spec)), None) if isinstance(envs, dict) else None


# CommonMark fenced code: up to 3 spaces of indent, then 3+ backticks or tildes.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fence_open(line: str):
    """Return (char, length) if the line opens a fence, else None."""
    m = _FENCE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    marker, info = m.group(1), m.group(2)
    if marker[0] == "`" and "`" in info:
        return None
    return marker[0], len(marker)


def _fence_closes(line: str, char: str, length: int) -> bool:
    """A fence closes only with the same char, length >= opening, nothing after."""
    m = _FENCE_RE.match(line.rstrip("\n"))
    if not m or m.group(2).strip():
        return False
    marker = m.group(1)
    return marker[0] == char and len(marker) >= length


def normalize(text: str) -> str:
    """Drop a leading BOM and turn CRLF / CR line endings into LF."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


TEXT, FENCE, CODE = "text", "fence", "code"


def _scan_lines(markdown_text: str):
    """Yield (lineno, line, kind) over normalized text; the single fence tracker.

    kind is FENCE for an opening/closing fence line, CODE inside a fence,
    TEXT otherwise. lineno is 1-based; line keeps its trailing newline.
    """
    fence = None  # (char, length) of the open fence
    for lineno, line in enumerate(normalize(markdown_text).splitlines(keepends=True), 1):
        if fence:
            if _fence_closes(line, *fence):
                fence = None
                yield lineno, line, FENCE
            else:
                yield lineno, line, CODE
        elif (opened := _fence_open(line)):
            fence = opened
            yield lineno, line, FENCE
        else:
            yield lineno, line, TEXT


def _is_boundary(line: str) -> bool:
    return line.startswith("## ")


# Any level-2 ATX heading, however it is written: up to 3 spaces of indent, `##`, then white space
# (a tab, a no-break or an ideographic space count too) or the end of the line.
_H2_RE = re.compile(r"^ {0,3}##(?!#)(?:\s|$)")
_H1_RE = re.compile(r"^ {0,3}#(?!#)(?:\s|$)")


def _splits(line: str, seen_section: bool) -> bool:
    """True when the line starts a new section for parse_sections. Wider than _is_boundary on purpose
    (fail closed): a heading written as `##<tab>Title`, indented, bare `##`, or a `# Title` after the
    first section never stays inside the section before it, where it would inherit that section's roles
    and visibility. suspect_headings still reports such lines so the source gets fixed."""
    return bool(_H2_RE.match(line)) or (seen_section and bool(_H1_RE.match(line)))


def _heading_of(line: str) -> str:
    return line.strip().lstrip("#").strip()


PREAMBLE_KEY = "__preamble__"


def unclosed_fence(markdown_text: str):
    """1-based line number of a code fence still open at EOF, else None."""
    open_at = None
    for lineno, _, kind in _scan_lines(markdown_text):
        if kind == FENCE:
            open_at = None if open_at else lineno
    return open_at


def _truncate_before_unclosed_fence(markdown_text: str) -> str:
    """Normalized text cut before the section holding an unclosed fence.

    If the fence opens in the preamble, nothing is kept.
    """
    fence_line = unclosed_fence(markdown_text)
    text = normalize(markdown_text)
    if fence_line is None:
        return text
    last_boundary = 0
    for lineno, line, kind in _scan_lines(text):
        if lineno >= fence_line:
            break
        if kind == TEXT and _is_boundary(line):
            last_boundary = lineno
    if last_boundary == 0:
        return ""
    return "".join(text.splitlines(keepends=True)[: last_boundary - 1])


def parse_sections(markdown_text: str):
    """Split markdown on `## ` headings outside code fences.

    Returns (preamble, [(heading, text), ...]); each text starts at its heading
    line and runs to the next heading that splits (see _splits) or EOF. The input is normalized
    first (leading BOM removed, line endings turned into LF).
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for _, line, kind in _scan_lines(markdown_text):
        if kind == TEXT and _splits(line, bool(sections)):
            sections.append((_heading_of(line), [line]))
            continue
        if sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "".join(preamble), [(h, "".join(body)) for h, body in sections]


_ATX_LOW_RE = re.compile(r"^( {0,3})(#{1,2})([ \t]|$)")
_ANY_ATX_RE = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")
_SETEXT_RE = re.compile(r"^ {0,3}(-+|=+)[ \t]*$")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])([ \t]|$)")


def _setext_can_follow(prev: str, prev_kind: str) -> bool:
    """True if `prev` is a plain paragraph line a setext underline would turn into a heading."""
    if prev_kind != TEXT or not prev.strip():
        return False
    if prev.startswith("    ") or prev.startswith("\t"):
        return False
    if _LIST_RE.match(prev) or prev.lstrip().startswith(">") or "|" in prev:
        return False
    if _ANY_ATX_RE.match(prev) or _SETEXT_RE.match(prev):
        return False
    return True


def suspect_headings(markdown_text: str) -> list[tuple[int, str]]:
    """Level <= 2 headings outside fences that parse_sections does not split on.

    Such headings would be folded into the previous section and inherit its
    roles and visibility. Returns [(1-based line number, kind), ...] where kind
    is one of: indented-atx, atx-tab, empty-h2, h1-after-sections, setext.
    A column-0 `# ` title before the first `## ` section is allowed. YAML front
    matter (line 1 `---` through the next `---`) is skipped only when closed.
    A setext `=` (h1) underline before the first `## ` section is allowed.
    An unclosed fence hides everything after it; see unclosed_fence().
    """
    found: list[tuple[int, str]] = []
    lines = normalize(markdown_text).split("\n")
    # Front matter only counts when a closing `---` exists; else scan normally.
    front_matter_end = 0
    if lines and lines[0] == "---" and "---" in lines[1:]:
        front_matter_end = lines.index("---", 1) + 1
    seen_section = False
    prev, prev_kind = "", CODE
    for lineno, line, kind in _scan_lines(markdown_text):
        body = line.rstrip("\n")
        if lineno <= front_matter_end:
            continue
        if kind == TEXT:
            if _is_boundary(line):
                seen_section = True
            else:
                m = _ATX_LOW_RE.match(body)
                if m:
                    indent, hashes, sep = m.groups()
                    if indent:
                        found.append((lineno, "indented-atx"))
                    elif hashes == "#":
                        if seen_section:
                            found.append((lineno, "h1-after-sections"))
                    elif sep == "\t":
                        found.append((lineno, "atx-tab"))
                    else:
                        found.append((lineno, "empty-h2"))
                elif _SETEXT_RE.match(body) and _setext_can_follow(prev, prev_kind):
                    # An `=` underline is an h1; like `# ` it is allowed as the title.
                    if seen_section or body.strip()[0] != "=":
                        found.append((lineno, "setext"))
        prev, prev_kind = body, kind
    return found


RULES_SRC = "rules_src"
PUBLIC_FILE = "public.md"  # the file of rules_src whose sections are registered public, and no other's


def rules_src_files(directory) -> list:
    """The Markdown files of a rules_src directory (not its subdirectories), by name."""
    return sorted((p for p in Path(directory).iterdir() if p.suffix == ".md" and p.is_file()), key=lambda p: p.name)


def join_rules_src(manifest: dict):
    """(full rules text or None, {heading: file name}, [(message, fix)]) from [sources] rules_src.

    The text is each section exactly as its file holds it, in [rule_sections] order (the preamble
    first, unregistered sections last in file order). Nothing is joined when any file fails: a heading
    in two places, a section split by an unclosed fence or a heading the parser does not see, or more
    than one file with text before its first `## `."""
    src = (manifest.get("sources") or {}).get(RULES_SRC)
    if not isinstance(src, str) or not src:
        return None, {}, [("清单 [sources] 没有 rules_src", "写 rules_src = \"~/...\"（规则源目录），或只用 rules 指向一个文件")]
    directory = Path(src)
    if not directory.is_dir():
        return None, {}, [(f"规则源目录不存在：{src}",
                           "确认 [sources] rules_src 指向存在的目录（有 sync_command 的环境由它自己的同步链接到规则源的克隆）")]
    try:
        files = rules_src_files(directory)
    except OSError as exc:
        return None, {}, [(f"规则源目录读不了：{src}（{type(exc).__name__}）", "确认目录可读")]
    if not files:
        return None, {}, [(f"规则源目录 {src} 里没有 .md 文件", "把规则按插件拆成 public.md、personal.md、local.md 放进去")]
    pieces, where, seen, errs = {}, {}, [], []
    preamble_in = None
    for f in files:
        try:
            text = normalize(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            errs.append((f"规则源 {f.name} 读不了（{type(exc).__name__}）", f"把 {f.name} 存成 UTF-8 文本"))
            continue
        fence = unclosed_fence(text)
        if fence:
            errs.append((f"规则源 {f.name} 第 {fence} 行开启的代码块没有闭合，之后的章节分不出来", "补上闭合围栏"))
            continue
        for lineno, kind in suspect_headings(text):
            errs.append((f"规则源 {f.name} 第 {lineno} 行疑似未识别的标题（{kind}），会被并进上一章节",
                         "若是标题，改成行首 `## 标题` 并在 [rule_sections] 登记；若是分隔线，在它前面加一个空行"))
        preamble, sections = parse_sections(text)
        parts = ([(PREAMBLE_KEY, preamble)] if preamble.strip() else []) + sections
        for heading, body in parts:
            if heading == PREAMBLE_KEY and preamble_in:
                errs.append((f"规则源 {preamble_in} 和 {f.name} 都在第一个 `## ` 之前写了内容，说明区只能有一份",
                             f"把 {f.name} 开头那段挪进 {preamble_in}，或删掉"))
                continue
            if heading in pieces:
                errs.append((f"章节「{heading}」在规则源里出现了两次（{where[heading]}、{f.name}）",
                             "每个章节只写在一个文件里：删掉一处，或把其中一个改成别的标题"))
                continue
            if heading == PREAMBLE_KEY:
                preamble_in = f.name
            pieces[heading], where[heading] = body, f.name
            seen.append(heading)
    if errs:
        return None, where, errs
    registry = manifest.get("rule_sections") if isinstance(manifest.get("rule_sections"), dict) else {}
    order = [PREAMBLE_KEY] if PREAMBLE_KEY in pieces else []
    order += [h for h in registry if h in pieces and h != PREAMBLE_KEY]
    order += [h for h in seen if h not in order]
    text = "".join(pieces[h] if pieces[h].endswith("\n") else pieces[h] + "\n" for h in order)
    return text, where, []


def read_rules(manifest: dict):
    """The full rules text every consumer reads: joined from [sources] rules_src when the manifest has
    one (see join_rules_src), else the file [sources] rules. None when it cannot be read or joined
    (`bin/agent-env check` says why)."""
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
    if isinstance(sources.get(RULES_SRC), str):
        text, _, errs = join_rules_src(manifest)
        return None if errs else text
    try:
        return Path(sources.get("rules")).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, TypeError):
        return None


LIMIT_UNITS = {"bytes": "B", "chars": "字符"}


def limit_unit(target: dict) -> str:
    """The unit of a target's rule_limit_bytes: "bytes" (default) or "chars"."""
    return target.get("rule_limit_unit", "bytes")


def measure(text: str, unit: str) -> int:
    """Size of text in the given unit.

    "bytes" is UTF-8 bytes. "chars" counts UTF-16 code units, which equals
    Python's len() for BMP text and is never smaller, so it is safe for hosts
    that cut by code points (Hermes, Python) or by UTF-16 units (WorkBuddy, JS).
    """
    if unit == "chars":
        return len(text.encode("utf-16-le")) // 2
    return len(text.encode("utf-8"))


def env_home(manifest: dict, env: str) -> str | None:
    """[environments.<env>] home: that environment's home directory as an absolute path, when
    the manifest records one (used to translate paths in configs written on that machine)."""
    spec = (manifest.get("environments") or {}).get(env)
    home = spec.get("home") if isinstance(spec, dict) else None
    return home.rstrip("/") if isinstance(home, str) and home.startswith("/") and home != "/" else None


def find_target(manifest: dict, target_id: str) -> dict:
    for target in manifest.get("targets", []):
        if target.get("id") == target_id:
            return target
    raise KeyError(f"unknown target: {target_id}")


def _wanted(meta, target_roles, public_only) -> bool:
    if not isinstance(meta, dict):
        return False
    roles = meta.get("roles")
    if not isinstance(roles, list) or not target_roles & set(roles):
        return False
    return not (public_only and meta.get("visibility") != "public")


def select_sections(manifest: dict, rules_text: str, target_id: str) -> str:
    """The registered parts of the rules whose roles intersect the target's roles.

    The preamble (text before the first `## `) is the pseudo-section
    PREAMBLE_KEY ("__preamble__") and follows the same rules as a section.
    A target with visibility = "public" only receives public parts.
    Original order is kept; the result is normalized (LF, no BOM).

    Unregistered sections are skipped on purpose: this is fail-closed privacy
    behavior, so a new section never leaks to a target before it is classified.
    Do not change it to include-by-default.

    Unclosed code fence: also fail-closed. Output stops before the section
    that contains the unclosed fence (nothing at all if it opens in the
    preamble), because text after it cannot be split into sections reliably.
    """
    target = find_target(manifest, target_id)
    target_roles = set(target.get("roles", []))
    public_only = target.get("visibility") == "public"
    registry = manifest.get("rule_sections", {})
    preamble, sections = parse_sections(_truncate_before_unclosed_fence(rules_text))
    parts = []
    if _wanted(registry.get(PREAMBLE_KEY), target_roles, public_only):
        parts.append(preamble)
    for heading, text in sections:
        if _wanted(registry.get(heading), target_roles, public_only):
            parts.append(text)
    return "".join(parts)
