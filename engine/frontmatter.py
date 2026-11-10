"""The scalar values of a Markdown file's YAML front matter, read as YAML reads them.

Skill limits (name <= 64, description <= 1024 characters) count the value a host parses, so every way
YAML can write a string is read here: plain (also continued on indented lines), single and double
quoted (also over several lines), and block scalars `|` / `>` with any chomping or indentation
indicator (`>+`, `|-`, `>2-`, ...). Only top-level keys are read; nested mappings and lists come back
as None. Stdlib only: the repo has no YAML dependency.

This is not a full YAML parser, so `strict=True` (what plugin builds use) refuses everything it would
have to guess at instead of reading it: anchors (&), aliases (*), tags (!), merge keys (<<), a key that
appears twice, a plain value that contains `: `, and a collection where a string is required. Every
refusal says how to rewrite the value. Without `strict` the reader stays lenient, as hosts are.
"""
import re

from habits import normalize


class FrontMatterError(ValueError):
    """The front matter cannot be read; the message says what is wrong."""


_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?$")
_BLOCK_RE = re.compile(r"^([|>])(?:([+-])([1-9])?|([1-9])([+-])?)?[ \t]*(?:#.*)?$")
_STRUCT_RE = re.compile(r"^[ \t]+(?:-(?:[ \t]|$)|[^\s#:][^:]*:(?:[ \t]|$))")
REWRITE = "改写成不带引号的单行文字，或 `key: >-` 加缩进正文的简单块；含冒号的文字用双引号包起来"
# value-start tokens in a nested mapping, list or flow collection that strict mode refuses
_NESTED_FANCY_RE = re.compile(r"""^\s*(?:-\s+)*(?:<<\s*:|(?:[^\s:#'"][^:]*:\s+)?[&*!])""")
_FLOW_FANCY_RE = re.compile(r"""(?:^|[\[{,]\s*|:\s+)(?:[&*!]|<<\s*:)""")
_PLAIN_BAD_START = ("%", "@", "`", ",", "|", ">", "- ", "? ", "-\t", "#")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
            "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ",
            "P": " "}


def block(text: str):
    """The front matter lines (between the opening `---` and the closing `---` / `...`), or None when
    the file has none. FrontMatterError when it opens but never closes."""
    lines = normalize(text).split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return lines[1:i]
    raise FrontMatterError("front matter 以 --- 开头但没有闭合的 ---")


def _fold_flow(lines: list) -> str:
    """Line folding of a plain or quoted scalar: a line break is a space, n empty lines are n newlines."""
    out, blanks = "", 0
    for i, ln in enumerate(lines):
        if not ln and 0 < i < len(lines) - 1:
            blanks += 1
            continue
        if i:
            out += "\n" * blanks if blanks else " "
        out += ln
        blanks = 0
    return out


def _fold_block(lines: list) -> str:
    """Folding of a `>` block scalar: text lines join with a space, more-indented lines keep their breaks."""
    out, blanks, prev = "", 0, None
    for ln in lines:
        if not ln.strip():
            blanks += 1
            continue
        if prev is None:
            out = "\n" * blanks + ln
        elif ln[0] in " \t" or prev[0] in " \t":
            out += "\n" * (blanks + 1) + ln
        else:
            out += "\n" * blanks + ln if blanks else " " + ln
        prev, blanks = ln, 0
    return out


def _block_scalar(m, lines: list) -> str:
    style = m.group(1)
    chomp = m.group(2) or m.group(5) or ""
    explicit = m.group(3) or m.group(4)
    body = list(lines)
    trailing = 0
    while body and not body[-1].strip():
        body.pop()
        trailing += 1
    if not body:
        return "\n" * trailing if chomp == "+" else ""
    first = next(ln for ln in body if ln.strip())
    indent = int(explicit) if explicit else len(first) - len(first.lstrip(" "))
    body = [ln[indent:] if ln.strip() else "" for ln in body]
    value = _fold_block(body) if style == ">" else "\n".join(body)
    if chomp == "-":
        return value
    return value + "\n" * (1 + trailing if chomp == "+" else 1)


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 >= len(s):
            out.append(c)
            i += 1
            continue
        n = s[i + 1]
        width = {"x": 2, "u": 4, "U": 8}.get(n)
        if width:
            digits = s[i + 2:i + 2 + width]
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                raise FrontMatterError(f"双引号字符串里的转义 \\{n}{digits} 无效")
            i += 2 + width
        else:
            out.append(_ESCAPES.get(n, n))
            i += 2
    return "".join(out)


def _quoted(quote: str, first: str, rest: list, lineno: int):
    """(value, number of `rest` lines used) for a quoted scalar starting after its opening quote."""
    pieces, used, line = [], 0, first
    while True:
        i, end = 0, None
        while i < len(line):
            c = line[i]
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and line[i + 1:i + 2] == "'":
                    i += 2
                    continue
                end = i
                break
            i += 1
        if end is not None:
            after = line[end + 1:].strip()
            if after and not after.startswith("#"):
                raise FrontMatterError(f"第 {lineno + used} 行：引号闭合之后还有内容")
            pieces.append(line[:end])
            break
        pieces.append(line)
        if used >= len(rest):
            raise FrontMatterError(f"第 {lineno} 行开始的引号没有闭合")
        line = rest[used]
        used += 1
    # the first piece keeps its leading text; later lines lose surrounding white space (YAML folding)
    lines = [pieces[0].rstrip() if len(pieces) > 1 else pieces[0]]
    lines += [p.strip() for p in pieces[1:-1]]
    if len(pieces) > 1:
        lines.append(pieces[-1].lstrip())
    if quote == "'":
        return _fold_flow(lines).replace("''", "'"), used
    merged = []
    for ln in lines:  # a backslash at the end of a line joins the next one without a space
        if merged and merged[-1].endswith("\\") and not merged[-1].endswith("\\\\"):
            merged[-1] = merged[-1][:-1] + ln
        else:
            merged.append(ln)
    return _unescape(_fold_flow(merged)), used


def body(text: str) -> str:
    """The text after the front matter (the whole text when there is none or it never closes)."""
    lines = normalize(text).split("\n")
    if not lines or lines[0].rstrip() != "---":
        return normalize(text)
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return "\n".join(lines[i + 1:])
    return normalize(text)


def _plain_ok(line: str, lineno: int) -> None:
    """Strict mode: a plain (unquoted) scalar line YAML itself would reject or read as something else."""
    if ": " in line or line.endswith(":") or ":\t" in line:
        raise FrontMatterError(f"第 {lineno} 行：不带引号的值里有「: 」，YAML 会把它读成另一个键。{REWRITE}")
    if line.startswith(_PLAIN_BAD_START):
        raise FrontMatterError(f"第 {lineno} 行：值以 YAML 的特殊符号开头。{REWRITE}")


def scalars(text: str, strict: bool = False, strings=()) -> dict:
    """{top-level key: its string value, or None for a mapping, list or flow collection}; {} when the
    file has no front matter. FrontMatterError when it cannot be read. `strict` refuses what this
    reader cannot read exactly (see the module docstring); `strings` names keys that must be strings."""
    fm = block(text)
    if fm is None:
        return {}
    out, i = {}, 0
    while i < len(fm):
        line = fm[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY_RE.match(line)
        if not m:
            if strict and line.lstrip().startswith("<<"):
                raise FrontMatterError(f"第 {i + 2} 行用了合并键 <<。{REWRITE}")
            raise FrontMatterError(f"第 {i + 2} 行不是「键: 值」")
        key, rest = m.group(1), (m.group(2) or "").strip()
        if strict and key in out:
            raise FrontMatterError(f"第 {i + 2} 行：键 {key} 出现了两次，各宿主取哪个不一定。只保留一个")
        if strict and rest[:1] in ("&", "*", "!"):
            what = {"&": "锚点 &", "*": "别名 *", "!": "标签 !"}[rest[0]]
            raise FrontMatterError(f"第 {i + 2} 行：{key} 的值用了 YAML {what}，这里不解析引用。{REWRITE}")
        if rest[:1] in ("'", '"'):
            value, used = _quoted(rest[0], rest[1:], fm[i + 1:], i + 2)
            out[key] = value
            i += 1 + used
            continue
        j = i + 1
        while j < len(fm) and (not fm[j].strip() or fm[j][0] in " \t"):
            j += 1
        follow = fm[i + 1:j]
        bm = _BLOCK_RE.match(rest)
        if bm:
            out[key] = _block_scalar(bm, follow)
        elif rest[:1] in ("[", "{"):
            if strict and (key in strings or _FLOW_FANCY_RE.search(rest[1:])
                           or any(_FLOW_FANCY_RE.search(ln.strip()) for ln in follow)):
                raise FrontMatterError(f"第 {i + 2} 行：{key} 写成了 [...] 或 {{...}}"
                                       + ("，这里要的是一段文字" if key in strings else "，里面还用了锚点、别名或标签")
                                       + f"。{REWRITE}")
            out[key] = None
        else:
            plain = re.sub(r"(^|[ \t])#.*$", "", rest).strip()
            cont = [ln.strip() for ln in follow]
            while cont and not cont[-1]:
                cont.pop()
            first = next((ln for ln in follow if ln.strip()), None)
            if not plain and first is not None and _STRUCT_RE.match(first):
                if strict:
                    if key in strings:
                        raise FrontMatterError(f"第 {i + 2} 行：{key} 是嵌套的表或列表，这里要的是一段文字。{REWRITE}")
                    for k, ln in enumerate(follow):
                        if _NESTED_FANCY_RE.match(ln):
                            raise FrontMatterError(f"第 {i + 3 + k} 行用了 YAML 锚点、别名、标签或合并键，这里不解析引用。{REWRITE}")
                out[key] = None  # a nested mapping or list
            else:
                if strict:
                    for k, ln in enumerate([plain] + cont):
                        _plain_ok(ln, i + 2 + k)
                out[key] = _fold_flow(([plain] if plain else []) + cont)
        i = j
    return out
