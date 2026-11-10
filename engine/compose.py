"""Compose (合成): the rules file a target on one of the user's own machines reads, built on that machine
from the target's ordered plugins (docs/plugins-design.md, "Compose"). A remote host pulls the
plugins as published instead (engine/pull.py); a cloud-hosted one keeps a pointer rule (plugins.pointer_text).

Manifest contract:

  [sources]
  rules_src = "~/.claude/agents-md"     # where people edit the rules: one file per owner, joined in
                                        # [rule_sections] order (habits.join_rules_src)
  rules = "~/.claude/AGENTS.md"         # the full rules as one file; since stage 4 the rules_output below

  [[targets]]                           # in an environment that is neither remote-exec nor cloud-hosted
  plugins = ["my-public", "my-personal", "my-laptop"]
  rules_output = "~/.claude/AGENTS.md"  # `bin/agent-env render --target <id> --write` writes it; in an
                                        # environment with sync_command, its own sync does

How the plugins merge, in the target's order:
  - a section a plugin takes from the shared rules (`sections`) sits once, at its place in [rule_sections];
    a later plugin that takes it again leaves it as it is (or replaces it when its text differs);
  - a section of a plugin's own rules_file whose heading is already there is appended to that section: its
    lines go after the section's own, the heading line is not repeated ("later plugins append");
  - a rules_file section with a new heading goes after the registered sections, in plugin order;
  - `omit`: the half-sentences a plugin cuts when it is built only hold on the author's machines. A target
    with a patch layer (a local plugin) is on such a machine, so it keeps them; without one they are cut, as
    in the published plugin. Each kept span counts as the patch layer's.
Every carried-again, replaced, appended or kept part is reported, and doctor shows it as 已覆盖.
The file is one header line, a blank line, then the sections; the same inputs give the same bytes.
No user-specific path lives in this file. Requires Python 3.11+.
"""
from __future__ import annotations

import os
from pathlib import Path

import habits
import plugins
import render

KEY = "rules_output"


class ComposeError(Exception):
    """A failure whose message already says how to fix it (修复：...)."""


def output_path(target: dict) -> Path:
    return Path(os.path.expanduser(target[KEY]))


def is_composed(target: dict) -> bool:
    return isinstance(target, dict) and KEY in target


def sync_command(manifest: dict, env) -> str | None:
    """[environments.<env>] sync_command: the command whose run writes this environment's composed files
    itself (an assembler that owns the host directories and reads the rules source from a clone), else None."""
    v = plugins.env_spec(manifest, env).get("sync_command")
    return v.strip() if isinstance(v, str) and v.strip() and "\n" not in v else None


def regen_command(manifest: dict, target: dict) -> str:
    """How the file is written again: the environment's own sync when it has one, elsewhere `render --write`."""
    return sync_command(manifest, target.get("env")) or f"bin/agent-env render --target {target.get('id')} --write"


def target_errors(manifest: dict, target: dict, env_spec: dict) -> list:
    """(message, fix) pairs for a target's rules_output (called by plugins.target_errors)."""
    if KEY not in target:
        return []
    tid = target.get("id")
    v = target[KEY]
    errs = []
    if not isinstance(v, str) or not v.startswith("~/") or any(c in v for c in "\n\r\t'\"$`\\"):
        errs.append((f"落点「{tid}」的 rules_output 不是 ~/ 开头的普通路径", "写成 rules_output = \"~/.claude/AGENTS.md\""))
    if env_spec.get("transport") in (plugins.REMOTE_EXEC, plugins.CLOUD_HOSTED):
        errs.append((f"落点「{tid}」在远程或云端托管环境 {target.get('env')}，不能在本机合成规则文件",
                     "删掉 rules_output：远程主机用 pull 自己拉取，云端托管落点用指路规则"))
    if "pull" in target:
        errs.append((f"落点「{tid}」同时写了 rules_output 和 pull", "本机合成用 rules_output，远程拉取用 pull，二选一"))
    if errs:
        return errs
    for t in manifest.get("targets", []) or []:
        if (isinstance(t, dict) and t is not target and t.get("id") != tid and t.get("env") == target.get("env")
                and t.get(KEY) == v):
            errs.append((f"落点「{tid}」和「{t.get('id')}」都往 {v} 合成规则", "一个文件只由一个落点合成：删掉其中一个的 rules_output"))
    return errs


def _patch(manifest: dict, names: list):
    table = plugins.table(manifest)
    return next((n for n in names if isinstance(table.get(n), dict) and plugins.visibility(table[n]) == plugins.LOCAL),
                None)


def _append(existing: str, own: str) -> str:
    """`existing` section with the lines of `own` (a section with the same heading) after its own; the
    trailing blank lines `existing` had stay at the end."""
    body = own.split("\n", 1)[1] if "\n" in own else ""
    body = body.strip("\n")
    if not body:
        return existing
    core = existing.rstrip("\n")
    return core + "\n\n" + body + (existing[len(core):] or "\n")


def _kept(shared: list, omit: list) -> dict:
    """{heading: [1-based omit item]} of the `omit` entries a section holds (the spans a patch layer keeps)."""
    out = {}
    for i, frag in enumerate(omit, 1):
        for heading, text in shared:
            if frag and frag in text:
                out.setdefault(heading, []).append(i)
                break
    return out


def compose(manifest: dict, target: dict, rules_text) -> tuple:
    """(sections text, report) for the target's plugins merged by the rules above. Each report item is a dict:
    what (kept | appended | carried | replaced), section, plugin (where the part came from), by (the plugin
    that kept, appended, carried or replaced it), items (kept: the omit entries). Raises ComposeError."""
    tid = target.get("id")
    if rules_text is None:
        raise ComposeError("规则源读不了，无法合成。修复：bin/agent-env check 看规则源哪里不对")
    names = plugins.target_plugins(target)
    if not names:
        raise ComposeError(f"落点「{tid}」没有插件。修复：在清单该落点写 plugins = [\"...\"]")
    table = plugins.table(manifest)
    patch = _patch(manifest, names)
    slots, report = {}, []
    for n in names:
        spec = table.get(n)
        if not isinstance(spec, dict):
            raise ComposeError(f"落点「{tid}」的插件「{n}」不在清单里。修复：先加 [plugins.{n}]，或从 plugins 删掉它")
        try:
            shared = plugins.rules_parts(manifest, n, rules_text, apply_omit=not patch)
            own = plugins.own_sections(manifest, spec)
        except plugins.PluginError as exc:
            raise ComposeError(str(exc)) from exc
        omit = spec.get("omit") or []
        if patch and omit:
            for heading, items in _kept(shared, omit).items():
                report.append({"what": "kept", "section": heading, "plugin": n, "by": patch, "items": items})
        for heading, text in shared:
            if heading in slots:
                what = "carried" if slots[heading][0] == text else "replaced"
                report.append({"what": what, "section": heading, "plugin": slots[heading][1], "by": n})
                if what == "replaced":
                    slots[heading] = [text, n]
            else:
                slots[heading] = [text, n]
        for heading, text in own:
            if heading in slots:
                report.append({"what": "appended", "section": heading, "plugin": slots[heading][1], "by": n})
                slots[heading][0] = _append(slots[heading][0], text)
            else:
                slots[heading] = [text, n]
    registry = manifest.get("rule_sections") if isinstance(manifest.get("rule_sections"), dict) else {}
    order = [habits.PREAMBLE_KEY] if habits.PREAMBLE_KEY in slots else []
    order += [h for h in registry if h in slots and h != habits.PREAMBLE_KEY]
    registered = set(order)
    order += [h for h in slots if h not in registered]
    out = ""
    for heading in order:
        text = slots[heading][0]
        if not text.endswith("\n"):
            text += "\n"
        if heading not in registered and out and not out.endswith("\n\n"):
            out += "\n"  # a section of a plugin's own file starts after a blank line
        out += text
    return out, report


def header(manifest: dict, target: dict, src_display=None) -> str:
    """One HTML comment line: generated, where the rules are edited (each source file with the plugins of
    this target that take from it) and how the file is written again. No time, so it is stable."""
    names = plugins.target_plugins(target)
    table = plugins.table(manifest)
    _, where, _ = habits.join_rules_src(manifest)
    files, extra = {}, {}
    for n in names:
        spec = table.get(n) if isinstance(table.get(n), dict) else {}
        for h in spec.get("sections", []) or []:
            if h in where and n not in files.setdefault(where[h], []):
                files[where[h]].append(n)
        rf = spec.get("rules_file")
        if isinstance(rf, str):
            extra.setdefault(render._tilde(plugins._resolve(manifest, rf)), []).append(n)
    src = (manifest.get("sources") or {}).get(habits.RULES_SRC)
    shown = src_display or (render._tilde(src) if isinstance(src, str) else "")
    labels = [f"{f}（{'插件 ' if i == 0 else ''}{'、'.join(ns)}）" for i, (f, ns) in enumerate(files.items())]
    where_text = f"规则源：{shown}/ 的 " + "、".join(labels) if shown and labels else "规则源见清单 [sources] rules_src"
    if extra:
        where_text += "；另有 " + "、".join(f"{f}（{'、'.join(ns)}）" for f, ns in extra.items())
    sync = sync_command(manifest, target.get("env"))
    if sync:
        how = f"在写入方改好并推送，这里同步时（{sync}）重新生成"
    else:
        how = f"改完在 agent-env 仓库运行 {regen_command(manifest, target)}"
    return f"<!-- 生成文件，不要手改。{where_text}，{how}。 -->"


def render_text(manifest: dict, target: dict, rules_text, src_display=None) -> str:
    """The whole file: header, blank line, composed sections."""
    body, _ = compose(manifest, target, rules_text)
    return header(manifest, target, src_display) + "\n\n" + body


def target_for(manifest: dict, env: str, path: Path):
    """The target of environment `env` that composes `path`, or None."""
    return next((t for t in manifest.get("targets", []) or [] if is_composed(t) and t.get("env") == env
                 and "plugins" in t and isinstance(t.get(KEY), str) and output_path(t) == Path(path)), None)


def render_from(manifest: dict, target: dict, src_dir, src_display: str) -> str:
    """render_text with the rules source read from `src_dir` instead of [sources] rules_src. An environment's own
    assembler (sync_command) reads the rules source from its clone, before [sources] rules_src is linked to it."""
    m = dict(manifest, sources=dict(manifest.get("sources") or {}, **{habits.RULES_SRC: str(src_dir)}))
    text, _, errs = habits.join_rules_src(m)
    if errs:
        msg, fix = errs[0]
        raise ComposeError(f"{msg}。修复：{fix}")
    return render_text(m, target, text, src_display)


def section_name(heading: str) -> str:
    return "说明区" if heading == habits.PREAMBLE_KEY else heading
