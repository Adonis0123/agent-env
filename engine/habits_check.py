#!/usr/bin/env python3
"""Validate a personal-layer habits manifest against its source files.

Usage: python3 habits_check.py <manifest> [--json]
Exit code 1 if any FAIL, 2 if the Python version is too old, else 0.
"""
import sys

if sys.version_info < (3, 11):
    print("habits_check 需要 Python 3.11+（依赖 tomllib），当前是 %d.%d；"
          "请改用 `uv run --python 3.12 python3 habits_check.py <清单>` 运行。"
          % sys.version_info[:2], file=sys.stderr)
    sys.exit(2)

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from habits import (LIMIT_UNITS, PREAMBLE_KEY, PUBLIC_FILE, RULES_SRC, join_rules_src, limit_unit,  # noqa: E402
                    load_manifest, measure, parse_sections,  # noqa: E402
                    select_sections, suspect_headings, unclosed_fence)  # noqa: E402
from render import RenderError, compose  # noqa: E402
import evals  # noqa: E402
import expect  # noqa: E402
import plugins  # noqa: E402
from mcp import allowed_in as mcp_allowed_in  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
WARN_RATIO = 0.8


class Report:
    def __init__(self):
        self.results = []

    def add(self, level, check, message, fix=""):
        self.results.append({"level": level, "check": check, "message": message, "fix": fix})

    @property
    def ok(self):
        return not any(r["level"] == FAIL for r in self.results)


def _is_str_list(value):
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) for x in value)


def _check_values(rep, check, label, meta, roles, visibility, need_roles=True):
    """Validate roles ⊆ manifest roles and visibility ∈ manifest visibility."""
    if not isinstance(meta, dict):
        rep.add(FAIL, check, f"{label} 不是表", f"把 {label} 写成带 roles 和 visibility 的 TOML 表")
        return False
    good = True
    if need_roles:
        r = meta.get("roles")
        if not _is_str_list(r):
            rep.add(FAIL, check, f"{label} 的 roles={r!r} 不是非空字符串列表",
                    f"给 {label} 写 roles = [...]，取值来自 {roles}")
            good = False
        else:
            bad = sorted(set(r) - set(roles))
            if bad:
                rep.add(FAIL, check, f"{label} 的 roles 含未定义角色 {bad}",
                        f"改成 {roles} 里的值，或先在顶层 roles 里登记 {bad}")
                good = False
    vis = meta.get("visibility")
    if not isinstance(vis, str) or vis not in visibility:
        rep.add(FAIL, check, f"{label} 的 visibility={vis!r} 无效",
                f"把 {label} 的 visibility 改成 {visibility} 之一")
        good = False
    return good


def check_schema(rep, m):
    c = "schema"
    if m.get("schema") != 1:
        rep.add(FAIL, c, f"schema={m.get('schema')!r}，只支持 1", "在清单顶部写 schema = 1")
    else:
        rep.add(PASS, c, "schema = 1")
    for key in ("roles", "visibility"):
        v = m.get(key)
        if not _is_str_list(v):
            rep.add(FAIL, c, f"顶层 {key} 不是非空字符串列表", f"在清单顶部写 {key} = [\"...\"]")
        else:
            rep.add(PASS, c, f"{key} = {v}")


def _rules_src_text(rep, c, m):
    """The joined text of [sources] rules_src (habits.join_rules_src) with its problems reported, plus the
    file-is-visibility convention: public.md holds exactly the sections registered public."""
    text, where, errs = join_rules_src(m)
    for msg, fix in errs:
        rep.add(FAIL, c, msg, fix)
    if text is None:
        return None
    registry = m.get("rule_sections") if isinstance(m.get("rule_sections"), dict) else {}
    for h, fname in where.items():
        meta = registry.get(h)
        vis = meta.get("visibility") if isinstance(meta, dict) else None
        label = "说明区（__preamble__）" if h == PREAMBLE_KEY else f"章节「{h}」"
        if vis is None:
            continue  # unregistered: reported below
        if fname == PUBLIC_FILE and vis != "public":
            rep.add(FAIL, c, f"{label} 写在 {PUBLIC_FILE}，却登记为 {vis}",
                    f"{PUBLIC_FILE} 只放公开的章节：把它移到 personal.md 或 local.md；确认能公开就把登记改成 public")
        elif fname != PUBLIC_FILE and vis == "public":
            rep.add(FAIL, c, f"{label} 登记为 public，却写在 {fname}",
                    f"公开的章节放 {PUBLIC_FILE}：把它移过去；不该公开就把登记改成 private")
    rep.add(PASS, c, f"规则源目录 {m['sources'][RULES_SRC]}：{len(set(where.values()))} 个文件，"
                     f"{len(where)} 个章节，按 [rule_sections] 顺序拼成全文 {len(text.encode('utf-8'))} B")
    return text


def check_rules(rep, m, roles, visibility):
    c = "rule_sections"
    if isinstance(m.get("sources", {}).get(RULES_SRC), str):
        text = _rules_src_text(rep, c, m)
        if text is None:
            return None
    else:
        path = m.get("sources", {}).get("rules")
        if not isinstance(path, str) or not Path(path).is_file():
            rep.add(FAIL, c, f"规则源文件不存在：{path}", "在 [sources] 里把 rules 指向存在的 AGENTS.md")
            return None
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            rep.add(FAIL, c, f"规则源文件读不了：{exc}", f"把 {path} 存成 UTF-8 文本并确认可读")
            return None
        fence_line = unclosed_fence(text)
        if fence_line:
            rep.add(FAIL, c, f"第 {fence_line} 行开启的代码块没有闭合，之后的章节无法识别",
                    f"在第 {fence_line} 行开启的代码块后补上闭合围栏")
        for lineno, kind in suspect_headings(text):
            rep.add(FAIL, c, f"第 {lineno} 行疑似未识别的标题（{kind}），会被并进上一章节",
                    "若是标题，改成行首 `## 标题` 并在清单登记；若是分隔线，在它前面加一个空行")
    preamble, sections = parse_sections(text)
    headings = [h for h, _ in sections]
    for h in sorted({h for h in headings if headings.count(h) > 1}):
        rep.add(FAIL, c, f"章节标题「{h}」重复出现 {headings.count(h)} 次",
                "给重复的章节改成不同标题")
    registry = m.get("rule_sections", {})
    if not isinstance(registry, dict):
        rep.add(FAIL, c, "[rule_sections] 不是表", "按 [rule_sections.\"<标题>\"] 的格式登记章节")
        return text
    if preamble.strip():
        headings_with_preamble = [PREAMBLE_KEY] + headings
        if PREAMBLE_KEY not in registry:
            rep.add(FAIL, c, "第一个 `## ` 之前的说明区未登记，不会输出到任何落点",
                    f"在清单登记 [rule_sections.\"{PREAMBLE_KEY}\"]，写 roles 和 visibility")
    else:
        headings_with_preamble = headings
    for h in headings:
        if h not in registry:
            rep.add(FAIL, c, f"章节「{h}」未在 [rule_sections] 登记",
                    f"在清单里加 [rule_sections.\"{h}\"]，写 roles 和 visibility")
    for h, meta in registry.items():
        if h == PREAMBLE_KEY and h not in headings_with_preamble:
            # Registered but the preamble is empty: allowed, still validate values.
            _check_values(rep, c, "说明区（__preamble__）", meta, roles, visibility)
            continue
        if h not in headings_with_preamble:
            rep.add(FAIL, c, f"[rule_sections.\"{h}\"] 在规则文件里已不存在",
                    "删掉该条登记，或把键改成现在的章节标题")
            continue
        label = "说明区（__preamble__）" if h == PREAMBLE_KEY else f"章节「{h}」"
        if _check_values(rep, c, label, meta, roles, visibility):
            rep.add(PASS, c, f"{label}：roles={meta['roles']} visibility={meta['visibility']}")
    return text


def _walk_md(root, errors=None):
    """All *.md under root as relative posix paths, following symlinked dirs once.

    Any directory or entry that cannot be listed or stat'ed is appended to
    ``errors`` (re-raised when ``errors`` is None) instead of being skipped
    silently, so the caller can report it. os.walk is not used because it
    swallows stat errors on entries without calling onerror.
    """
    root = Path(root)
    found, seen = [], set()

    def record(err):
        if errors is None:
            raise err
        errors.append(err)

    def visit(directory):
        real = os.path.realpath(directory)
        if real in seen:  # symlink loop or a second link to the same dir
            return
        seen.add(real)
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as err:
            record(err)
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir()  # follows symlinks
                is_file = not is_dir and entry.is_file()
            except OSError as err:
                record(err)
                continue
            if is_dir:
                visit(entry.path)
            elif is_file and entry.name.endswith(".md"):
                found.append(Path(entry.path).relative_to(root).as_posix())

    visit(root)
    return sorted(found)


def _check_file_registry(rep, c, kind, root, registry, visibility, discovered):
    if not isinstance(registry, dict):
        rep.add(FAIL, c, f"[{c}] 不是表", f"按 \"<相对路径>\" = \"private\" 的格式登记 [{c}]")
        return
    for rel in discovered:
        if rel not in registry:
            rep.add(FAIL, c, f"{kind}「{rel}」未在 [{c}] 登记",
                    f"在 [{c}] 加一行 \"{rel}\" = \"private\"（或 public）")
    for rel, vis in registry.items():
        norm = os.path.normpath(rel)
        if os.path.isabs(rel) or norm == ".." or norm.startswith(".." + os.sep):
            rep.add(FAIL, c, f"[{c}] 登记的「{rel}」不在{kind}目录内",
                    f"登记路径必须在 {kind} 目录内")
            continue
        if not (Path(root) / rel).is_file():
            rep.add(FAIL, c, f"[{c}] 登记的「{rel}」在{kind}目录里不存在",
                    f"删掉 [{c}] 里的 \"{rel}\"，或恢复该文件")
        elif not isinstance(vis, str) or vis not in visibility:
            rep.add(FAIL, c, f"「{rel}」的可见性 {vis!r} 无效", f"改成 {visibility} 之一")
        else:
            rep.add(PASS, c, f"{kind}「{rel}」：{vis}")


def check_docs(rep, m, visibility):
    root = m.get("sources", {}).get("docs")
    if not isinstance(root, str) or not Path(root).is_dir():
        rep.add(FAIL, "docs", f"docs 目录不存在：{root}", "在 [sources] 里把 docs 指向存在的目录")
        return
    errors = []
    discovered = _walk_md(root, errors)
    for err in errors:
        where = getattr(err, "filename", None) or root
        rep.add(FAIL, "docs", f"无法读取 docs 下的目录：{where}（{type(err).__name__}）",
                "修好该目录的读取权限，或把它移出 docs，否则其中的文档无法核对登记")
    _check_file_registry(rep, "docs", "文档", root, m.get("docs", {}), visibility, discovered)


def check_hooks(rep, m, visibility):
    root = m.get("sources", {}).get("hooks")
    if not isinstance(root, str) or not Path(root).is_dir():
        rep.add(FAIL, "hooks", f"hooks 目录不存在：{root}", "在 [sources] 里把 hooks 指向存在的目录")
        return
    # Only wired hooks are registered, so unregistered scripts are not reported.
    _check_file_registry(rep, "hooks", "hook 脚本", root, m.get("hooks", {}), visibility, [])


def _target_field_errors(t, roles, visibility):
    """Return (message, fix) pairs for wrongly typed or invalid target fields."""
    tid = t.get("id")
    errs = []
    r = t.get("roles")
    if not _is_str_list(r) or set(r) - set(roles):
        errs.append((f"落点「{tid}」的 roles={r!r} 无效", f"改成 {roles} 里的非空子集"))
    if "visibility" in t and t["visibility"] not in visibility:
        errs.append((f"落点「{tid}」的 visibility={t['visibility']!r} 无效",
                     f"改成 {visibility} 之一，或删掉该字段"))
    limit = t.get("rule_limit_bytes", 0)
    if isinstance(limit, bool) or not isinstance(limit, int):
        errs.append((f"落点「{tid}」的 rule_limit_bytes={limit!r} 不是整数",
                     "写成不带引号的整数，未核实就写 0"))
    elif limit < 0:
        errs.append((f"落点「{tid}」的 rule_limit_bytes={limit} 是负数", "写成 0（未核实）或宿主的实际上限"))
    if "rule_limit_unit" in t and t["rule_limit_unit"] not in LIMIT_UNITS:
        errs.append((f"落点「{tid}」的 rule_limit_unit={t['rule_limit_unit']!r} 无效",
                     f"写成 {sorted(LIMIT_UNITS)} 之一（宿主按字符截断就写 \"chars\"），或删掉该字段按 bytes 计"))
    if "verified" in t and not isinstance(t["verified"], bool):
        errs.append((f"落点「{tid}」的 verified={t['verified']!r} 不是布尔值",
                     "写成不带引号的 true 或 false"))
    if "supplement" in t:
        sup = t["supplement"]
        if not isinstance(sup, str) or not sup:
            errs.append((f"落点「{tid}」的 supplement={sup!r} 不是路径字符串",
                         "写成档案路径字符串，相对清单目录，如 \"../profiles/mac/hermes.md\""))
        elif not Path(sup).is_file():
            errs.append((f"落点「{tid}」的档案不存在：{sup}",
                         "创建该档案文件，或把 supplement 改成存在的路径（相对清单目录）"))
    if "output" in t:
        out = t["output"]
        if not isinstance(out, str) or not out.startswith("~/"):
            errs.append((f"落点「{tid}」的 output={out!r} 不是 ~ 开头的路径",
                         "写成 \"~/...\" 形式的生成文件路径，引擎运行时展开"))
    if "also_output" in t:
        extra = t["also_output"]
        if not isinstance(extra, list) or not extra or \
                not all(isinstance(x, str) and x.startswith("~/") for x in extra):
            errs.append((f"落点「{tid}」的 also_output={extra!r} 不是 ~ 开头的路径列表",
                         "写成 also_output = [\"~/...\"]（和 output 写同一份内容的额外路径），用不到就删掉该字段"))
        elif t.get("output") in extra or len(set(extra)) != len(extra):
            errs.append((f"落点「{tid}」的 also_output 有重复路径（含与 output 重复）",
                         "删掉重复项，每个路径只写一次"))
        if t.get("render") != "generated":
            errs.append((f"落点「{tid}」有 also_output 但没有声明 render = \"generated\"",
                         "also_output 只给引擎生成的文件用：补上 render = \"generated\" 和 output，或删掉 also_output"))
    if "render" in t:
        if t["render"] != "generated":
            errs.append((f"落点「{tid}」的 render={t['render']!r} 无效",
                         "只支持 render = \"generated\"（引擎负责该文件）；不由引擎负责就删掉该字段"))
        elif "output" not in t:
            errs.append((f"落点「{tid}」声明 render = \"generated\" 但没有 output",
                         "补上 output = \"~/...\"，写明生成文件的位置"))
    if "skills_disabled" in t:
        sd = t["skills_disabled"]
        if not isinstance(sd, list) or not all(isinstance(x, str) and x for x in sd):
            errs.append((f"落点「{tid}」的 skills_disabled={sd!r} 不是字符串列表",
                         "写成 skills_disabled = [\"name\", ...]，没有禁用项就写 []"))
        if not isinstance(t.get("skills_config"), str) or not t["skills_config"].startswith("~/"):
            errs.append((f"落点「{tid}」有 skills_disabled 但 skills_config={t.get('skills_config')!r} 无效",
                         "补上 skills_config = \"~/...\"，指向宿主里写 skills.disabled 的配置文件"))
    elif "skills_config" in t:
        errs.append((f"落点「{tid}」有 skills_config 但没有 skills_disabled",
                     "补上 skills_disabled = [...]（当前禁用列表），或删掉 skills_config"))
    errs += _expect_errors(t)
    return errs


REMOTE_EXEC = "remote-exec"


def _expect_errors(t):
    """(message, fix) pairs for a target's [[targets.expect]] list and host_version."""
    tid = t.get("id")
    errs = []
    hv = t.get("host_version")
    if hv is not None:
        cmd = hv.get("cmd") if isinstance(hv, dict) else None
        if (not isinstance(hv, dict) or not _is_str_list(cmd)
                or not isinstance(hv.get("recorded"), str) or not hv["recorded"]):
            errs.append((f"落点「{tid}」的 host_version={hv!r} 无效",
                         "写成 host_version = { cmd = [\"<命令>\", \"--version\"], recorded = \"<版本>\" }"))
    exps = t.get("expect", [])
    if not isinstance(exps, list):
        return errs + [(f"落点「{tid}」的 expect 不是数组", "每条期望写成一个 [[targets.expect]] 表")]
    for i, e in enumerate(exps, 1):
        errs += expect.validate(e, t, f"落点「{tid}」第 {i} 条期望")  # kind, habit, the kind's fields, name
    return errs


def _eval_errors(t, m):
    """(message, fix) pairs for eval_cases / eval_skip / skills of one target."""
    tid = t.get("id")
    errs = []
    if "eval_cases" in t and "eval_skip" in t:
        errs.append((f"落点「{tid}」同时写了 eval_cases 和 eval_skip", "能抽测就只留 eval_cases，不能就只留 eval_skip"))
    if "eval_skip" in t and (not isinstance(t["eval_skip"], str) or not t["eval_skip"].strip()):
        errs.append((f"落点「{tid}」的 eval_skip 不是非空字符串", "写明为什么不能抽测，如 \"没有无头 CLI\""))
    cases = t.get("eval_cases")
    if cases is not None:
        if not _is_str_list(cases) or not cases:
            errs.append((f"落点「{tid}」的 eval_cases={cases!r} 不是非空字符串列表",
                         "写成 eval_cases = [\"case\", ...]（3–5 个）"))
        else:
            host = t.get("host")
            if host in evals.RUNNER_HOSTS:
                script = m.get("sources", {}).get("eval")
                if not isinstance(script, str) or not Path(script).is_file():
                    errs.append((f"落点「{tid}」有 eval_cases，但 [sources] eval={script!r} 不是存在的文件",
                                 "在 [sources] 写 eval = \"~/.../agents-md-eval.zsh\""))
                else:
                    cases_dir = Path(script).parent / evals.RUNNER_CASES_DIR
                    bad = [c for c in cases if not (cases_dir / c / "check.zsh").is_file()]
                    if bad:
                        errs.append((f"落点「{tid}」的用例 {bad} 在 {cases_dir} 里不存在",
                                     f"改成 {cases_dir} 下已有的用例目录名"))
            elif host in evals.BOT_COMMANDS:
                bad = [c for c in cases if c not in evals.BOT_CASES]
                if bad:
                    errs.append((f"落点「{tid}」的 bot 用例 {bad} 不存在", f"改成 {sorted(evals.BOT_CASES)} 之一"))
            else:
                errs.append((f"落点「{tid}」的宿主 {host!r} 没有测评适配", "删掉 eval_cases，改写 eval_skip = \"原因\""))
    skills = t.get("skills")
    if skills is not None:
        root = m.get("sources", {}).get("skills")
        if not _is_str_list(skills):
            errs.append((f"落点「{tid}」的 skills={skills!r} 不是字符串列表", "写成 skills = [\"name\", ...]"))
        elif not isinstance(root, str) or not Path(root).is_dir():
            errs.append((f"落点「{tid}」有 skills，但 [sources] skills={root!r} 不是存在的目录",
                         "在 [sources] 写 skills = \"~/.agents/skills\""))
        else:
            bad = [x for x in skills if not (Path(root) / x / "SKILL.md").is_file()]
            if bad:
                errs.append((f"落点「{tid}」选的 skill {bad} 在 {root} 里没有 SKILL.md", "改成共享 skill 目录里已有的名字"))
    return errs


def check_targets(rep, m, roles, visibility, rules_text):
    c = "targets"
    targets = m.get("targets", [])
    if not isinstance(targets, list):
        rep.add(FAIL, c, "targets 不是 [[targets]] 数组", "每个落点写成一个 [[targets]] 表")
        return
    seen = set()
    for t in targets:
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or not tid:
            rep.add(FAIL, c, "有落点缺少字符串 id", "给每个 [[targets]] 写唯一的 id")
            continue
        if tid in seen:
            rep.add(FAIL, c, f"落点 id「{tid}」重复", f"把重复的「{tid}」改成唯一 id")
            continue
        seen.add(tid)
        errs = (_target_field_errors(t, roles, visibility) + _eval_errors(t, m) + _remote_target_errors(t, m)
                + _company_skills_errors(t, m) + plugins.target_errors(m, t))
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if errs:
            continue
        limit = t.get("rule_limit_bytes", 0)
        if "plugins" in t and "pull" in t:
            # a host that pulls its plugins: what it reads is the plugins' AGENTS.md joined in order
            try:
                import pull  # engine/pull.py imports plugins, which this module also imports
                text = pull.joined_rules(m, t, rules_text) if rules_text is not None else ""
                unit = limit_unit(t)
                size = measure(text, unit)
                if isinstance(limit, int) and 0 < limit < size:
                    rep.add(FAIL, c, f"落点「{tid}」拼接后的规则 {size} {LIMIT_UNITS.get(unit, unit)}，超过上限 {limit}",
                            "从插件里移出章节或 skill 说明，或换一个插件组合")
                else:
                    rep.add(PASS, c, f"落点「{tid}」：插件 {t['plugins']}，远程自己拉取，拼接后 {size} "
                                     f"{LIMIT_UNITS.get(unit, unit)}" + (f" / 上限 {limit}" if limit else ""))
            except plugins.PluginError as exc:
                msg, _, fix = str(exc).partition("。修复：")
                rep.add(FAIL, c, msg, fix or "按报错修正清单")
        elif "plugins" in t and "rules_output" in t:
            # one of the user's machines: the plugins are composed into one file there (engine/compose.py)
            try:
                import compose as composer  # engine/compose.py; `compose` here is render's role-based one
                text = composer.render_text(m, t, rules_text) if rules_text is not None else ""
                unit = limit_unit(t)
                size = measure(text, unit)
                if isinstance(limit, int) and 0 < limit < size:
                    rep.add(FAIL, c, f"落点「{tid}」合成后的规则 {size} {LIMIT_UNITS.get(unit, unit)}，超过上限 {limit}",
                            "从插件里移出章节，或换一个插件组合")
                else:
                    rep.add(PASS, c, f"落点「{tid}」：插件 {t['plugins']}，本机合成到 {t['rules_output']}，{size} "
                                     f"{LIMIT_UNITS.get(unit, unit)}" + (f" / 上限 {limit}" if limit else ""))
            except composer.ComposeError as exc:
                msg, _, fix = str(exc).partition("。修复：")
                rep.add(FAIL, c, msg, fix or "按报错修正清单或规则源")
        elif "plugins" in t:
            # a plugin target gets no composed rules file: what is measured is its pointer rule
            try:
                size = len(plugins.pointer_text(m, t).rstrip("\n"))
                rep.add(PASS, c, f"落点「{tid}」：插件 {t['plugins']}，指路规则 {size} 字符 / 上限 "
                                 f"{t.get('pointer_limit_chars', plugins.POINTER_LIMIT)} 字符")
            except plugins.PluginError as exc:
                msg, _, fix = str(exc).partition("。修复：")
                rep.add(FAIL, c, msg, fix or "按报错修正清单")
                continue
        elif limit > 0 and rules_text is not None:
            # Measure what render/deliver produce: any target that gets a composed
            # file (profile, generated note, skill list) is checked on compose();
            # link-only targets receive the bare sections.
            composed = ("supplement" in t or t.get("render") == "generated" or bool(t.get("skills"))
                        or any(isinstance(e, dict) and e.get("kind") == "delivered"
                               for e in t.get("expect", []) or []))
            if composed:
                try:
                    body = compose(m, rules_text, tid)  # profile + note + sections + skills
                except RenderError as exc:
                    rep.add(FAIL, c, f"落点「{tid}」生成失败：{exc}", "按报错修正档案或规则源")
                    continue
            else:
                body = select_sections(m, rules_text, tid)
            unit = limit_unit(t)
            label = LIMIT_UNITS[unit]
            size = measure(body, unit)
            msg = f"落点「{tid}」规则 {size} {label} / 上限 {limit} {label}（{size * 100 // limit}%）"
            if size > limit:
                rep.add(FAIL, c, msg + "，超限",
                        f"在 [rule_sections] 里把部分章节从「{tid}」的角色移出，或精简规则文件")
            elif size > limit * WARN_RATIO:
                rep.add(WARN, c, msg + "，超过 80%", f"精简规则文件，或减少「{tid}」角色收到的章节")
            else:
                rep.add(PASS, c, msg)
        else:
            rep.add(PASS, c, f"落点「{tid}」：roles={t['roles']}")
        if t.get("verified") is not True:
            note = "缺少 verified 字段，按未核实处理；" if "verified" not in t else ""
            rep.add(WARN, "verified", f"落点「{tid}」{note}规则加载方式未核实",
                    f"在该宿主上实测规则文件是否被加载，核实后把「{tid}」的 verified 写成 true")


def _company_skills_errors(t, m):
    """(message, fix) pairs for company_skills / company_skills_dir / company_skills_backup."""
    if "company_skills" not in t:
        for key in ("company_skills_dir", "company_skills_backup"):
            if key in t:
                return [(f"落点「{t.get('id')}」有 {key} 但没有 company_skills", "补上 company_skills = [...]，或删掉该字段")]
        return []
    tid, rels = t.get("id"), t["company_skills"]
    if not _is_str_list(rels) or not rels:
        return [(f"落点「{tid}」的 company_skills={rels!r} 不是非空字符串列表", "写成 company_skills = [\"metrics-query\", ...]")]
    errs = []
    if len(set(rels)) != len(rels):
        errs.append((f"落点「{tid}」的 company_skills 有重复项", "每个 skill 只写一次"))
    for key in ("company_skills_dir", "company_skills_backup"):
        v = t.get(key)
        if (key == "company_skills_dir" or v is not None) and (not isinstance(v, str) or not v.startswith("~/")):
            errs.append((f"落点「{tid}」的 {key}={v!r} 不是 ~/ 开头的路径",
                         f"写成 {key} = \"~/.hermes/skills\" 这类路径（远程环境里 ~ 是 remote_home）"))
    if not any(isinstance(e, dict) and e.get("kind") == "company-skills" for e in t.get("expect", []) or []):
        errs.append((f"落点「{tid}」有 company_skills 但没有 company-skills 期望，doctor 不会核对",
                     "加一条 [[targets.expect]] kind = \"company-skills\" habit = \"skills\""))
    envs = m.get("environments") if isinstance(m.get("environments"), dict) else {}
    spec = envs.get(t.get("env"))
    if isinstance(spec, dict) and spec.get("transport") == REMOTE_EXEC and not isinstance(spec.get("remote_home"), str):
        errs.append((f"落点「{tid}」要装公司 skill，但环境没有 remote_home", "在环境表写 remote_home = \"/home/<用户>\""))
    import company_skills
    errs += [(f"落点「{tid}」：{msg}", fix) for msg, fix in company_skills.source_errors(m, rels)]
    return errs


def _remote_target_errors(t, m):
    """(message, fix) pairs tying a target's expectations to its environment's transport."""
    envs = m.get("environments") if isinstance(m.get("environments"), dict) else {}
    spec = envs.get(t.get("env"))
    is_remote = isinstance(spec, dict) and spec.get("transport") == REMOTE_EXEC
    tid = t.get("id")
    errs = expect.env_errors(t, spec if is_remote else None)  # e.g. service only remote, delivered never
    if is_remote:
        if "also_output" in t:
            errs.append((f"落点「{tid}」在远程环境里，deliver 和 doctor 只处理 output，also_output 不会生效",
                         "删掉 also_output；远程只支持一个 output"))
        for key in ("mcp_config", "skills_config", "host_version"):
            if key in t:
                errs.append((f"落点「{tid}」在远程环境里，{key} 会在本机读取或执行，结果不对",
                             f"删掉 {key}；远程核对用 [[targets.expect]]"))
    return errs


def _cmd_errors(label, cmd, base):
    if not _is_str_list(cmd):
        return [(f"{label} 不是非空字符串列表", "写成 [\"<程序>\", \"<参数>\", ...]")]
    errs = []
    for item in cmd:
        if item.startswith(("./", "../", "~/")):
            path = Path(os.path.expanduser(item)) if item.startswith("~/") else base / item
            if not path.exists():
                errs.append((f"{label} 里的 {item} 不存在", "改成存在的脚本路径（./ ../ 相对清单目录）"))
    return errs


def check_environments(rep, m):
    c = "environments"
    envs = m.get("environments", {})
    if not isinstance(envs, dict):
        rep.add(FAIL, c, "[environments] 不是表", "按 [environments.<名字>] 的格式登记环境")
        return
    base = Path(m.get("__manifest_path__", ".")).parent
    for name, spec in envs.items():
        label = f"[environments.{name}]"
        if not isinstance(spec, dict):
            rep.add(FAIL, c, f"{label} 不是表", "写成带 platform 或 transport 的表")
            continue
        transport = spec.get("transport")
        home = spec.get("home")
        if home is not None and (not isinstance(home, str) or not home.startswith("/") or home == "/"):
            rep.add(FAIL, c, f"{label} 的 home={home!r} 不是绝对路径",
                    "写成该环境家目录的绝对路径，如 home = \"/home/<用户>\"，或删掉")
        sync = spec.get("sync_command")
        if sync is not None and (not isinstance(sync, str) or not sync.strip() or "\n" in sync):
            rep.add(FAIL, c, f"{label} 的 sync_command={sync!r} 无效",
                    "写成这台机器同步时运行的一行命令（它会重新合成规则文件），或删掉")
        if transport is None:
            continue
        if transport == plugins.CLOUD_HOSTED:
            errs = plugins.env_errors(name, spec)
            df = spec.get("driven_from")
            if isinstance(df, str) and df not in envs:
                errs.append((f"{label} 的 driven_from={df!r} 不是 [environments] 里的环境", f"改成 {sorted(envs)} 之一"))
            for msg, fix in errs:
                rep.add(FAIL, c, msg, fix)
            if not errs:
                rep.add(PASS, c, f"{label}：{plugins.CLOUD_HOSTED}，从 {df or '任一环境'} 检查")
            continue
        if transport != REMOTE_EXEC:
            rep.add(FAIL, c, f"{label} 的 transport={transport!r} 无效",
                    f"写成 \"{REMOTE_EXEC}\" 或 \"{plugins.CLOUD_HOSTED}\"，本机环境不写 transport")
            continue
        errs = _cmd_errors(f"{label} exec", spec.get("exec"), base)
        dv = spec.get("deny_visibility")
        if dv is not None and (not isinstance(dv, list) or not dv or not all(isinstance(v, str) for v in dv)
                               or set(dv) - set(plugins.VISIBILITY)):
            errs.append((f"{label} 的 deny_visibility 无效", f"写成 {sorted(plugins.VISIBILITY)} 里的值组成的列表"))
        if "send" in spec:
            errs += _cmd_errors(f"{label} send", spec.get("send"), base)
        for key in ("remote_home", "remote_user"):
            v = spec.get(key)
            if v is not None and (not isinstance(v, str) or not v or (key == "remote_home" and not v.startswith("/"))):
                errs.append((f"{label} 的 {key}={v!r} 无效", "remote_home 写远程绝对路径，remote_user 写远程用户名"))
        for key in ("timeout", "chunk_bytes"):
            v = spec.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
                errs.append((f"{label} 的 {key}={v!r} 不是正整数", f"写成不带引号的正整数，或删掉用默认值"))
        if "platform" in spec:
            errs.append((f"{label} 是远程环境却写了 platform", "删掉 platform：远程环境只能用 --env 指定，不按本机推断"))
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if not errs:
            rep.add(PASS, c, f"{label}：{REMOTE_EXEC}，exec = {spec['exec'][0]} …")


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MCP_TRANSPORTS = ("stdio", "http")
MCP_HOSTS = ("claude-code", "codex", "grok", "pi", "cursor", "hermes", "workbuddy")


def _mcp_entry_errors(name, spec, visibility, env_names=None):
    """(message, fix) pairs for one [mcp.<name>] table. env_names: the [environments] names
    an `envs` list may use; None skips that check."""
    label = f"[mcp.{name}]"
    if not isinstance(spec, dict):
        return [(f"{label} 不是表", f"写成 {label} 表，带 transport、command 或 url、env、visibility")]
    errs = []
    known = {"transport", "command", "args", "url", "env", "auth_env", "visibility", "envs"}
    extra = sorted(set(spec) - known)
    if extra:
        errs.append((f"{label} 有未知字段 {extra}", f"只用 {sorted(known)}；密钥值不要写进清单"))
    transport = spec.get("transport")
    if transport not in MCP_TRANSPORTS:
        errs.append((f"{label} 的 transport={transport!r} 无效", f"写成 {list(MCP_TRANSPORTS)} 之一"))
    elif transport == "stdio":
        if not isinstance(spec.get("command"), str) or not spec["command"]:
            errs.append((f"{label} 是 stdio 但没有 command", "写 command = \"<命令名>\"，参数放 args"))
        if "url" in spec:
            errs.append((f"{label} 是 stdio 却写了 url", "删掉 url，或把 transport 改成 http"))
        args = spec.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errs.append((f"{label} 的 args 不是字符串列表", "写成 args = [\"...\"]，没有参数就删掉"))
    else:
        url = spec.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
            errs.append((f"{label} 是 http 但 url={url!r} 不是完整 URL", "写成 url = \"https://主机/路径\""))
        elif parsed.username or parsed.password or parsed.query or parsed.fragment:
            errs.append((f"{label} 的 url 带用户信息、查询串或片段，可能含 token",
                         "只写不含 token 的 URL；token 放环境变量，用 auth_env 写变量名"))
        if "command" in spec or "args" in spec:
            errs.append((f"{label} 是 http 却写了 command 或 args", "删掉 command、args，或把 transport 改成 stdio"))
    env = spec.get("env", [])
    if not isinstance(env, list) or not all(isinstance(v, str) and _ENV_NAME_RE.match(v) for v in env):
        errs.append((f"{label} 的 env 不是变量名列表", "写成 env = [\"VAR_NAME\"]，只写变量名，不写值"))
    if "auth_env" in spec:
        auth = spec["auth_env"]
        if not isinstance(auth, str) or not _ENV_NAME_RE.match(auth):
            errs.append((f"{label} 的 auth_env 不是变量名", "写成 auth_env = \"VAR_NAME\"（存 Bearer token 的变量名）"))
        elif transport != "http":
            errs.append((f"{label} 写了 auth_env 但不是 http", "auth_env 只用于 http；stdio 的变量写进 env"))
    vis = spec.get("visibility")
    if vis not in visibility:
        errs.append((f"{label} 的 visibility={vis!r} 无效", f"改成 {visibility} 之一"))
    if "envs" in spec:
        envs = spec["envs"]
        if not isinstance(envs, list) or not envs or not all(isinstance(e, str) and e for e in envs):
            errs.append((f"{label} 的 envs={envs!r} 不是非空的环境名列表",
                         "写成 envs = [\"mac\"]（只能在这些环境注册）；哪里都能用就删掉 envs"))
        else:
            unknown = sorted({e for e in envs if env_names is not None and e not in env_names})
            if unknown:
                errs.append((f"{label} 的 envs 里 {unknown} 不是 [environments] 里的环境",
                             f"改成 {sorted(env_names)} 里的名字，或先登记 [environments.<名字>]"))
            if len(set(envs)) != len(envs):
                errs.append((f"{label} 的 envs 有重复项", "每个环境只写一次"))
    return errs


def _mcp_target_errors(t, servers, targets):
    """(message, fix) pairs for a target's mcp / mcp_config / compat fields."""
    tid = t.get("id")
    fields = ("mcp", "mcp_config", "mcp_compat_from", "mcp_unmanaged")
    if not any(f in t for f in fields):
        return []
    errs = []
    config = t.get("mcp_config")
    if not isinstance(config, str) or not config.startswith("~/"):
        errs.append((f"落点「{tid}」有 MCP 字段但 mcp_config={config!r} 无效",
                     "写 mcp_config = \"~/...\"，指向宿主的 MCP 配置文件"))
    if t.get("host") not in MCP_HOSTS:
        errs.append((f"落点「{tid}」的宿主 {t.get('host')!r} 没有 MCP 读取器",
                     f"删掉该落点的 MCP 字段，或先在 engine/mcp.py 加读取器（已支持 {list(MCP_HOSTS)}）"))
    for field in ("mcp", "mcp_compat_from", "mcp_unmanaged"):
        v = t.get(field, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            errs.append((f"落点「{tid}」的 {field} 不是字符串列表", f"写成 {field} = [\"...\"]，没有就写 []"))
    if "mcp" not in t:
        errs.append((f"落点「{tid}」有 mcp_config 但没有 mcp", "补上 mcp = [...]（期望启用的 server），没有就写 []"))
    names = t.get("mcp") if isinstance(t.get("mcp"), list) else []
    for name in names:
        if isinstance(name, str) and name not in servers:
            errs.append((f"落点「{tid}」的 mcp 引用了未定义的 server「{name}」",
                         f"在清单加 [mcp.{name}]，或从该落点的 mcp 里删掉"))
        elif isinstance(name, str) and not mcp_allowed_in({"mcp": servers}, name, t.get("env")):
            errs.append((f"落点「{tid}」在 {t.get('env')} 环境，但 mcp 里的「{name}」"
                         f"只能在 {servers[name].get('envs')} 注册",
                         f"从该落点的 mcp 里删掉「{name}」，或把 {t.get('env')} 加进 [mcp.{name}] envs"))
    lowered = [n.lower() for n in names if isinstance(n, str)]
    for dup in sorted({n for n in lowered if lowered.count(n) > 1}):
        errs.append((f"落点「{tid}」的 mcp 里「{dup}」重复（不区分大小写）", "只保留一个"))
    unmanaged = t.get("mcp_unmanaged") if isinstance(t.get("mcp_unmanaged"), list) else []
    both = sorted({n for n in unmanaged if isinstance(n, str)} & {n for n in names if isinstance(n, str)})
    if both:
        errs.append((f"落点「{tid}」的 {both} 同时在 mcp 和 mcp_unmanaged", "二选一：由清单管就留在 mcp"))
    compat = t.get("mcp_compat_from") if isinstance(t.get("mcp_compat_from"), list) else []
    for host in compat:
        if host == t.get("host"):
            errs.append((f"落点「{tid}」的 mcp_compat_from 含它自己的宿主 {host}", "删掉这一项"))
        elif not any(isinstance(o, dict) and o.get("host") == host and o.get("env") == t.get("env")
                     and isinstance(o.get("mcp_config"), str) for o in targets):
            errs.append((f"落点「{tid}」的 mcp_compat_from 里 {host} 在 {t.get('env')} 环境没有带 mcp_config 的落点",
                         f"给 {t.get('env')} 的 {host} 落点写 mcp_config，或删掉这一项"))
    return errs


def check_mcp(rep, m, visibility):
    c = "mcp"
    servers = m.get("mcp", {})
    if not isinstance(servers, dict):
        rep.add(FAIL, c, "[mcp] 不是表", "按 [mcp.<名字>] 的格式登记 MCP server")
        return
    envs = m.get("environments")
    env_names = set(envs) if isinstance(envs, dict) else set()
    for name, spec in servers.items():
        errs = _mcp_entry_errors(name, spec, visibility, env_names)
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if not errs:
            where = spec.get("url") or spec.get("command")
            only = f"，只在 {spec['envs']}" if "envs" in spec else ""
            rep.add(PASS, c, f"[mcp.{name}]：{spec['transport']} {where}{only}")
    lowered = [n.lower() for n in servers]
    for dup in sorted({n for n in lowered if lowered.count(n) > 1}):
        rep.add(FAIL, c, f"[mcp] 里有只差大小写的同名 server「{dup}」", "合并成一个名字")
    targets = m.get("targets", []) if isinstance(m.get("targets"), list) else []
    for t in targets:
        if not isinstance(t, dict):
            continue
        errs = _mcp_target_errors(t, servers, targets)
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if not errs and "mcp_config" in t:
            rep.add(PASS, c, f"落点「{t.get('id')}」：mcp={t.get('mcp')}")


def check_names(rep, m):
    """[host_names] / [env_names] / [eval_case_names]: display names for the panel's issues."""
    c = "names"
    for key in ("host_names", "env_names", "eval_case_names"):
        t = m.get(key, {})
        if not isinstance(t, dict) or not all(isinstance(v, str) and v.strip() for v in t.values()):
            rep.add(FAIL, c, f"[{key}] 不是「原名 = \"显示名\"」的表", f"在 [{key}] 下每行写 \"<原名>\" = \"<中文名>\"")
    names = m.get("eval_case_names") if isinstance(m.get("eval_case_names"), dict) else {}
    used = sorted({x for t in m.get("targets", []) if isinstance(t, dict)
                   for x in (t.get("eval_cases") if _is_str_list(t.get("eval_cases")) else [])})
    unnamed = [x for x in used if x not in names]
    if unnamed:
        rep.add(WARN, c, f"抽测用例没有中文名：{unnamed}（面板会显示原名）",
                "在 [eval_case_names] 里给它们各写一个短的中文名")
    else:
        rep.add(PASS, c, f"{len(used)} 个抽测用例都有中文名")


def check_git_hosts(rep, m):
    """[git_hosts."<host>"]: how `plugin repos --refresh` asks a host other than github.com about a repository."""
    import plugins_view  # here, not at the top: plugins_view imports the expect package this module also loads
    c = "git_hosts"
    table = m.get("git_hosts", {})
    if not isinstance(table, dict):
        rep.add(FAIL, c, "[git_hosts] 不是表", "按 [git_hosts.\"<主机>\"] 的格式登记")
        return
    for host, spec in table.items():
        errs = plugins_view.git_host_errors(host, spec)
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if not errs:
            rep.add(PASS, c, f'[git_hosts."{host}"]：用 {spec["command"][0]} 查仓库是否公开')


def check_plugins(rep, m):
    """[plugins.<name>] tables: visibility, sections, own and third-party skills, Agent Skills limits."""
    c = "plugins"
    table = m.get("plugins", {})
    if not isinstance(table, dict):
        rep.add(FAIL, c, "[plugins] 不是表", "按 [plugins.<名字>] 的格式登记插件")
        return
    if table and (not isinstance(m.get("plugin_owner"), str) or not m["plugin_owner"]):
        rep.add(FAIL, c, "有插件但顶层没有 plugin_owner",
                "在清单顶部写 plugin_owner = \"<GitHub 用户名>\"（插件和 marketplace 的作者名，不写邮箱）")
    for name, spec in table.items():
        errs = plugins.plugin_errors(m, name, spec)
        for msg, fix in errs:
            rep.add(FAIL, c, msg, fix)
        if not errs:
            vis = plugins.visibility(spec)
            note = "（没标可见性，按本机处理）" if "visibility" not in spec else ""
            rep.add(PASS, c, f"[plugins.{name}]：{plugins.label(vis)}{note}，章节 {len(spec.get('sections', []))} 个，"
                             f"自写 skill {len(spec.get('skills', []))} 个，第三方 skill {len(spec.get('skill_sources', []))} 个"
                             + (f"（复制进插件 {len(spec['vendor'])} 个）" if spec.get("vendor") else "")
                             + (f"，公司 skill {len(spec['company_skills'])} 个" if spec.get("company_skills") else ""))


def run_checks(manifest_path):
    rep = Report()
    try:
        m = load_manifest(manifest_path)
    except Exception as exc:  # unreadable file, bad TOML, bad encoding
        rep.add(FAIL, "load", f"清单读不了：{exc}", "确认路径存在且是合法的 UTF-8 TOML")
        return rep
    roles = m.get("roles") if _is_str_list(m.get("roles")) else []
    visibility = m.get("visibility") if _is_str_list(m.get("visibility")) else []
    state = {"rules_text": None}

    def rules():
        state["rules_text"] = check_rules(rep, m, roles, visibility)

    steps = [
        ("schema", lambda: check_schema(rep, m)),
        ("rule_sections", rules),
        ("docs", lambda: check_docs(rep, m, visibility)),
        ("hooks", lambda: check_hooks(rep, m, visibility)),
        ("environments", lambda: check_environments(rep, m)),
        ("plugins", lambda: check_plugins(rep, m)),
        ("git_hosts", lambda: check_git_hosts(rep, m)),
        ("targets", lambda: check_targets(rep, m, roles, visibility, state["rules_text"])),
        ("mcp", lambda: check_mcp(rep, m, visibility)),
        ("names", lambda: check_names(rep, m)),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # never crash: every error becomes a FAIL line
            rep.add(FAIL, name, f"检查出错：{type(exc).__name__}: {exc}",
                    f"按报错修正清单里 {name} 相关条目的类型或路径")
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验习惯清单")
    ap.add_argument("manifest")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = run_checks(args.manifest)
    if args.json:
        print(json.dumps({"results": rep.results, "ok": rep.ok}, ensure_ascii=False, indent=2))
    else:
        for r in rep.results:
            line = f"{r['level']} [{r['check']}] {r['message']}"
            if r["level"] != PASS:
                line += f"。修复：{r['fix']}"
            print(line)
        counts = {lv: sum(r["level"] == lv for r in rep.results) for lv in (PASS, WARN, FAIL)}
        print(f"合计：PASS {counts[PASS]}，WARN {counts[WARN]}，FAIL {counts[FAIL]}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
