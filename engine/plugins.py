"""Plugins (插件): ordered, visibility-tagged bundles of rules and skills that a target pulls itself.
Design: docs/plugins-design.md; decision: docs/adr/0005-targets-pull-plugins.md; terms: GLOSSARY.md (Delivery).

Manifest contract:

  plugin_owner = "<name>"                     # author of the plugins (.claude-plugin/*.json); no email

  [sources]
  skills = "~/.agents/skills"                 # own skills: copied into a plugin
  skills_lock = "~/.agents/.skill-lock.json"  # `npx skills` global lock: where third-party skills come from

  [plugins.<name>]                            # name: lowercase letters, digits, hyphens (<= 64)
  visibility = "public"                       # public 公开 / personal 个人 / company 公司 / local 本机;
                                              # missing = local (never pushed)
  description = "..."
  version = "0.1.0"                           # optional
  sections = ["Communication", ...]           # `## ` headings of [sources] rules ("__preamble__" too);
                                              # a public plugin takes only sections registered public
  omit = ["，细节见 `docs/x.md`"]             # exact pieces of text (substrings, not patterns) cut from
                                              # the sections above when this plugin is generated: the
                                              # half-sentences that only hold on the author's machine
                                              # (pointers to files the plugin does not carry, personal
                                              # tooling). Each entry cuts one occurrence (write it twice
                                              # to cut two) and must be found, or nothing is generated.
                                              # A piece carries its own joining punctuation. The pieces
                                              # are never written into the plugin (see "omit" below)
  rules_file = "../profiles/<env>/patch.md"   # the plugin's own `## ` sections. A local plugin (the patch
                                              # layer) may repeat a shared heading to override it; a pushed
                                              # plugin may not carry a registered heading this way, and a
                                              # public plugin has no rules_file at all
  skills = ["name"]                           # own skills, copied from [sources] skills
  company_skills = ["group/name"]             # company skills (company or local plugins only), copied from
                                              # [sources] company_skills into skills/<last segment>/
  skill_sources = ["name"]                    # third-party skills, recorded by upstream (no copy)
  env = ["VAR"]                               # environment variable names it needs, never values
  repo = "https://..."                        # its git repository; unset until it exists
  output = "~/..."                            # optional; default <state dir>/plugins/<name>

  [[targets]]
  plugins = ["public", "personal"]            # ordered, later overrides earlier; a local plugin
                                              # (the patch layer) goes last
  token = { name = "VAR", expires = 2027-02-08 }  # cloud-hosted: the read token's name and expiry date only
  pointer_limit_chars = 400                   # cloud-hosted: longest pointer rule (指路规则)
  pull = { dir = "~/...", output = "~/..." }  # remote-exec: the host pulls the plugins itself into
                                              # <dir>/<name> and joins their AGENTS.md into output
                                              # (engine/pull.py; `deliver` installs the puller)
  rules_output = "~/.claude/AGENTS.md"        # one of the user's own machines: the plugins are composed
                                              # into this file there (engine/compose.py)

  [environments.<name>]
  transport = "cloud-hosted"                  # the target pulls plugins itself; never inferred, use --env
  driven_from = "mac"                         # the environment whose state dir holds builds and records
  deny_visibility = ["company"]               # visibilities that must never reach it

Rule of thumb for what goes where: a rule belongs to the plugin of whatever it mentions. A sentence
that names a file, a tool or a service only one machine has does not belong in a public plugin. A whole
such sentence or section goes into the patch layer, which appends to a section of the same name when
the rules are composed on the user's machine (engine/compose.py). Half a sentence cannot be appended
without changing what that machine reads, so `omit` cuts it out of a shared section when the plugin is
built, and a composed target with a patch layer keeps it. outside_paths refuses a public plugin that
still points at a file it does not carry (a personal or company plugin only gets a warning: it will
carry its own documents later).

What may enter a plugin is checked on every path (render_plugin calls the same checks `agent-env check`
runs): sections by their registered visibility, own skills without credential-store files and without
symlinks, and every file scanned as bytes. The guard fails closed: a public or personal plugin's staged
directory is also scanned with gitleaks (required for public), a file that is not UTF-8 text must be a
real image, and anything the scanners cannot read is refused. A build is byte-for-byte stable. It is written into a temp
directory beside the output under a per-output lock and swapped in with renames that roll back on any
failure, so an interrupted or concurrent build never loses the old output or its `.git`. `publish`
records each finished step (build, scan, commit, push) in a ledger, so a rerun resumes. commit mirrors
the build into a working clone of the plugin's `repo` (state dir, `plugin-repos/<name>`); push is a dry run
unless --yes, asks the host whether the repository is public first, and never force-pushes.
No user-specific path lives in this file. Requires Python 3.11+.
"""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import collation
import company_skills
import frontmatter
import render
from habits import (MANIFEST_PATH_KEY, PREAMBLE_KEY, TEXT, _scan_lines, normalize, parse_sections, rules_src_files,
                    suspect_headings, unclosed_fence)
from state import state_dir

VISIBILITY = {"public": "公开", "personal": "个人", "company": "公司", "local": "本机"}
LOCAL = "local"
GUARDED = ("public", "personal")             # leave this machine for places others may read
SECRET_SCANNED = ("public", "personal", "company")  # pushed somewhere: credentials never go in
CLOUD_HOSTED = "cloud-hosted"
REMOTE_EXEC = "remote-exec"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SKILL_NAME_MAX = 64
SKILL_DESC_MAX = 1024
POINTER_LIMIT = 400
DEFAULT_VERSION = "0.1.0"
WARN_DAYS = 7
PRESERVE = (".git",)  # kept across rebuilds once the output is a repository clone (phase 2)
LEDGER_STEPS = ("build", "scan", "commit", "push")
OLD_RENDER_FIELDS = ("render", "output", "also_output", "supplement", "skills")

# Files that store credentials are never copied into a plugin, whatever they contain. The repo had no
# such list; this mirrors Hermes' profile-distribution exclusions plus the other usual key stores.
CREDENTIAL_FILES = (".env*", "*.env", "auth.json", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*",
                    "id_ecdsa*", "id_ed25519*", ".npmrc", ".netrc", ".pypirc", ".git-credentials", "credentials*")
# The only files that are not UTF-8 text a public or personal plugin may carry: images, by extension
# (any case) and by their magic bytes. They are still scanned (see _texts).
IMAGE_MAGIC = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
               ".gif": (b"GIF87a", b"GIF89a"), ".webp": (b"RIFF",), ".ico": (b"\x00\x00\x01\x00",)}
BINARY_OK = tuple(IMAGE_MAGIC)
# Codex cuts a skill's body at 8 KB when it loads it
# (https://github.com/wshobson/agents/blob/main/docs/harnesses.md); a longer body builds, with a warning.
CODEX_SKILL_BODY_LIMIT = 8 * 1024
GITLEAKS_CONFIG = Path(__file__).resolve().parent / "gitleaks.toml"
GITLEAKS_INSTALL = "brew install gitleaks（Linux 见 https://github.com/gitleaks/gitleaks#installing）"
GITLEAKS_TIMEOUT = 300

# ── what must not leave this machine ──────────────────────────────────────
# Home directories in every spelling: macOS and Linux homes, Windows profiles (any case, either slash,
# JSON-escaped), the same seen from WSL under /mnt/<drive>, file:// URLs of any of them, /root/, ~user/.
# http(s) URLs are taken out first: a URL path is not a local path. Placeholders such as <user> or $HOME
# do not match.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
HOME_PATH_RES = (
    re.compile(r"[\\/](?:Users|home)[\\/]+[A-Za-z0-9._-]+"),
    re.compile(r"(?i)[a-z]:[\\/]+(?:users|documents and settings)[\\/]+[^\\/\s\"'<>|]+"),
    re.compile(r"(?<![\w/.~-])/root(?=[/\s\"'`]|$)"),
    re.compile(r"(?<![\w/.~\\-])~[A-Za-z_][A-Za-z0-9._-]*[\\/]"),
)
EMAIL_RE = re.compile(r"(?<![\w.+-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Secrets. Three layers: gitleaks on the staged directory (gitleaks_hits), company_skills.SECRET_PATTERNS
# (what skill installs already refuse) with TOKEN_PATTERNS, and one structural rule that does not depend
# on how the value is spelled: a sensitive key name followed, on the same line or in the lines of its
# block, by a run of 20 or more characters a credential is made of. YAML anchors, tags and block-scalar
# indicators between the key and the value are ignored; so are blank lines and comments.
_SECRET_KEY = (r"(?:api[_-]?key|token|secret|passw(?:or)?d|passwd|private[_-]?key|access[_-]?key|credential"
               r"|auth(?!ors?(?![a-z])))")
_RUN_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{20,}")
_KEY_ANY_RE = re.compile(rf"""(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.-]*{_SECRET_KEY}[A-Za-z0-9_.-]*["']?\s*[:=](.*)$""")
_KEY_START_RE = re.compile(rf"""(?i)^(\s*(?:-\s+)*(?:export\s+)?)["']?[A-Za-z0-9_.-]*{_SECRET_KEY}[A-Za-z0-9_.-]*["']?"""
                           r"""\s*[:=](.*)$""")
_YAML_DECOR_RE = re.compile(r"(?:^|(?<=\s))(?:[&*][^\s,\[\]{}]+|![^\s]*|[|>][+-]?\d*[+-]?)(?=\s|$)")
BLOCK_LINES = 100  # how many non-blank lines of a key's block are read
# token shapes the shared patterns miss or only see after a word boundary (`\b` fails right after a
# letter, a digit, `_` or any non-ASCII letter, so "令牌ghp_..." or "x_ghp_..." would slip through)
TOKEN_PATTERNS = (
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("xAI API key", re.compile(r"xai-[A-Za-z0-9]{20,}")),
)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
_ESCAPED_CHAR_RE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))")
_SAFE_SHOWN_RE = re.compile(r"^[A-Za-z0-9_./~ -]{1,64}$")


class PluginError(Exception):
    """A plugin failure whose message already says how to fix it (修复：...)."""


class NotYet(PluginError):
    """A publish step that cannot run yet (no repository, no confirmation); the run stops there and
    resumes later."""


# ── manifest access ───────────────────────────────────────────────────────

def table(manifest: dict) -> dict:
    t = manifest.get("plugins")
    return t if isinstance(t, dict) else {}


def visibility(spec) -> str:
    """A plugin's visibility id; an unmarked plugin is local (本机)."""
    v = spec.get("visibility") if isinstance(spec, dict) else None
    return v if isinstance(v, str) and v else LOCAL


def label(vis: str) -> str:
    return VISIBILITY.get(vis, vis)


def get(manifest: dict, name: str) -> dict:
    spec = table(manifest).get(name)
    if not isinstance(spec, dict):
        raise PluginError(f"清单里没有插件「{_shown(name)}」。修复：改成 {sorted(table(manifest))} 之一，"
                          f"或先在清单加 [plugins.<名字>]")
    return spec


def _base(manifest: dict) -> Path:
    return Path(manifest.get(MANIFEST_PATH_KEY, ".")).parent


def _resolve(manifest: dict, value: str) -> Path:
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else Path(os.path.normpath(_base(manifest) / p))


def output_dir(manifest: dict, name: str) -> Path:
    out = get(manifest, name).get("output")
    if isinstance(out, str) and out:
        return Path(os.path.expanduser(out))
    return state_dir() / "plugins" / name


def env_spec(manifest: dict, env) -> dict:
    envs = manifest.get("environments")
    spec = envs.get(env) if isinstance(envs, dict) else None
    return spec if isinstance(spec, dict) else {}


def is_cloud(manifest: dict, env) -> bool:
    return env_spec(manifest, env).get("transport") == CLOUD_HOSTED


def target_plugins(target: dict) -> list:
    p = target.get("plugins")
    return [x for x in p if isinstance(x, str)] if isinstance(p, list) else []


def read_lock(manifest: dict) -> dict:
    """The `skills` table of [sources] skills_lock ({} when it is not set or unreadable)."""
    path = manifest.get("sources", {}).get("skills_lock")
    if not isinstance(path, str):
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    return skills if isinstance(skills, dict) else {}


# ── secrets and private data ──────────────────────────────────────────────

def normalised(text: str) -> str:
    """`text` with the encodings a path or a token can hide in taken off: percent-encoding (up to three
    passes), HTML entities, and \\uXXXX / \\xXX / \\/ escapes. Scanners read the raw text and this."""
    out = text
    for _ in range(3):
        nxt = urllib.parse.unquote(out, errors="replace")
        if nxt == out:
            break
        out = nxt
    out = html.unescape(out)
    out = _ESCAPED_CHAR_RE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), out)
    return out.replace("\\/", "/")


def _value(text: str) -> str:
    """What follows a key, without URLs and without the YAML decorations that are not the value."""
    return _YAML_DECOR_RE.sub(" ", _URL_RE.sub(" ", text))


def secret_kinds(line: str) -> list:
    """The kinds of credential one line looks like ([] when none). The line is read as it is, with every
    non-ASCII character turned into a space (a token glued to CJK text or to a binary byte), and with
    its encodings taken off (normalised)."""
    variants = dict.fromkeys((line, _NON_ASCII_RE.sub(" ", line), normalised(line)))
    kinds = []
    for kind, rx in company_skills.SECRET_PATTERNS + TOKEN_PATTERNS:
        if any(rx.search(v) for v in variants):
            kinds.append(kind)
    if not kinds:
        for v in variants:
            m = _KEY_ANY_RE.search(v)
            if m and _RUN_RE.search(_value(m.group(1))):
                kinds.append("sensitive key with a value")
                break
    return kinds


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def multiline_secrets(text: str) -> list:
    """1-based line numbers of sensitive keys whose value is in the following lines: the lines of the
    key's block (indented more than the key, or `- ` items under it), up to an open quote's end, or
    joined by a trailing backslash. Blank lines and comment lines neither end the block nor count toward
    its BLOCK_LINES lines. A hit is a 20+ run in one line, or in the lines joined."""
    lines = text.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = _KEY_START_RE.match(line)
        if not m:
            continue
        col, rest = len(m.group(1)), m.group(2).strip()
        bare = _value(rest).strip()
        parts, taken = [], 0
        if bare[:1] in ("'", '"') and bare.count(bare[0]) % 2 == 1:  # an open quote: up to the closing one
            quote = bare[0]
            parts.append(bare[1:])
            for nxt in lines[i + 1:]:
                if taken >= BLOCK_LINES:
                    break
                if quote in nxt:
                    parts.append(nxt.split(quote, 1)[0])
                    break
                parts.append(nxt)
                taken += bool(nxt.strip())
        elif rest.endswith("\\"):  # a shell line continuation
            parts.append(rest[:-1])
            for nxt in lines[i + 1:i + 1 + BLOCK_LINES]:
                parts.append(nxt.rstrip().rstrip("\\"))
                if not nxt.rstrip().endswith("\\"):
                    break
        else:
            for nxt in lines[i + 1:]:
                if taken >= BLOCK_LINES:
                    break
                stripped = nxt.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                ind = _indent(nxt)
                if ind < col or (ind == col and not stripped.startswith("- ")):
                    break
                parts.append(nxt)
                taken += 1
        cleaned = [_value(p).strip().strip("\"'") for p in parts]
        if any(_RUN_RE.search(c) for c in cleaned) or _RUN_RE.search("".join(cleaned)):
            hits.append(i + 1)
    return hits


def looks_secret(text: str) -> bool:
    return any(secret_kinds(line) for line in str(text).splitlines())


def home_path(line: str) -> bool:
    """A home directory in `line`, as written or after its encodings are taken off (normalised)."""
    for variant in dict.fromkeys((line, normalised(line))):
        variant = _URL_RE.sub(" ", variant)
        if any(rx.search(variant) for rx in HOME_PATH_RES):
            return True
    return False


def _shown(value) -> str:
    """A manifest value for an error message. Only short, plain, non-secret text is echoed: a value put
    in the wrong field (a token as a name, a URL with a password) must never reach a log or a chat."""
    if isinstance(value, str) and _SAFE_SHOWN_RE.match(value) and not looks_secret(value) and not home_path(value):
        return value
    return "（值不显示）"


def is_credential_file(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, pat) for pat in CREDENTIAL_FILES)


def is_image(rel: str, data: bytes) -> bool:
    """An allowed image: its extension (any case) is on the list and its first bytes are that type's."""
    ext = Path(rel).suffix.lower()
    magic = IMAGE_MAGIC.get(ext)
    if not magic or not data.startswith(magic):
        return False
    return ext != ".webp" or data[8:12] == b"WEBP"


def _texts(data: bytes):
    """(texts to scan, is binary). UTF-8 text without NUL bytes is read as it is. Anything else is
    binary and is read three ways, Latin-1, UTF-16-LE and UTF-16-BE, so an ASCII or UTF-16 token or
    path inside it still shows whatever the file claims to be."""
    try:
        text = data.decode("utf-8")
        if "\0" not in text:
            return [text], False
    except UnicodeDecodeError:
        pass
    return [data.decode("latin-1"), data.decode("utf-16-le", errors="replace"),
            data.decode("utf-16-be", errors="replace")], True


def line_key(rel: str, line: str) -> str:
    """`<rel>#<12 hex>`: one reviewed line of a company plugin (privacy_reviewed). The hash covers the line
    text, so an edited line is flagged again; it is short, so it says nothing about the line's content."""
    return f"{rel}#{hashlib.sha256(line.strip().encode('utf-8')).hexdigest()[:12]}"


def privacy_hits(files: dict, vis: str, reviewed=()) -> list:
    """['<rel>:<line> <what>'] for content that must not leave this machine at this visibility: secrets
    in anything pushed (public, personal, company), plus home paths and emails in public and personal
    plugins. There a file that is not UTF-8 text must be a real image, and is scanned for secrets and
    home paths all the same. File names are checked like content.
    `reviewed` (company plugins only, [plugins.<name>] privacy_reviewed) lists line_key()s a person checked
    and found to hold no secret value (a variable name, a call): the one-line secret patterns skip exactly
    those lines, every other check still runs, and a hit on any other line names its key for review."""
    if vis not in SECRET_SCANNED:
        return []
    reviewed = set(reviewed) if vis == "company" else set()
    guarded = vis in GUARDED
    hits = []
    for rel, (data, _) in files.items():
        texts, binary = _texts(data)
        if any(is_credential_file(part) for part in rel.split("/")):
            hits.append(f"{rel} 凭证文件")
        if looks_secret(rel):
            hits.append(f"{rel} 文件名疑似密钥")
        if guarded and (home_path("/" + rel) or EMAIL_RE.search(rel)):
            hits.append(f"{rel} 文件名里有家目录路径或邮箱")
        if binary and guarded and not is_image(rel, data):
            hits.append(f"{rel} 不是 UTF-8 文本，也不是允许的图片（扩展名 {list(BINARY_OK)} 且文件头相符）")
        found = []
        for text in texts:
            for n, line in enumerate(text.splitlines(), 1):
                where = f"{rel}:{n}" if not binary else rel
                if guarded:
                    if home_path(line):
                        found.append(f"{where} 绝对家目录路径")
                    if not binary and EMAIL_RE.search(line):
                        found.append(f"{where} 邮箱")
                kinds = secret_kinds(line)
                if kinds and not binary and vis == "company":
                    key = line_key(rel, line)
                    if key in reviewed:
                        continue
                    kinds = [f"{k}；确认不是密钥值后可登记 privacy_reviewed：{key}" for k in kinds]
                for kind in kinds:
                    found.append(f"{where} 疑似密钥（{kind}）")
            found += [f"{rel}:{n}" + " 疑似密钥（多行的值）" if not binary else f"{rel} 疑似密钥（多行的值）"
                      for n in multiline_secrets(text)]
        hits += list(dict.fromkeys(found))
    return hits


# ── paths that point outside the plugin ───────────────────────────────────
# A pushed plugin is read on machines that have the plugin and nothing else, so "see `docs/x.md`" is a
# dead pointer there. The check reads the files written as prose (PROSE_SUFFIXES; in a script a backtick
# is code), skips fenced code blocks, and looks at two places only: inline code spans and link targets.
#
# In a code span each word (split on whitespace, quotes, `=`, `,`, `;`, parentheses) is a path when
#   1. it is `~` or starts with `~/`: a home path is never inside a plugin; or
#   2. it starts with `../`: it leaves the directory of the file it is written in; or
#   3. it contains `/`, does not start with `/`, and its first segment is one of KNOWN_DIRS
#      (`docs/`, `scripts/x.zsh`, `bin/tool`) or its last segment ends in a Markdown extension
#      (`profiles/mac/README.md`).
# Not a path, by rule and not by guess:
#   - a word without `/` (`.env`, `a.png`, `AGENTS.md`, `settings.json`, `git`): a bare name says what a
#     file is called, not where it is;
#   - a URL (`https://...`) or an scp-style remote (`git@host:owner/repo.git`);
#   - a word that starts with `/` (`/tmp`, `/usr/bin/trash`, `/permissions`): the same on every machine,
#     or a slash command. A home directory spelled out is the privacy scan's job (HOME_PATH_RES);
#   - a word with a placeholder, a variable or a glob in it (one of `<>$*{}`): `skills/<name>/SKILL.md`,
#     `$REPO_ROOT/docs/x.md`. There is nothing to resolve. (`~/...` is rule 1 whatever follows.)
#   - anything else with a slash: `owner/repo`, `feat/<slug>`, `and/or`, `.claude/settings.json`,
#     `src/index.ts`. These name things in the reader's own project as often as in the author's, so
#     they pass. A directory of the author's that keeps slipping through is added to KNOWN_DIRS.
# A link target (`[text](target)`, `[id]: target`) is a path unless it has a scheme (`https:`,
# `mailto:`), is an anchor (`#...`), starts with `/` or holds a placeholder; a bare file name counts
# here, because a link always says where.
# A path passes when it names a file or a directory of this plugin, read from the directory of the file
# it is written in or from the plugin root.
PROSE_SUFFIXES = (".md", ".mdx", ".markdown", ".json")
MARKDOWN_SUFFIXES = (".md", ".mdx", ".markdown")
KNOWN_DIRS = ("docs", "scripts", "bin", "skills", "references", "assets", "tests", "hooks")
OUTSIDE_WARNED = ("personal", "company")     # same check as public, reported instead of refused
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_WORD_SPLIT_RE = re.compile(r"""[\s="'(),;]+""")
_SCP_REMOTE_RE = re.compile(r"^[\w.-]+@[\w.-]+:")
_LINE_REF_RE = re.compile(r"(?::\d+)+$")
_LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)")
_LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_UNRESOLVABLE = "<>$*{}"


def _span_path(word: str):
    """The path a word of a code span names, or None (rules above)."""
    if "://" in word or _SCP_REMOTE_RE.match(word):
        return None
    word = _LINE_REF_RE.sub("", word).rstrip(".:")
    if word == "~" or word.startswith("~/") or word.startswith("../"):
        return word
    if "/" not in word or word.startswith("/") or any(c in word for c in _UNRESOLVABLE):
        return None
    bare = word[2:] if word.startswith("./") else word
    if bare.split("/", 1)[0] in KNOWN_DIRS or bare.split("#", 1)[0].lower().endswith(MARKDOWN_SUFFIXES):
        return word
    return None


def _link_path(target: str):
    """The path a link target names, or None (rules above)."""
    if _SCHEME_RE.match(target) or target.startswith(("#", "/")) or any(c in target for c in _UNRESOLVABLE):
        return None
    return target


def _in_plugin(path: str, rel: str, tree: set) -> bool:
    if path == "~" or path.startswith("~/"):
        return False
    path = path.split("#", 1)[0].split("?", 1)[0]
    for base in (posixpath.dirname(rel), ""):
        p = posixpath.normpath(posixpath.join(base, path))
        if p == "." or p in tree:
            return True
    return False


def outside_paths(files: dict) -> list:
    """[(file, line, path)] for every path in a prose file of `files` that is not a file or directory of
    the plugin itself. See the rules above; the same input always gives the same list."""
    tree = set()
    for rel in files:
        parts = rel.split("/")
        tree.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    hits = []
    for rel, (data, _) in files.items():
        if not rel.lower().endswith(PROSE_SUFFIXES):
            continue
        texts, binary = _texts(data)
        if binary:
            continue
        for n, line, kind in _scan_lines(texts[0]):
            if kind != TEXT:
                continue
            found = []

            def span(m):
                found.extend(map(_span_path, _WORD_SPLIT_RE.split(m.group(2))))
                return " "
            rest = _CODE_SPAN_RE.sub(span, line)
            for m in list(_LINK_RE.finditer(rest)) + list(_LINK_DEF_RE.finditer(rest)):
                found.append(_link_path(m.group(1)))
            hits += [(rel, n, p) for p in dict.fromkeys(filter(None, found)) if not _in_plugin(p, rel, tree)]
    return hits


def _outside_shown(hits: list, limit: int) -> str:
    return "；".join(f"{rel}:{n} `{_shown(p)}`" for rel, n, p in hits[:limit]) + (" 等" if len(hits) > limit else "")


def _outside_fix(name: str) -> str:
    return (f"规则里的句子：在清单 [plugins.{name}] 的 omit 里写上带这条路径的那半句原文（连同它前后的连接标点），"
            f"生成时去掉；skill 或 rules_file 里的句子改原文；读者确实要看的文档就放进插件"
            f"（现在只能放进自写 skill 的目录），再用相对路径引用")


def outside_notes(name: str, vis: str, files: dict) -> list:
    """The warning a personal or company plugin gets for paths that point outside it (a public plugin
    is refused in render_plugin; a local one is not checked: it never leaves this machine)."""
    hits = outside_paths(files) if vis in OUTSIDE_WARNED else []
    if not hits:
        return []
    return [f"提醒：{label(vis)}插件「{name}」有 {len(hits)} 处指向插件外的文件（{_outside_shown(hits, 3)}），"
            f"只拿到插件的机器打不开；公开插件遇到同样的情况会拒绝生成。修复：{_outside_fix(name)}"]


# ── gitleaks: the same scanner the public repositories already use ─────────

def gitleaks_exe():
    return shutil.which("gitleaks")


def gitleaks_hits(root: Path):
    """['<rel>:<line> gitleaks:<rule>'] for a staged plugin directory; None when gitleaks is not
    installed. Values are never printed (--redact). `gitleaks:allow` comments in the content do not
    switch a finding off, and the rules are pinned to engine/gitleaks.toml."""
    exe = gitleaks_exe()
    if not exe:
        return None
    cmd = [exe, "dir", str(root), "--no-banner", "--redact", "--exit-code", "1", "--report-format", "json",
           "--report-path", "-", "--log-level", "error", "--ignore-gitleaks-allow", "--config", str(GITLEAKS_CONFIG)]
    try:
        proc = subprocess.run(cmd, capture_output=True, cwd=str(root), timeout=GITLEAKS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginError(f"gitleaks 没跑成（{type(exc).__name__}），不能确认没有密钥，这次不写入。"
                          f"修复：先单独跑 gitleaks version 确认它可用，再重跑")
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        tail = proc.stderr.decode("utf-8", errors="replace").strip()[-200:]
        raise PluginError(f"gitleaks 出错（退出码 {proc.returncode}）：{tail}。"
                          f"修复：先单独跑 gitleaks version 确认它可用，再重跑")
    try:
        leaks = json.loads(proc.stdout.decode("utf-8", errors="replace") or "[]")
    except ValueError:
        return ["gitleaks 报告读不了（按有密钥处理）"]
    base = os.path.realpath(root)
    out = set()
    for leak in leaks if isinstance(leaks, list) else []:
        full = os.path.realpath(str(leak.get("File", "")))
        rel = os.path.relpath(full, base) if full.startswith(base + os.sep) else "?"
        out.add(f"{rel.replace(os.sep, '/')}:{leak.get('StartLine', '?')} gitleaks:{leak.get('RuleID', '?')}")
    return sorted(out) or ["gitleaks 报告有命中但读不出位置（按有密钥处理）"]


def guard_staged(name: str, vis: str, staged: Path) -> list:
    """Scan a staged plugin directory with gitleaks before it replaces the output. Public and personal
    plugins only. Returns notes to print; raises PluginError on a finding, and for a public plugin when
    gitleaks is not installed (a personal plugin gets a warning then: it stays in a private repository)."""
    if vis not in GUARDED:
        return []
    hits = gitleaks_hits(staged)
    if hits is None:
        if vis == "public":
            raise PluginError(f"公开插件「{name}」要先过 gitleaks 扫描，本机没装 gitleaks，这次不写入。"
                              f"修复：{GITLEAKS_INSTALL}，装好后重跑")
        return [f"提醒：本机没装 gitleaks，个人插件「{name}」只过了内置扫描。修复：{GITLEAKS_INSTALL}"]
    if hits:
        raise PluginError(f"插件「{name}」（{label(vis)}）没过 gitleaks 扫描：{'；'.join(hits[:5])}"
                          + (f" 等 {len(hits)} 处" if len(hits) > 5 else "")
                          + "。修复：删掉这些位置的密钥值（示例值写成 <占位符>），已经泄露过的去签发处作废重建；改完重跑")
    return []


# ── validation (habits_check, and again before anything is generated) ─────

def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def front_matter(skill_md: Path) -> dict:
    """`name` and `description` of a SKILL.md as YAML reads them (frontmatter.FrontMatterError if broken)."""
    fields = frontmatter.scalars(skill_md.read_text(encoding="utf-8"), strict=True, strings=("name", "description"))
    return {k: fields[k].strip() if isinstance(fields.get(k), str) else "" for k in ("name", "description")}


def skill_limit_errors(skill_dir: Path) -> list:
    """(message, fix) pairs for the Agent Skills limits (agentskills.io/specification)."""
    md = skill_dir / "SKILL.md"
    try:
        fm = front_matter(md)
    except (OSError, UnicodeDecodeError) as exc:
        return [(f"skill「{skill_dir.name}」的 SKILL.md 读不了（{type(exc).__name__}）",
                 f"确认 {render._tilde(md)} 存在且是 UTF-8")]
    except frontmatter.FrontMatterError as exc:
        return [(f"skill「{skill_dir.name}」的 SKILL.md front matter 这里不认：{exc}",
                 f"{frontmatter.REWRITE}（name 和 description 各写一次，都是一段文字）")]
    errs = []
    name, desc = fm["name"], fm["description"]
    if not name:
        errs.append((f"skill「{skill_dir.name}」的 SKILL.md 没有 name", f"在开头的 front matter 写 name: {skill_dir.name}"))
    elif len(name) > SKILL_NAME_MAX:
        errs.append((f"skill「{skill_dir.name}」的 name 有 {len(name)} 字符，超过 {SKILL_NAME_MAX}",
                     f"把 name 缩到 {SKILL_NAME_MAX} 字符以内（目录名同步改）"))
    elif name != skill_dir.name:
        errs.append((f"skill「{skill_dir.name}」的 name 和目录名不一致",
                     "Agent Skills 规范要求两者相同：改 name 或改目录名"))
    if not desc:
        errs.append((f"skill「{skill_dir.name}」的 SKILL.md 没有 description", "在 front matter 写一句 description，说清做什么、何时用"))
    elif len(desc) > SKILL_DESC_MAX:
        errs.append((f"skill「{skill_dir.name}」的 description 有 {len(desc)} 字符，超过 {SKILL_DESC_MAX}",
                     f"把 description 精简到 {SKILL_DESC_MAX} 字符以内，细节挪进正文"))
    return errs


def company_skill_names(manifest: dict) -> set:
    """Names of company skills: every directory of [sources] company_skills and every entry of a
    target's company_skills list. They may only enter a company or local plugin."""
    names = set()
    for t in manifest.get("targets", []) or []:
        rels = t.get("company_skills") if isinstance(t, dict) else None
        for rel in rels if isinstance(rels, list) else []:
            if isinstance(rel, str) and rel:
                names |= {rel, rel.rsplit("/", 1)[-1]}
    root = manifest.get("sources", {}).get("company_skills")
    try:
        if isinstance(root, str) and Path(root).is_dir():
            names |= {d.name for d in Path(root).iterdir() if d.is_dir()}
    except OSError:
        pass
    return names


def _same_file(a: Path, b) -> bool:
    try:
        return isinstance(b, str) and os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _rules_file_errors(manifest: dict, lbl: str, spec: dict, vis: str) -> list:
    """A rules_file is the plugin's own text. Shared rule sections reach a pushed plugin only through
    `sections`, where their registered visibility is checked; this closes the other way in."""
    rf = spec.get("rules_file")
    if not isinstance(rf, str):
        return []
    if vis == "public":
        return [(f"{lbl} 是公开插件却写了 rules_file",
                 "公开插件的规则只能来自 sections 里登记为 public 的章节：删掉 rules_file，"
                 "要公开的内容先写进规则源并在 [rule_sections] 登记为 public")]
    path = _resolve(manifest, rf)
    if not path.is_file():
        return [(f"{lbl} 的 rules_file 不存在", "创建该文件（相对清单目录），或删掉 rules_file")]
    errs = []
    try:
        text = normalize(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return [(f"{lbl} 的 rules_file 读不了（{type(exc).__name__}）", "存成 UTF-8 文本并确认可读")]
    if unclosed_fence(text):
        errs.append((f"{lbl} 的 rules_file 有没闭合的代码块", "补上闭合围栏"))
    for lineno, kind in suspect_headings(text):
        errs.append((f"{lbl} 的 rules_file 第 {lineno} 行疑似未识别的标题（{kind}）", "标题改成行首 `## 标题`"))
    if vis == LOCAL:
        return errs  # the patch layer repeats shared headings on purpose, and never leaves this machine
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
    src_dir = sources.get("rules_src")
    try:
        src_files = rules_src_files(src_dir) if isinstance(src_dir, str) and Path(src_dir).is_dir() else []
    except OSError:
        src_files = []
    if _same_file(path, sources.get("rules")) or any(_same_file(path, str(f)) for f in src_files):
        errs.append((f"{lbl} 的 rules_file 就是规则源本身，会把没分配给它的章节全带出去",
                     "删掉 rules_file，在 sections 里逐个列出要带的章节"))
    registry = manifest.get("rule_sections") if isinstance(manifest.get("rule_sections"), dict) else {}
    for heading, _ in parse_sections(text)[1]:
        if heading in registry:
            errs.append((f"{lbl} 的 rules_file 带了已登记的共享章节「{_shown(heading)}」，绕过了章节的可见性登记",
                         "共享章节只能写进 sections；rules_file 里只放这个插件自己的章节，标题别和共享章节重名"))
    return errs


def plugin_errors(manifest: dict, name: str, spec) -> list:
    """(message, fix) pairs for one [plugins.<name>] table. Messages never echo a value that could be a
    secret (see _shown)."""
    lbl = f"[plugins.{_shown(name)}]"
    if not isinstance(spec, dict):
        return [(f"{lbl} 不是表", f"写成 {lbl} 表，带 visibility、sections、skills 等字段")]
    errs = []
    known = {"visibility", "description", "version", "sections", "omit", "rules_file", "skills", "skill_sources",
             "company_skills", "privacy_reviewed", "env", "repo", "output"}
    extra = sorted(k for k in set(spec) - known)
    if extra:
        errs.append((f"{lbl} 有未知字段 {[_shown(k) for k in extra]}", f"只用 {sorted(known)}；密钥值不要写进清单"))
    if not NAME_RE.match(name) or len(name) > SKILL_NAME_MAX:
        errs.append((f"插件名「{_shown(name)}」不合法", "只用小写字母、数字和连字符，64 字符以内（Claude Code 插件名的要求）"))
    vis = spec.get("visibility")
    if vis is not None and vis not in VISIBILITY:
        errs.append((f"{lbl} 的 visibility 无效（现在是 {_shown(vis)}）",
                     "写成 " + " / ".join(f"\"{k}\"（{v}）" for k, v in VISIBILITY.items()) + "，不写按本机处理"))
    vis = visibility(spec)
    for key in ("description", "version", "repo", "output", "rules_file"):
        if key in spec and (not isinstance(spec[key], str) or not spec[key]):
            errs.append((f"{lbl} 的 {key} 不是非空字符串", f"写成 {key} = \"...\"，用不到就删掉"))
    if isinstance(spec.get("output"), str) and spec["output"] and not spec["output"].startswith("~/"):
        errs.append((f"{lbl} 的 output 不是 ~ 开头的路径", "写成 \"~/...\"，或删掉用默认的 state 目录"))
    repo = spec.get("repo")
    if isinstance(repo, str) and repo and (not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9._~/-]+$", repo)
                                           or looks_secret(repo)):
        errs.append((f"{lbl} 的 repo 不是不带凭证的 https 地址（值不显示）",
                     "写成 https://github.com/<owner>/<repo>，不带用户名、token、查询串；还没建仓就删掉"))
    if vis == LOCAL and spec.get("repo"):
        errs.append((f"{lbl} 是本机插件却写了 repo", "本机插件（补丁层）不推送：删掉 repo，或把 visibility 改成要推送的可见性"))
    for key in ("sections", "omit", "skills", "skill_sources", "company_skills", "privacy_reviewed", "env"):
        if key in spec and not _str_list(spec[key]):
            errs.append((f"{lbl} 的 {key} 不是字符串列表", f"写成 {key} = [\"...\"]，没有就删掉"))
    if _str_list(spec.get("omit")):
        if not spec.get("sections"):
            errs.append((f"{lbl} 写了 omit 却没有 sections", "omit 只从 sections 取到的章节里去掉原文：补上 sections，或删掉 omit"))
        for i, frag in enumerate(spec["omit"], 1):
            if not frag.strip():
                errs.append((f"{lbl} 的 omit 第 {i} 项只有空白", "写要去掉的那半句原文（连同它前后的连接标点），或删掉这一项"))
    for key in ("description", "version"):
        if isinstance(spec.get(key), str) and looks_secret(spec[key]):
            errs.append((f"{lbl} 的 {key} 疑似含密钥（值不显示）", "删掉密钥值；清单里只写名字"))
    if errs:
        return errs
    registry = manifest.get("rule_sections") if isinstance(manifest.get("rule_sections"), dict) else {}
    for h in spec.get("sections", []):
        meta = registry.get(h)
        if not isinstance(meta, dict):
            errs.append((f"{lbl} 的章节「{_shown(h)}」没有在 [rule_sections] 登记", "改成规则源里已登记的 `## ` 标题，或先登记该章节"))
        elif vis == "public" and meta.get("visibility") != "public":
            errs.append((f"{lbl} 是公开插件，但章节「{_shown(h)}」没有登记为 public",
                         "把该章节移到个人或本机插件；确认能公开后再把 [rule_sections] 里它的 visibility 改成 public"))
    if len(set(spec.get("sections", []))) != len(spec.get("sections", [])):
        errs.append((f"{lbl} 的 sections 有重复项", "每个章节只写一次"))
    errs += _rules_file_errors(manifest, lbl, spec, vis)
    for i, var in enumerate(spec.get("env", []), 1):
        if not ENV_VAR_RE.match(var) or looks_secret(var):
            errs.append((f"{lbl} 的 env 第 {i} 项不是变量名（值不显示）", "只写变量名，如 GITHUB_TOKEN，不写值"))
    lock = read_lock(manifest)
    root = manifest.get("sources", {}).get("skills")
    company = company_skill_names(manifest) if vis in GUARDED else set()
    for s in list(spec.get("skills", [])) + list(spec.get("skill_sources", [])):
        if s in company:
            errs.append((f"{lbl} 是{label(vis)}插件，但「{_shown(s)}」是公司 skill（在 [sources] company_skills 或落点的 company_skills 里）",
                         "公司 skill 只能进公司插件或本机插件：从这个插件删掉它"))
    for s in spec.get("skills", []):
        if not company_skills.REL_RE.match(s) or "/" in s:
            errs.append((f"{lbl} 的 skills 里有不合法的目录名（{_shown(s)}）", "写 skill 源下的一级目录名"))
        elif not isinstance(root, str) or not (Path(root) / s / "SKILL.md").is_file():
            errs.append((f"{lbl} 的自写 skill「{s}」在 [sources] skills 里没有 SKILL.md", "改成 skill 源里已有的目录名"))
        elif s in lock:
            errs.append((f"{lbl} 把「{s}」当自写 skill 复制，但锁文件记着它有上游",
                         "第三方 skill 只记来源不复制：把它从 skills 移到 skill_sources"))
        else:
            errs += [(f"{lbl}：{m}", f) for m, f in skill_limit_errors(Path(root) / s)]
    for s in spec.get("skill_sources", []):
        entry = lock.get(s)
        if not isinstance(entry, dict) or not entry.get("source") or not entry.get("skillPath"):
            errs.append((f"{lbl} 的第三方 skill「{_shown(s)}」在锁文件里没有来源（source / skillPath）",
                         "用 npx skills add 装它，让 [sources] skills_lock 指向的锁文件记下来源；"
                         "或把它当自写 skill 放进 skills"))
    dup = sorted(set(spec.get("skills", [])) & set(spec.get("skill_sources", [])))
    if dup:
        errs.append((f"{lbl} 的 {[_shown(d) for d in dup]} 同时在 skills 和 skill_sources", "二选一：有上游就放 skill_sources"))
    errs += _company_skill_errors(manifest, lbl, spec, vis)
    if spec.get("privacy_reviewed"):
        if vis != "company":
            errs.append((f"{lbl} 是{label(vis)}插件却写了 privacy_reviewed",
                         "只有公司插件能登记复核过的行；公开和个人插件的扫描不放行任何一行：删掉它"))
        bad = [i for i, k in enumerate(spec["privacy_reviewed"], 1) if not re.fullmatch(r"[^\s#]+#[0-9a-f]{12}", k)]
        if bad:
            errs.append((f"{lbl} 的 privacy_reviewed 第 {bad} 项不是 <文件>#<12 位哈希>",
                         "照生成时报错里给的写法原样抄，如 \"skills/x/scripts/a.py#0123456789ab\""))
    return errs


def _company_skill_errors(manifest: dict, lbl: str, spec: dict, vis: str) -> list:
    """A plugin's company_skills: only a company or local plugin carries them, each one a skill directory of
    [sources] company_skills, and no two of the plugin's skills land in the same skills/<name>/."""
    rels = spec.get("company_skills") or []
    if not rels:
        return []
    if vis not in ("company", LOCAL):
        return [(f"{lbl} 是{label(vis)}插件却写了 company_skills", "公司 skill 只能进公司插件或本机插件：删掉 company_skills")]
    errs = [(f"{lbl}：{m}", f) for m, f in company_skills.source_errors(manifest, rels)]
    if errs:
        return errs
    root = company_skills.source_root(manifest)
    leaves = [r.rsplit("/", 1)[-1] for r in rels]
    clash = sorted({x for x in leaves if leaves.count(x) > 1} | (set(leaves) & set(spec.get("skills", []) or [])))
    if clash:
        errs.append((f"{lbl} 有同名 skill {[_shown(c) for c in clash]}（company_skills 按最后一级目录名放进 skills/）",
                     "每个 skill 名只留一个"))
    for rel in rels:
        errs += [(f"{lbl}：{m}", f) for m, f in skill_limit_errors(root / rel)]
    return errs


def token_errors(tid, tok) -> list:
    """(message, fix) for a target's `token` table. Only key names are named: a value (also one put
    under the wrong key) is never printed."""
    how = "写成 token = { name = \"<secret 名>\", expires = 2027-02-08 }，只记名字和到期日，不写值"
    if not isinstance(tok, dict):
        return [(f"落点「{tid}」的 token 不是表（值不显示）", how)]
    errs = []
    extra = sorted(str(k) for k in set(tok) - {"name", "expires"})
    if extra:
        errs.append((f"落点「{tid}」的 token 多了键 {[_shown(k) for k in extra]}（值不显示）；清单不存 token 的值",
                     f"删掉这些键；如果填的是真 token，它已经进过文件，去签发处作废重建。{how}"))
    name = tok.get("name")
    if not isinstance(name, str) or not ENV_VAR_RE.match(name) or len(name) > SKILL_NAME_MAX or looks_secret(name):
        errs.append((f"落点「{tid}」的 token.name 不是 secret 的名字（值不显示）",
                     f"name 写 secret 的名字（字母、数字、下划线，64 字符以内），不是 token 本身。{how}"))
    exp = tok.get("expires")
    if isinstance(exp, datetime):
        errs.append((f"落点「{tid}」的 token.expires 带了时间",
                     "只写日期，如 expires = 2027-02-08（当天就算到期，不比较时分秒和时区）"))
    elif parse_date(exp) is None:
        errs.append((f"落点「{tid}」的 token.expires 不是日期", f"写成 expires = 2027-02-08。{how}"))
    return errs


def target_errors(manifest: dict, target: dict) -> list:
    """(message, fix) pairs for a target's plugins list, token and pointer limit."""
    tid = target.get("id")
    errs = []
    if "plugins" not in target:
        for key in ("token", "pointer_limit_chars", "pull", "rules_output"):
            if key in target:
                errs.append((f"落点「{tid}」有 {key} 但没有 plugins", "补上 plugins = [...]，或删掉该字段"))
        return errs
    names = target["plugins"]
    if not _str_list(names) or not names:
        return [(f"落点「{tid}」的 plugins 不是非空字符串列表", "写成 plugins = [\"public\", ...]，按覆盖顺序排")]
    old = [k for k in OLD_RENDER_FIELDS if k in target]
    if old:
        errs.append((f"落点「{tid}」用插件，却还写着按角色生成单份规则的字段 {old}",
                     f"删掉 {old}：插件落点的内容只来自 plugins 列表；要在本机合成规则文件就写 rules_output"))
    known = table(manifest)
    env = target.get("env")
    spec = env_spec(manifest, env)
    remote = spec.get("transport") in (REMOTE_EXEC, CLOUD_HOSTED)
    deny = spec.get("deny_visibility") if _str_list(spec.get("deny_visibility")) else []
    if len(set(names)) != len(names):
        errs.append((f"落点「{tid}」的 plugins 有重复项", "每个插件只写一次"))
    locals_ = []
    for i, n in enumerate(names):
        if n not in known:
            errs.append((f"落点「{tid}」的 plugins 引用了未定义的插件「{_shown(n)}」",
                         f"改成 {sorted(known)} 之一，或先在清单加 [plugins.<名字>]"))
            continue
        vis = visibility(known[n])
        if vis == LOCAL:
            locals_.append((i, n))
            if remote:
                errs.append((f"落点「{tid}」在{'云端托管' if spec.get('transport') == CLOUD_HOSTED else '远程'}环境 {env}，"
                             f"不能用本机插件「{n}」（没标可见性的插件按本机处理）",
                             f"从该落点的 plugins 删掉「{n}」；内容要给它就把插件 visibility 改成 personal 等可推送的可见性"))
        if vis in deny:
            errs.append((f"落点「{tid}」所在环境 {env} 不允许{label(vis)}内容，但 plugins 里有「{n}」（{label(vis)}）",
                         f"从该落点的 plugins 删掉「{n}」；{label(vis)}内容不进这个环境（docs/plugins-design.md）"))
    if len(locals_) > 1:
        errs.append((f"落点「{tid}」有 {len(locals_)} 个本机插件 {[n for _, n in locals_]}", "补丁层只能有一个：合并成一个本机插件"))
    elif locals_ and locals_[0][0] != len(names) - 1:
        errs.append((f"落点「{tid}」的补丁层「{locals_[0][1]}」没有排在最后", "把本机插件挪到 plugins 列表末尾，它要覆盖前面所有插件"))
    if "token" in target:
        errs += token_errors(tid, target["token"])
    if "pointer_limit_chars" in target:
        v = target["pointer_limit_chars"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errs.append((f"落点「{tid}」的 pointer_limit_chars 不是正整数", "写成不带引号的正整数，或删掉用默认 400"))
    import compose  # engine/compose.py and engine/pull.py import this module
    import pull
    errs += pull.target_errors(manifest, target, spec)
    errs += compose.target_errors(manifest, target, spec)
    return errs


def env_errors(name: str, spec: dict) -> list:
    """(message, fix) pairs for a cloud-hosted [environments.<name>] table."""
    lbl = f"[environments.{name}]"
    errs = []
    for key in ("platform", "exec", "send", "remote_home"):
        if key in spec:
            errs.append((f"{lbl} 是云端托管环境却写了 {key}", f"删掉 {key}：云端托管落点自己去取插件，本机不执行也不发送"))
    if "driven_from" in spec and (not isinstance(spec["driven_from"], str) or not spec["driven_from"]):
        errs.append((f"{lbl} 的 driven_from 不是环境名", "写成存放插件生成结果和投递记录的那个环境，如 driven_from = \"mac\""))
    if "deny_visibility" in spec:
        dv = spec["deny_visibility"]
        if not _str_list(dv) or set(dv) - set(VISIBILITY):
            errs.append((f"{lbl} 的 deny_visibility 无效", f"写成 {sorted(VISIBILITY)} 里的值组成的列表"))
    return errs


def content_errors(manifest: dict, name: str, rules_text) -> list:
    """Everything that must hold before plugin `name` is generated: its own table, and a rules source
    whose headings all split cleanly (a heading the parser does not see would stay inside the section
    before it and leave with that section)."""
    errs = plugin_errors(manifest, name, table(manifest).get(name))
    if isinstance(rules_text, str):
        fence = unclosed_fence(rules_text)
        if fence:
            errs.append((f"规则源第 {fence} 行开启的代码块没有闭合，之后的章节分不出来", "在规则源里补上闭合围栏"))
        for lineno, kind in suspect_headings(rules_text):
            errs.append((f"规则源第 {lineno} 行疑似未识别的标题（{kind}）",
                         "若是标题，改成行首 `## 标题` 并在 [rule_sections] 登记；若是分隔线，在它前面加一个空行"))
    return errs


# ── content ───────────────────────────────────────────────────────────────

def parse_date(value):
    """A calendar date (TOML local date or YYYY-MM-DD). A date-time is not accepted: its time and zone
    would be dropped, so the token would look expired or valid at the wrong moment."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
        except ValueError:
            return None
    return None


def own_sections(manifest: dict, spec: dict) -> list:
    """[(heading, text)] of the plugin's rules_file; its preamble (before the first `## `) is dropped."""
    rf = spec.get("rules_file")
    if not isinstance(rf, str):
        return []
    path = _resolve(manifest, rf)
    try:
        text = normalize(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginError(f"插件规则文件读不了：{render._tilde(path)}（{type(exc).__name__}）。"
                          f"修复：确认 rules_file 指向存在的 UTF-8 文件（相对清单目录）") from exc
    if unclosed_fence(text):
        raise PluginError(f"{render._tilde(path)} 有没闭合的代码块。修复：补上闭合围栏")
    return parse_sections(text)[1]


# ── omit: cut the half-sentences that only hold on the author's machine ───
# Each `omit` entry is an exact piece of the section text. Entries are applied in the order written,
# each to the text the earlier ones left. An entry cuts one occurrence: an entry written k times must
# occur exactly k times in the plugin's sections at that point, otherwise nothing is generated (the
# rules source changed, so the entry would silently stop working or cut something new).
# The author writes a piece with its own joining punctuation ("，见 `docs/x.md`", not "见 `docs/x.md`").
# The generator only does what needs no judgement, on the lines a cut touched: it drops trailing
# whitespace, one of two spaces the cut brought together, a line left empty, and one of two blank lines
# that line stood between. It then refuses what the author must fix: an empty pair of brackets, two
# punctuation marks brought together, a line that starts with one or ends with a separator, a list item
# left with only its marker, a `## ` heading or a code fence cut away.
# The entries are text that must not be published, so they go into no generated file, and an error
# names an entry by its position, never by its text.
_OMIT_MARK = "\0"
_BARE_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*$")
_LINE_HEAD_RE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?$")
_EMPTY_PAIRS = ("（）", "()", "「」", "『』", "[]", "【】", "《》", "“”", "‘’")
_SEPARATORS = "，、；："
_STOPS = "。！？"
_CLOSERS = "）」』】》”’"


def _seam_problem(left: str, right: str):
    """What a cut left dangling where it was (None when clean). `left` and `right` are the line's text
    before and after the cut."""
    a, b = left.rstrip(" ")[-1:], right.lstrip(" ")[:1]
    if _BARE_ITEM_RE.match(left) and not right.strip():
        return "只剩标记的空列表项"
    if a and b and a + b in _EMPTY_PAIRS:
        return f"空括号 {a + b}"
    if b and _LINE_HEAD_RE.match(left) and b in _SEPARATORS + _STOPS + _CLOSERS:
        return f"行首的标点 {b}"
    if a and b and ((a in _SEPARATORS and b in _SEPARATORS + _STOPS + _CLOSERS)
                    or (a in _STOPS and b in _SEPARATORS + _STOPS)):
        return f"连在一起的标点 {a + b}"
    if a and a in "，、；" and not right.strip():
        return f"行尾的标点 {a}"
    return None


def _tidy(text: str, lbl: str, index: int) -> str:
    """`text` with every _OMIT_MARK taken out and the lines it stood on cleaned (rules above)."""
    lines = text.split("\n")
    last = lines.pop() if text.endswith("\n") else None
    out, dropped = [], False
    for line in lines:
        if _OMIT_MARK not in line:
            if dropped and not line and (not out or not out[-1]):
                dropped = False  # the dropped line stood between two blank lines: keep one
                continue
            dropped = False
            out.append(line)
            continue
        segs = line.split(_OMIT_MARK)
        for k in range(1, len(segs)):
            problem = _seam_problem("".join(segs[:k]), "".join(segs[k:]))
            if problem:
                raise PluginError(f"{lbl} 的 omit 第 {index} 项去掉后留下了{problem}。"
                                  f"修复：把这一项前后的连接标点（或列表标记和换行）一起写进片段；"
                                  f"生成器只清行尾空白、空行和重复的空格，不替你改标点")
        joined = segs[0]
        for seg in segs[1:]:
            joined += seg[1:] if joined.endswith(" ") and seg.startswith(" ") else seg
        joined = joined.rstrip()
        if joined:
            dropped = False
            out.append(joined)
        else:
            dropped = True
    if last is not None:
        out.append(last)
    return "\n".join(out)


def _shape(text: str) -> tuple:
    preamble, sections = parse_sections(text)
    return bool(preamble.strip()), [h for h, _ in sections], unclosed_fence(text) is None


def _apply_omit(name: str, parts: list, omit: list) -> list:
    """`parts` ([(heading, text)]) with every `omit` entry cut out of the texts (rules above)."""
    lbl = f"[plugins.{_shown(name)}]"
    texts = [t for _, t in parts]
    if any(_OMIT_MARK in t for t in texts):
        raise PluginError(f"规则源里有 NUL 字符，{lbl} 的 omit 没法可靠地定位原文。修复：删掉规则源里的 NUL 字符")
    done = set()
    for i, frag in enumerate(omit, 1):
        if frag in done:
            continue
        done.add(frag)
        listed, found = omit.count(frag), sum(t.count(frag) for t in texts)
        if not found:
            raise PluginError(f"{lbl} 的 omit 第 {i} 项在它取的章节里找不到（规则源改过，或它和前面的项重叠）。"
                              f"修复：对照规则源现在的原文重写这一项（逐字相同，含标点和空格）；那句话已经不在了就删掉这一项")
        if found != listed:
            raise PluginError(f"{lbl} 的 omit 第 {i} 项在它取的章节里出现 {found} 处，清单里写了 {listed} 次。"
                              f"修复：写一次去掉一处：{found} 处都要去掉就把这一项写 {found} 次；"
                              f"只去其中一处就把片段写长到只对上那一处")
        texts = [_tidy(t.replace(frag, _OMIT_MARK), lbl, i) if frag in t else t for t in texts]
    for (_, old), new in zip(parts, texts):
        if _shape(new) != _shape(old):
            raise PluginError(f"{lbl} 的 omit 去掉了 `## ` 标题行或代码块围栏，或把一段说明区去空了，章节分不清了。"
                              f"修复：片段里不要带标题行和围栏行；整章都不要就从 sections 里删掉那一章")
    return [(h, t) for (h, _), t in zip(parts, texts)]


def rules_parts(manifest: dict, name: str, rules_text: str, apply_omit: bool = True) -> list:
    """[(heading, text)] the plugin carries from the shared rules, in rules order, with its `omit` entries cut
    out. `apply_omit=False` keeps them: a target composed on the author's own machine with a patch layer
    (engine/compose.py) reads the spans the published plugin leaves out."""
    return _rules_parts(manifest, name, rules_text, apply_omit)


def _rules_parts(manifest: dict, name: str, rules_text: str, apply_omit: bool = True) -> list:
    spec = get(manifest, name)
    if unclosed_fence(rules_text):
        raise PluginError("规则源有没闭合的代码块，之后的章节会被丢掉。修复：在规则源里补上闭合围栏，再生成")
    want = set(spec.get("sections", []) or [])
    preamble, sections = parse_sections(rules_text)
    parts = [(PREAMBLE_KEY, preamble)] if PREAMBLE_KEY in want and preamble.strip() else []
    parts += [(h, t) for h, t in sections if h in want]
    missing = sorted(want - {h for h, _ in parts} - ({PREAMBLE_KEY} if not preamble.strip() else set()))
    if missing:
        raise PluginError(f"插件要的章节 {[_shown(h) for h in missing]} 在规则源里找不到。修复：改成规则源里现有的 `## ` 标题")
    omit = spec.get("omit") or []
    return _apply_omit(name, parts, omit) if omit and apply_omit else parts


def agents_md(manifest: dict, name: str, rules_text: str) -> str:
    spec = get(manifest, name)
    vis = visibility(spec)
    head = (f"<!-- 插件 {name}（可见性：{label(vis)}）的规则，由 agent-env 生成，不要手改。"
            f"落点按插件顺序读各插件的 AGENTS.md，后读的覆盖先读的。 -->\n\n")
    body = "".join(t if t.endswith("\n") else t + "\n" for _, t in _rules_parts(manifest, name, rules_text))
    own = "".join(t if t.endswith("\n") else t + "\n" for _, t in own_sections(manifest, spec))
    if own and body and not body.endswith("\n\n"):
        body += "\n"
    return head + body + own


def skill_copy(skill_dir: Path):
    """(files to copy, credential-store files left out, symlinks) of an own skill, as relative POSIX
    paths. Runtime folders (.venv, __pycache__, node_modules) are never walked."""
    keep, dropped, links = [], [], []
    for top, dirs, names in os.walk(skill_dir):
        rel_top = os.path.relpath(top, skill_dir).replace(os.sep, "/")
        prefix = "" if rel_top == "." else rel_top + "/"
        for d in sorted(dirs):
            if os.path.islink(os.path.join(top, d)):
                links.append(prefix + d)
            elif is_credential_file(d):
                dropped.append(prefix + d + "/")
        dirs[:] = [d for d in dirs if d not in company_skills.RUNTIME_DIRS and not is_credential_file(d)
                   and not os.path.islink(os.path.join(top, d))]
        for f in names:
            full = os.path.join(top, f)
            if os.path.islink(full):
                links.append(prefix + f)
            elif f in company_skills.SKIP_FILES or not os.path.isfile(full):
                continue
            elif is_credential_file(f):
                dropped.append(prefix + f)
            else:
                keep.append(prefix + f)
    return sorted(keep), sorted(dropped), sorted(links)


def skill_dirs(manifest: dict, spec: dict) -> list:
    """[(name in the plugin, source directory or None)] of the skills a plugin copies: its own skills from
    [sources] skills, then its company skills from [sources] company_skills (under their last segment)."""
    out = []
    root = manifest.get("sources", {}).get("skills")
    for s in spec.get("skills", []) or []:
        out.append((s, Path(root) / s if isinstance(root, str) else None))
    croot = manifest.get("sources", {}).get("company_skills")
    for rel in spec.get("company_skills", []) or []:
        out.append((rel.rsplit("/", 1)[-1], Path(croot) / rel if isinstance(croot, str) else None))
    return out


def _skill_files(manifest: dict, name: str, spec: dict) -> dict:
    out = {}
    for s, d in skill_dirs(manifest, spec):
        if d is None or not (d / "SKILL.md").is_file():
            raise PluginError(f"插件「{name}」的 skill「{s}」找不到 SKILL.md。修复：确认 [sources] skills"
                              f"（公司 skill 是 [sources] company_skills）下有它，或从 [plugins.{name}] 删掉")
        keep, _, links = skill_copy(d)
        if links:  # a plugin's skills are real copies: Grok Build skips links that point outside the plugin
            raise PluginError(f"插件「{name}」的自写 skill「{s}」里有符号链接 {links[:3]}（Grok Build 会跳过指向插件外的链接）。"
                              f"修复：把链接换成真实文件，或删掉")
        for rel in keep:
            full = d / rel
            out[f"skills/{s}/{rel}"] = (full.read_bytes(), bool(full.stat().st_mode & 0o111))
    return out


def dropped_credentials(manifest: dict, name: str) -> list:
    """['<skill>/<rel>'] credential-store files of the plugin's copied skills that are not copied."""
    out = []
    for s, d in skill_dirs(manifest, get(manifest, name)):
        if d is not None and d.is_dir():
            out += [f"{s}/{rel}" for rel in skill_copy(d)[1]]
    return out


def skill_warnings(manifest: dict, name: str) -> list:
    """Notes that do not stop a build: credential-store files left out, and own skills whose SKILL.md
    body is over the size Codex loads (CODEX_SKILL_BODY_LIMIT)."""
    notes = []
    dropped = dropped_credentials(manifest, name)
    if dropped:
        notes.append(f"没有复制凭证文件 {dropped[:5]}{' 等' if len(dropped) > 5 else ''}（{len(dropped)} 个，凭证不进插件）")
    for s, d in skill_dirs(manifest, get(manifest, name)):
        try:
            size = len(frontmatter.body((d / "SKILL.md").read_text(encoding="utf-8")).encode("utf-8"))
        except (OSError, UnicodeDecodeError, TypeError, AttributeError):
            continue
        if size > CODEX_SKILL_BODY_LIMIT:
            notes.append(f"提醒：skill「{s}」的 SKILL.md 正文 {size} B，超过 Codex 加载时的 {CODEX_SKILL_BODY_LIMIT} B 上限，"
                         f"超出的部分 Codex 读不到。修复：把细节挪进 {s}/references/，正文只留步骤和入口")
    return notes


def skill_folder_hash(skill_dir: Path) -> str:
    """`computedHash` of a project `skills-lock.json` entry, as `npx skills` (skills 1.7.6,
    computeSkillFolderHash) computes it: sha256 over each regular file's relative path then its bytes,
    .git and node_modules skipped, symlinks not followed, the paths sorted with `a.localeCompare(b)`.
    That sort is ICU collation in the machine's default locale, so the CLI's own hash differs between
    machines for names a locale orders differently (Swedish å/ä/ö, CJK). Ours must be byte-stable, so
    it is always the en-US order (collation.sort_key, which never reads the locale): equal to the CLI on
    an English-locale machine, possibly not elsewhere. An accepted limitation (docs/plugins-design.md)."""
    files = []
    for top, dirs, names in os.walk(skill_dir):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules") and not os.path.islink(os.path.join(top, x))]
        for f in names:
            full = os.path.join(top, f)
            if os.path.isfile(full) and not os.path.islink(full):
                files.append(os.path.relpath(full, skill_dir).replace(os.sep, "/"))
    h = hashlib.sha256()
    for rel in sorted(files, key=collation.sort_key):
        h.update(rel.encode("utf-8"))
        h.update((skill_dir / rel).read_bytes())
    return h.hexdigest()


def installed_hash(manifest: dict, plugin: str, skill: str) -> str:
    """computedHash from the copy of a third-party skill installed under [sources] skills."""
    root = manifest.get("sources", {}).get("skills")
    d = Path(root) / skill if isinstance(root, str) else None
    if d is None or not (d / "SKILL.md").is_file():
        raise PluginError(f"插件「{plugin}」的第三方 skill「{skill}」本机没装（[sources] skills 下没有 {skill}/SKILL.md），"
                          f"算不出 computedHash。修复：用 npx skills add 装上，或从 skill_sources 删掉")
    return skill_folder_hash(d.resolve())


def _json(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_plugin(manifest: dict, name: str, rules_text) -> dict:
    """{relative path: (bytes, executable)} for one plugin, sorted by path. Raises PluginError (with a
    fix) when the plugin's table or the rules source fails validation, on missing sources, Agent Skills
    limit hits, privacy guard hits, an `omit` entry that no longer matches, or (public plugins) a path
    that points outside the plugin. The `omit` entries are written into no file. Nothing is generated from a manifest that `check` would fail."""
    if rules_text is None:
        raise PluginError("规则源读不了，无法生成插件。修复：在 [sources] 里把 rules 指向存在的 UTF-8 文件")
    spec = get(manifest, name)
    errs = content_errors(manifest, name, rules_text)
    if errs:
        msg, fix = errs[0]
        more = f"（另有 {len(errs) - 1} 项，bin/agent-env check 看全部）" if len(errs) > 1 else ""
        raise PluginError(f"插件「{name}」没过校验，不生成：{msg}{more}。修复：{fix}")
    vis = visibility(spec)
    version = spec.get("version") or DEFAULT_VERSION
    desc = spec.get("description") or f"agent-env 插件 {name}"
    files = {"AGENTS.md": (agents_md(manifest, name, rules_text).encode("utf-8"), False)}
    files.update(_skill_files(manifest, name, spec))
    third = spec.get("skill_sources", []) or []
    if third:
        lock = read_lock(manifest)
        entries = {}
        for s in third:
            e = lock.get(s)
            if not isinstance(e, dict) or not e.get("source") or not e.get("skillPath"):
                raise PluginError(f"插件「{name}」的第三方 skill「{s}」在锁文件里没有来源。"
                                  f"修复：用 npx skills add 装它，或从 skill_sources 删掉")
            entry = {"source": e["source"]}
            if isinstance(e.get("sourceUrl"), str) and e["sourceUrl"]:
                entry["sourceUrl"] = e["sourceUrl"]
            entry["sourceType"] = e.get("sourceType", "github")
            entry["skillPath"] = e["skillPath"]
            entry["computedHash"] = installed_hash(manifest, name, s)
            entries[s] = entry
        files["skills-lock.json"] = (_json({"version": 1, "skills": entries}), False)
    files["agent-env-plugin.json"] = (_json({
        "name": name, "version": version, "visibility": vis, "visibility_zh": label(vis), "description": desc,
        "env": list(spec.get("env", []) or []), "sections": [h for h, _ in _rules_parts(manifest, name, rules_text)]
        + [h for h, _ in own_sections(manifest, spec)],
        "skills": list(spec.get("skills", []) or []), "skill_sources": list(third),
        # only a plugin that has company skills carries the key, so the others keep their bytes
        **({"company_skills": [r.rsplit("/", 1)[-1] for r in spec["company_skills"]]}
           if spec.get("company_skills") else {})}), False)
    owner = manifest.get("plugin_owner")
    author = {"author": {"name": owner}} if isinstance(owner, str) and owner else {}
    files[".claude-plugin/plugin.json"] = (_json({"name": name, "version": version, "description": desc, **author}), False)
    # The repository is its own marketplace, so Claude Code, Codex and Grok Build install it from one URL.
    files[".claude-plugin/marketplace.json"] = (_json({
        "name": name, "owner": {"name": owner if isinstance(owner, str) and owner else name}, "description": desc,
        "plugins": [{"name": name, "source": "./", "description": desc, "version": version}]}), False)
    files[".cursor-plugin/plugin.json"] = (_json({"name": name, "version": version, "description": desc,
                                                  "skills": "./skills/"}), False)
    files["package.json"] = (_json({"name": name, "version": version, "private": True, "description": desc,
                                    "keywords": ["pi-package"], "pi": {"skills": ["./skills"]}}), False)
    hits = privacy_hits(files, vis, spec.get("privacy_reviewed") or ())
    if hits:
        raise PluginError(f"插件「{name}」（{label(vis)}）有不能外发的内容：{'；'.join(hits[:5])}"
                          + (f" 等 {len(hits)} 处" if len(hits) > 5 else "")
                          + "。修复：把绝对路径改成 ~/ 或环境变量，删掉邮箱和密钥值（示例值写成 <占位符>）；"
                            "不是 UTF-8 文本也不是图片的文件转成 UTF-8，或移到插件之外的 references；"
                            "这段内容只该留在本机就把它移到本机插件（补丁层）")
    outside = outside_paths(files) if vis == "public" else []
    if outside:
        raise PluginError(f"公开插件「{name}」有 {len(outside)} 处指向插件外的文件，只拿到插件的人打不开："
                          f"{_outside_shown(outside, 5)}。修复：{_outside_fix(name)}")
    return dict(sorted(files.items()))


def files_hash(files: dict) -> str:
    h = hashlib.sha256()
    for rel, (data, exe) in sorted(files.items()):
        h.update(f"{rel}\0{int(exe)}\0{hashlib.sha256(data).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def tree_files(path: Path):
    """The files of a built plugin directory in render_plugin's shape; None when it is absent."""
    if not path.is_dir():
        return None
    out = {}
    for top, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in PRESERVE)
        for f in names:
            full = Path(top) / f
            if f in company_skills.SKIP_FILES or full.is_symlink() or not full.is_file():
                continue
            out[full.relative_to(path).as_posix()] = (full.read_bytes(), bool(full.stat().st_mode & 0o111))
    return dict(sorted(out.items()))


def diff_counts(old: dict, new: dict) -> tuple:
    added = len(set(new) - set(old))
    removed = len(set(old) - set(new))
    changed = sum(1 for k in set(old) & set(new) if old[k] != new[k])
    return added, removed, changed


# ── writing: one lock per output, a temp dir beside it, renames that roll back ────

def lock_path(dest: Path) -> Path:
    return dest.parent / f".{dest.name}.lock"


@contextlib.contextmanager
def output_lock(dest: Path):
    """Hold the output's lock for a build. A second build of the same output fails at once instead of
    waiting: whoever holds the lock owns every `.<name>.tmp-*` and `.<name>.old-*` beside the output,
    so nothing ever cleans up a directory another build is still writing. The lock is an flock on a
    file that stays; it is released when the process exits, however it exits."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    path = lock_path(dest)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PluginError(f"另一个 agent-env 进程正在生成 {render._tilde(dest)}，这次没有动它。"
                              f"修复：等那个进程结束后重跑（锁随进程退出自动释放，不用手动删 {render._tilde(path)}）")
        yield
    finally:
        os.close(fd)


def _write_file(path: Path, data: bytes, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o755 if executable else 0o644)


def recover(dest: Path) -> list:
    """Finish what a killed build left. Call it only while holding output_lock(dest): then every temp
    and set-aside dir beside the output is a leftover, never another build's work.

    - no output but a set-aside old one: the swap died between its two renames; the old output (with
      its `.git`) goes back.
    - an output and a set-aside old one: the new tree had landed; `.git` moves over if it has not yet,
      then the old one goes.
    Returns what it did, for the caller to print."""
    did = []
    parent = dest.parent
    if not parent.is_dir():
        return did
    olds = sorted(parent.glob(f".{dest.name}.old-*"))
    for old in olds:
        if not dest.exists():
            os.rename(old, dest)
            did.append(f"上次替换中断，已把旧输出放回 {render._tilde(dest)}")
            continue
        for keep in PRESERVE:
            if (old / keep).exists() and not (dest / keep).exists():
                os.rename(old / keep, dest / keep)
                did.append(f"上次替换中断，已把 {keep} 移回 {render._tilde(dest)}")
        shutil.rmtree(old, ignore_errors=True)
        did.append(f"清理了上次中断留下的 {render._tilde(old)}")
    for tmp in sorted(parent.glob(f".{dest.name}.tmp-*")):
        shutil.rmtree(tmp, ignore_errors=True)
        did.append(f"清理了上次中断留下的 {render._tilde(tmp)}")
    return did


def _swap(dest: Path, tmp: Path) -> None:
    """Put the finished temp tree in place of dest. Order: set the old output aside, move the new tree
    in, move `.git` from the old output into it, drop the old output. `.git` never sits in a temp dir,
    and a failure at any step puts the old output back exactly as it was."""
    old = dest.parent / f".{dest.name}.old-{tmp.name.rsplit('-', 1)[-1]}"
    set_aside = False
    try:
        if dest.exists():
            os.rename(dest, old)
            set_aside = True
        os.rename(tmp, dest)
        if set_aside:
            for keep in PRESERVE:
                if (old / keep).exists():
                    os.rename(old / keep, dest / keep)
    except BaseException:
        if set_aside and old.exists():
            if dest.exists():  # the new tree is in: give `.git` back, then take the new tree out
                for keep in PRESERVE:
                    if (dest / keep).exists() and not (old / keep).exists():
                        os.rename(dest / keep, old / keep)
                os.rename(dest, tmp)
            os.rename(old, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if set_aside:
        shutil.rmtree(old, ignore_errors=True)


def stage(dest: Path, files: dict) -> Path:
    """Write `files` into a fresh temp dir beside dest (the caller holds output_lock(dest)) and return
    it. A failure leaves nothing behind and dest untouched."""
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=parent, prefix=f".{dest.name}.tmp-"))
    try:
        for rel, (data, exe) in files.items():
            _write_file(tmp / rel, data, exe)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


def install(name: str, vis: str, dest: Path, files: dict) -> tuple:
    """(changed, notes): stage `files` beside dest, scan the staged dir (guard_staged), then replace
    dest when it differs. The caller holds output_lock(dest). Nothing that fails the scan ever becomes
    the output; the staged dir is removed on every path."""
    tmp = stage(dest, files)
    try:
        notes = guard_staged(name, vis, tmp)
        if tree_files(dest) == files:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, notes
        _swap(dest, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True, notes


def build(manifest: dict, name: str, rules_text) -> tuple:
    """(changed, output dir, files hash, notes). The files are staged beside the output and scanned
    there (guard_staged) on every run; the output is replaced only when it differs from the sources."""
    files = render_plugin(manifest, name, rules_text)
    dest = output_dir(manifest, name)
    with output_lock(dest):
        vis = visibility(get(manifest, name))
        notes = recover(dest) + skill_warnings(manifest, name) + outside_notes(name, vis, files)
        changed, more = install(name, vis, dest, files)
    return changed, dest, files_hash(files), notes + more


def resolve_names(manifest: dict, name=None, all_=False) -> list:
    if all_ == bool(name):
        raise PluginError("要么 --plugin <名字>，要么 --all。修复：二选一")
    if name:
        get(manifest, name)
        return [name]
    names = sorted(table(manifest))
    if not names:
        raise PluginError("清单里没有任何插件。修复：先加 [plugins.<名字>]（docs/plugins-design.md）")
    return names


# ── publish: a step ledger so a rerun resumes ─────────────────────────────

def ledger_path() -> Path:
    return state_dir() / "plugins" / "publish-ledger.json"


def read_ledger() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("plugins"), dict) else {"plugins": {}}
    except (OSError, ValueError):
        return {"plugins": {}}


def _save_ledger(data: dict) -> None:
    render.atomic_write(ledger_path(), json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _step_build(manifest, name, files, dest, h, **_):
    changed, notes = install(name, visibility(get(manifest, name)), dest, files)
    extra = "".join(f"；{n}" for n in notes)
    if not changed:
        return f"{render._tilde(dest)} 已是最新（{len(files)} 个文件）{extra}"
    return f"已生成 {render._tilde(dest)}（{len(files)} 个文件）{extra}"


def _step_scan(manifest, name, files, dest, h, **_):
    actual = tree_files(dest)
    if actual is None or files_hash(actual) != h:
        raise PluginError(f"{render._tilde(dest)} 在生成之后被改过，和记录的生成结果对不上。"
                          f"修复：重跑 bin/agent-env plugin publish --plugin {name}，会重新生成再扫描")
    vis = visibility(get(manifest, name))
    hits = privacy_hits(actual, vis, get(manifest, name).get("privacy_reviewed") or ())
    if hits:
        raise PluginError(f"扫描发现不能外发的内容：{'；'.join(hits[:5])}。修复：改源后重跑 publish")
    tmp = stage(dest, actual)  # a copy without .git: what would be pushed, nothing else
    try:
        notes = guard_staged(name, vis, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "隐私扫描通过（gitleaks、家目录路径、邮箱、密钥模式、非图片的二进制）" + "".join(f"；{n}" for n in notes)


def _step_commit(manifest, name, files, dest, h, **_):
    url = _repo_url(manifest, name)
    actual = tree_files(dest)
    if actual is None or files_hash(actual) != h:  # what was scanned is what gets committed
        raise PluginError(f"{render._tilde(dest)} 在扫描之后被改过，和扫描过的生成结果对不上。"
                          f"修复：重跑 bin/agent-env plugin publish --plugin {name}，会重新生成再扫描")
    clone = clone_dir(name)
    branch = _sync_clone(clone, url, name)
    for entry in clone.iterdir():  # an exact mirror: what is no longer generated goes, `.git` stays
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for rel, (data, exe) in actual.items():
        _write_file(clone / rel, data, exe)
    _git(clone, "add", "-A", "-f", ".")
    staged = _index_blobs(clone)
    if staged != _expected_blobs(actual):
        raise PluginError(f"工作克隆 {render._tilde(clone)} 暂存的文件和扫描过的生成结果对不上（多了或少了文件），没有提交。"
                          f"修复：看 git -C {render._tilde(clone)} status，删掉这个工作克隆后重跑 publish")
    if _has_head(clone) and not _git(clone, "status", "--porcelain").stdout.strip():
        return f"{render._tilde(clone)} 和 {url} 的 {branch} 一致，已是最新，无需提交"
    who = _identity(url)
    env = dict(os.environ)
    if who:
        env.update(GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_COMMITTER_NAME=who[0], GIT_COMMITTER_EMAIL=who[1])
    _git(clone, "commit", "-q", "-m", COMMIT_MESSAGE.format(name=name), env=env,
         fix="确认 git 能提交（git config user.name / user.email 已设，提交钩子没有拦）后重跑 publish")
    sha = _git(clone, "rev-parse", "--short", "HEAD").stdout.strip()
    author = _git(clone, "log", "-1", "--format=%ae").stdout.strip()
    return f"已提交 {sha} 到 {render._tilde(clone)}（{branch}，{len(actual)} 个文件，作者 {author}）"


def _step_push(manifest, name, files, dest, h, yes=False, out=None, **_):
    url = _repo_url(manifest, name)
    clone = clone_dir(name)
    if not _has_head(clone):
        raise PluginError(f"工作克隆 {render._tilde(clone)} 里没有提交。修复：重跑 bin/agent-env plugin publish --plugin {name}，"
                          f"会重新提交")
    branch = _fetch(clone, url, name)
    remote = f"refs/remotes/origin/{branch}"
    has_remote = _git(clone, "rev-parse", "-q", "--verify", remote, check=False).returncode == 0
    if has_remote and _git(clone, "merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode != 0:
        raise PluginError(_diverged(clone, url, branch, name))
    ahead = _git(clone, "rev-list", "--reverse", f"{remote}..HEAD" if has_remote else "HEAD").stdout.split()
    if not ahead:
        _mark_pushed(url)  # the content is there, so the repository is not empty (an old cache may still say so)
        return f"{url} 的 {branch} 已有这份内容，无需推送"
    vis = visibility(get(manifest, name))
    seen = _remote_visibility(url)
    refuse = _push_refusal(vis, seen, url)
    if not yes:
        w = out.write if out is not None else (lambda s: None)
        shown = {"public": "公开", "private": "私有"}.get(seen.get("visibility"), "未知")
        w(f"插件 {name}：演练，将推送到 {url}（{branch}；仓库{shown}，插件{label(vis)}）\n")
        for sha in ahead:
            w(f"插件 {name}：  提交 {_git(clone, 'log', '-1', '--format=%h %s', sha).stdout.strip()}\n")
        diff = (_git(clone, "diff", "--name-status", "--no-renames", remote, "HEAD") if has_remote else
                _git(clone, "show", "--name-status", "--no-renames", "--format=", "HEAD")).stdout.splitlines()
        if not has_remote:  # an empty repository: every file of HEAD is new
            diff = [f"A\t{p}" for p in _git(clone, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()]
        for line in diff[:DRY_RUN_FILES]:
            w(f"插件 {name}：  {line}\n")
        if len(diff) > DRY_RUN_FILES:
            w(f"插件 {name}：  ……还有 {len(diff) - DRY_RUN_FILES} 个文件（git -C {render._tilde(clone)} "
              f"diff --stat {'origin/' + branch if has_remote else 'HEAD'}）\n")
        if refuse:
            w(f"插件 {name}：加 --yes 也会被拒：{refuse}\n")
        raise DryRun(f"演练：{len(ahead)} 个提交、{len(diff)} 个文件待推送，加 --yes 才推送")
    if refuse:
        raise PluginError(refuse)
    p = _git(clone, "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}", check=False, network=True)
    if p.returncode != 0:
        said = (p.stderr + "\n" + p.stdout).lower()
        if "non-fast-forward" in said or "fetch first" in said or "rejected" in said:
            raise PluginError(_diverged(clone, url, branch, name))
        raise PluginError(f"推送到 {url} 失败：{_first_line(p)}。修复：确认能联网、有这个仓库的写权限后重跑 publish --yes")
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    there = _git(clone, "ls-remote", "origin", f"refs/heads/{branch}", network=True).stdout.split()
    if not there or there[0] != head:
        raise PluginError(f"推送后 {url} 的 {branch} 不是 {head[:7]}。修复：git -C {render._tilde(clone)} fetch 看远端，再重跑 publish --yes")
    _mark_pushed(url, seen)  # the panel's 插件 tab must not keep calling it 空仓 until the next refresh
    return f"已推送 {len(ahead)} 个提交到 {url}（{branch}，{head[:7]}）"


# ── publish: the working clone and the remote ─────────────────────────────

COMMIT_MESSAGE = "✨ feat: update the {name} plugin generated by agent-env"
DRY_RUN_FILES = 50
GIT_TIMEOUT = 120


class DryRun(NotYet):
    """push without --yes: what would be pushed has been printed; nothing left this machine."""


def clone_dir(name: str) -> Path:
    """The plugin's working clone: committed here, pushed from here. Separate from the build output."""
    return state_dir() / "plugin-repos" / name


def _repo_url(manifest: dict, name: str) -> str:
    url = get(manifest, name).get("repo")
    if not isinstance(url, str) or not url.strip():
        raise NotYet(f"清单 [plugins.{name}] 没有 repo。修复：先建仓并在清单写 repo = \"<仓库地址>\"")
    return url.strip()


def _first_line(p) -> str:
    return next((ln.strip() for ln in ((p.stderr or "") + "\n" + (p.stdout or "")).splitlines() if ln.strip()),
                "没有输出")[:200]


def _git(repo: Path, *args, check=True, network=False, env=None, fix=""):
    """Run git in `repo`. Network commands never prompt for a password (they fail instead)."""
    env = dict(env if env is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env,
                           timeout=GIT_TIMEOUT if network else None)
    except subprocess.TimeoutExpired:
        raise PluginError(f"git {args[0]} {GIT_TIMEOUT} 秒没有返回。修复：检查网络后重跑 publish")
    except FileNotFoundError:
        raise PluginError("这台机器没有 git 命令。修复：装好 git 后重跑 publish")
    if check and p.returncode != 0:
        raise PluginError(f"git {' '.join(args[:2])} 失败：{_first_line(p)}。修复："
                          + (fix or (f"确认能联网、有仓库权限后重跑 publish" if network else
                                     f"看 {render._tilde(repo)}，必要时删掉这个工作克隆后重跑 publish")))
    return p


def _has_head(clone: Path) -> bool:
    return (clone / ".git").exists() and _git(clone, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0


def _fetch(clone: Path, url: str, name: str) -> str:
    """Fetch origin into the clone (made if missing, origin pointed at `url`); return the branch to use:
    origin's default branch, or `main` when the repository is empty."""
    if not (clone / ".git").exists():
        clone.mkdir(parents=True, exist_ok=True)
        _git(clone, "init", "-q", "-b", "main")
    have = _git(clone, "remote", "get-url", "origin", check=False)
    if have.returncode != 0:
        _git(clone, "remote", "add", "origin", url)
    elif have.stdout.strip() != url:
        _git(clone, "remote", "set-url", "origin", url)
    _git(clone, "fetch", "-q", "--prune", "origin", network=True)
    heads = _git(clone, "ls-remote", "--symref", "origin", "HEAD", network=True).stdout
    m = re.search(r"^ref:\s*refs/heads/(\S+)\s+HEAD", heads, re.M)
    if m:
        return m.group(1)
    branches = _git(clone, "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin").stdout.split()
    branches = [b for b in branches if b != "HEAD"]
    for b in ("main", "master"):
        if b in branches:
            return b
    return branches[0] if branches else "main"


def _diverged(clone: Path, url: str, branch: str, name: str) -> str:
    c = render._tilde(clone)
    return (f"{url} 的 {branch} 有工作克隆 {c} 没有的提交（不是快进），不会强推。修复：先看远端多了什么"
            f"（git -C {c} log HEAD..origin/{branch}）；确认可以丢掉本机未推送的提交后跑 "
            f"git -C {c} reset --hard origin/{branch}，再重跑 bin/agent-env plugin publish --plugin {name}，会重新提交")


def _sync_clone(clone: Path, url: str, name: str) -> str:
    """Bring the working clone to origin's default branch: fetched, fast-forwarded, clean. Commits made
    here but not pushed yet are kept; a clone that has diverged from origin is an error."""
    branch = _fetch(clone, url, name)
    remote = f"refs/remotes/origin/{branch}"
    has_remote = _git(clone, "rev-parse", "-q", "--verify", remote, check=False).returncode == 0
    if has_remote and _git(clone, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).returncode != 0:
        _git(clone, "update-ref", f"refs/heads/{branch}", remote)
    _git(clone, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    if _has_head(clone):
        _git(clone, "reset", "-q", "--hard")
    _git(clone, "clean", "-q", "-ffdx")
    if has_remote:
        if _git(clone, "merge-base", "--is-ancestor", "HEAD", remote, check=False).returncode == 0:
            _git(clone, "merge", "-q", "--ff-only", remote)
        elif _git(clone, "merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode != 0:
            raise PluginError(_diverged(clone, url, branch, name))
    return branch


def _expected_blobs(files: dict) -> dict:
    """{path: (mode, blob id)} git would store for `files`."""
    out = {}
    for rel, (data, exe) in files.items():
        out[rel] = ("100755" if exe else "100644", hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
    return out


def _index_blobs(clone: Path, tree=None) -> dict:
    """{path: (mode, blob id)} of the index, or of a commit's tree when `tree` is given."""
    if tree:
        raw = _git(clone, "ls-tree", "-r", "-z", tree).stdout
        rows = (e.split("\t", 1) for e in raw.split("\0") if e)
        return {path: (meta.split()[0], meta.split()[2]) for meta, path in rows}
    raw = _git(clone, "ls-files", "-s", "-z").stdout
    rows = (e.split("\t", 1) for e in raw.split("\0") if e)
    return {path: (meta.split()[0], meta.split()[1]) for meta, path in rows}


def committed(name: str, files: dict) -> bool:
    """Whether the working clone's HEAD holds exactly `files` (a commit recorded earlier still stands)."""
    clone = clone_dir(name)
    try:
        return _has_head(clone) and _index_blobs(clone, "HEAD") == _expected_blobs(files)
    except PluginError:
        return False


def _remote_visibility(url: str) -> dict:
    """Ask the host now whether the repository is public (plugins_view.probe; never raises)."""
    import plugins_view  # imports this module
    key = plugins_view.repo_key(url)
    if not key:
        return {"visibility": "unknown", "error": f"认不出 {url} 是哪个托管上的仓库",
                "fix": "repo 写成 https://<托管>/<owner>/<仓库> 或 git@<托管>:<owner>/<仓库>.git"}
    return plugins_view.probe(key)


def _mark_pushed(url: str, seen=None) -> None:
    """Record in the visibility cache that the repository now has content (plugins_view.mark_pushed; never raises)."""
    import plugins_view  # imports this module
    plugins_view.mark_pushed(url, seen)


def _push_refusal(vis: str, seen: dict, url: str) -> str:
    """Why this push must not happen, or ""."""
    state = seen.get("visibility")
    if state not in ("public", "private"):
        return (f"查不到 {url} 是公开还是私有（{seen.get('error') or '原因不明'}），不推送。"
                f"修复：{seen.get('fix') or '确认能联网并已登录'}，再重跑 publish --yes")
    if state == "public" and vis != "public":
        return (f"{url} 是公开仓库，{label(vis)}插件绝不推送到公开仓。修复：把仓库改成私有，"
                f"或把清单里这个插件的 repo 换成私有仓，再重跑 publish --yes")
    return ""


def _github_id(owner: str):
    """The GitHub user id of `owner` (for its noreply address), or None."""
    try:
        p = subprocess.run(["gh", "api", f"users/{owner}", "--jq", ".id"], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = p.stdout.strip()
    return out if p.returncode == 0 and out.isdigit() else None


def _identity(url: str):
    """(name, email) to commit as. github.com: the repository owner's noreply address, so no real email
    goes into a plugin repository; elsewhere: the global git identity (None lets git decide)."""
    import plugins_view  # imports this module
    key = plugins_view.repo_key(url) or ""
    host, _, path = key.partition("/")
    glob = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True).stdout.strip()
    if host == "github.com":
        owner = path.split("/", 1)[0]
        uid = _github_id(owner)
        if not uid:
            raise PluginError(f"查不到 GitHub 用户 {owner} 的 id，拼不出 noreply 提交邮箱，没有提交。"
                              f"修复：确认 gh 已登录（gh auth login）且能联网，再重跑 publish")
        return glob or owner, f"{uid}+{owner}@users.noreply.github.com"
    email = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True).stdout.strip()
    return (glob, email) if glob and email else None


STEPS = {"build": _step_build, "scan": _step_scan, "commit": _step_commit, "push": _step_push}


def _still_done(step: str, name: str, files: dict, dest: Path) -> bool:
    """Whether a step the ledger records as done still holds: the output still equals the sources (build),
    the working clone's HEAD still holds them (commit). Once a step reruns, every later one reruns too."""
    if step == "build":
        return tree_files(dest) == files
    if step == "commit":
        return committed(name, files)
    return True


def publish(manifest: dict, names: list, rules_text, out, yes=False) -> int:
    """Run build → scan → commit → push per plugin, recording each finished step; a rerun with the same
    sources skips what is done. Local plugins are never published. push leaves this machine, so without
    `yes` it only prints what it would push (a dry run). 0 when every step ran, 1 otherwise."""
    ledger = read_ledger()
    code = 0
    for name in names:
        spec = get(manifest, name)
        if visibility(spec) == LOCAL:
            out.write(f"插件 {name}：本机插件（补丁层）不发布，跳过\n")
            continue
        files = render_plugin(manifest, name, rules_text)
        h = files_hash(files)
        dest = output_dir(manifest, name)
        with output_lock(dest):
            for note in recover(dest) + skill_warnings(manifest, name) + outside_notes(name, visibility(spec), files):
                out.write(f"插件 {name}：{note}\n")
            rec = ledger["plugins"].get(name)
            if not isinstance(rec, dict) or rec.get("input") != h:
                if isinstance(rec, dict):
                    out.write(f"插件 {name}：源变了，从生成重新开始\n")
                rec = {"input": h, "steps": {}}
                ledger["plugins"][name] = rec
            rerun = False
            for step in LEDGER_STEPS:
                done = rec["steps"].get(step)
                if done and not rerun and _still_done(step, name, files, dest):
                    out.write(f"插件 {name}：{step} 已完成（{done}），跳过\n")
                    continue
                rerun = True
                rec["steps"].pop(step, None)
                try:
                    msg = STEPS[step](manifest, name, files, dest, h, yes=yes, out=out)
                except DryRun as exc:
                    _save_ledger(ledger)
                    out.write(f"插件 {name}：{exc}：bin/agent-env plugin publish --plugin {name} --yes\n")
                    code = 1
                    break
                except NotYet as exc:
                    _save_ledger(ledger)
                    out.write(f"插件 {name}：未完成，停在 {step}：{exc}。已完成的步骤记在 {render._tilde(ledger_path())}；"
                              f"接好后重跑 bin/agent-env plugin publish --plugin {name}，会从 {step} 续上\n")
                    code = 1
                    break
                rec["steps"][step] = _now()
                _save_ledger(ledger)
                out.write(f"插件 {name}：{step} 完成，{msg}\n")
    return code


# ── pointer rule (指路规则) ───────────────────────────────────────────────

def pointer_plugins(manifest: dict, target: dict) -> list:
    """The target's non-local plugins, in order."""
    return [n for n in target_plugins(target) if n in table(manifest) and visibility(table(manifest)[n]) != LOCAL]


def pointer_missing(manifest: dict, target: dict) -> list:
    """Plugins of the pointer whose repository is not recorded yet (the pointer has a placeholder)."""
    return [n for n in pointer_plugins(manifest, target) if not table(manifest)[n].get("repo")]


def token_name(target: dict):
    """The target's token name when it is a plain secret name, else None (never a value)."""
    tok = target.get("token")
    name = tok.get("name") if isinstance(tok, dict) else None
    ok = isinstance(name, str) and ENV_VAR_RE.match(name) and len(name) <= SKILL_NAME_MAX and not looks_secret(name)
    return name if ok else None


def pointer_text(manifest: dict, target: dict) -> str:
    """The short rule a cloud-hosted target keeps in its own memory; ends with one newline."""
    tid = target.get("id")
    errs = target_errors(manifest, target)
    if errs:
        raise PluginError(f"落点「{tid}」没过校验，不输出指路规则：{errs[0][0]}。修复：{errs[0][1]}")
    names = pointer_plugins(manifest, target)
    if not names:
        raise PluginError(f"落点「{tid}」没有可拉取的插件。修复：在清单该落点写 plugins = [\"public\", ...]")
    for n in names:
        perrs = [e for e in plugin_errors(manifest, n, table(manifest)[n]) if "repo" in e[0]]
        if perrs:
            raise PluginError(f"{perrs[0][0]}。修复：{perrs[0][1]}")
    repos = [table(manifest)[n].get("repo") or f"<{label(visibility(table(manifest)[n]))}插件仓地址>" for n in names]
    secret = token_name(target)
    auth = f"私有仓用 secret {secret} 认证。" if secret else ""
    # third-party skills ship only as a project skills-lock.json (generated from skill_sources), so the
    # install step is spelled out only when some plugin of the pointer has one
    locked = any(table(manifest)[n].get("skill_sources") for n in names)
    lock = ("；有 skills-lock.json 的仓在仓里跑 npx -y skills experimental_install 后读 .agents/skills/，"
            "失败就按其 sourceUrl+skillPath 用 git 取，说明用了哪种" if locked else "")
    text = ("每次开工前在 /workspace 按顺序 git pull 这些插件仓（没有就 clone）："
            + "；".join(f"{i}. {r}" for i, r in enumerate(repos, 1)) + "。" + auth
            + "依次读各仓根目录 AGENTS.md，后读的覆盖先读的。技能用到再读：skills/ 直接读" + lock + "。"
            "拉取失败就用上次的版本并说明可能过期；从没拉成功过就停下问我。")
    limit = target.get("pointer_limit_chars", POINTER_LIMIT)
    if len(text) > limit:
        raise PluginError(f"落点「{tid}」的指路规则 {len(text)} 字符，超过上限 {limit}。"
                          f"修复：缩短插件仓地址，或减少该落点的插件数")
    return text + "\n"


# ── doctor cells ──────────────────────────────────────────────────────────

def overrides(manifest: dict, target: dict, rules_text) -> list:
    """[(what, item, earlier plugin, later plugin)] for each section or skill a later plugin
    of the target carries again; the later one wins."""
    seen = {}
    found = []
    for n in target_plugins(target):
        spec = table(manifest).get(n)
        if not isinstance(spec, dict):
            continue
        items = [("章节", h) for h in spec.get("sections", []) or []]
        try:
            items += [("章节", h) for h, _ in own_sections(manifest, spec)]
        except PluginError:
            pass
        items += [("skill", s) for s in (spec.get("skills", []) or []) + (spec.get("skill_sources", []) or [])
                  + [r.rsplit("/", 1)[-1] for r in spec.get("company_skills", []) or [] if isinstance(r, str)]]
        for item in dict.fromkeys(items):
            if item in seen and seen[item] != n:
                found.append((item[0], item[1], seen[item], n))
            seen[item] = n
    return found


def today() -> date:
    return date.today()


def token_state(target: dict, manifest: dict, now=None):
    """(state, detail, fix, warn) for a cloud-hosted target's read token. The expiry is a calendar date
    and the token counts as expired from the start of that day (the earliest it can stop working)."""
    now = now or today()
    private = [n for n in pointer_plugins(manifest, target) if visibility(table(manifest)[n]) != "public"]
    tok = target.get("token")
    renew = ("建 fine-grained token：只授权个人插件仓、Contents 只读、90 天到期，经 Grok Bot 的 secret card 交给它；"
             "再在清单该落点写 token = { name = \"<secret 名>\", expires = <到期日> }")
    if not isinstance(tok, dict):
        if not private:
            return "synced", "只用公开插件，不需要 token", "", False
        return "missing", f"还没登记读私有插件 {private} 的 token（清单只记名字和到期日）", renew, False
    errs = token_errors(target.get("id"), tok)
    if errs:
        return "drift", errs[0][0], errs[0][1], False
    name, expires = token_name(target), parse_date(tok.get("expires"))
    days = (expires - now).days
    if days <= 0:
        return "drift", f"token {name} 已于 {expires.isoformat()} 到期，私有插件拉不到了", renew.replace("建", "续建", 1), False
    if days <= WARN_DAYS:
        return "synced", f"token {name} 还有 {days} 天到期（{expires.isoformat()}），尽快续期", \
            renew.replace("建", "续建", 1), True
    return "synced", f"token {name} {expires.isoformat()} 到期，还有 {days} 天", "", False
