"""kind = "composed": the target's rules_output must equal its plugins composed on this machine
(engine/compose.py), and every part the merge took from one plugin into another's section, or kept
because the target has a patch layer, is reported as 已覆盖 with where it came from.

The file lives on the machine the target is on, so the check runs there only (never over remote exec).
"""
import hashlib
import os
from pathlib import Path

import compose
import plugins
import render
from cells import CUSTOMIZED, DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import tilde

KIND = "composed"


def validate(exp: dict, target: dict, label: str) -> list:
    if "plugins" not in target or not isinstance(target.get(compose.KEY), str):
        return [(f"{label} 是 composed，但落点没有 plugins 和 rules_output",
                 "给落点写 plugins = [...] 和 rules_output = \"~/...\"，或删掉这条期望")]
    return []


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is not None:
        return [(f"落点「{target.get('id')}」在远程环境，composed 只查本机合成的文件",
                 "远程主机拉取插件用 kind = \"pulled\"")]
    return []


def _report_cells(kind: str, manifest: dict, report: list) -> list:
    """已覆盖 rows: one per section a merge step touched, in the order the steps happened; a section touched
    more than once (kept spans, then appended lines) says each in one detail."""
    table = plugins.table(manifest)
    rows = {}
    for r in report:
        section = compose.section_name(r["section"])
        by = r["by"]
        who = "补丁层" if plugins.visibility(table.get(by)) == plugins.LOCAL else "插件"
        name = f"已覆盖：章节「{section}」"
        if r["what"] == "kept":
            items = "、".join(str(i) for i in r["items"])
            detail = (f"插件 {r['plugin']} 发布时去掉的 {len(r['items'])} 处本机半句（omit 第 {items} 项），"
                      f"这里由{who} {by} 保留；源里改了这几句，这里随源更新")
        elif r["what"] == "appended":
            detail = f"{who} {by} 往插件 {r['plugin']} 的这一章追加了自己的内容；上游这一章再改，这里照常跟进"
        elif r["what"] == "replaced":
            detail = f"原来自插件 {r['plugin']}，由{who} {by} 的同名章节替换；上游再改不会自动跟进"
        else:
            detail = f"插件 {r['plugin']} 和{who} {by} 都带这一章，内容相同，只放一份"
        rows.setdefault(name, []).append(detail)
    return [cell(kind, name, CUSTOMIZED, "；".join(details), layer="env") for name, details in rows.items()]


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    kind = habit_of(exp)
    path = compose.output_path(target)
    name = name_of(exp, path.name)
    fix = compose.regen_command(manifest, target)
    if rules_text is None:
        return [cell(kind, name, UNKNOWN, "规则源读不了，无法合成期望内容", "bin/agent-env check")]
    try:
        text = compose.render_text(manifest, target, rules_text)
        _, report = compose.compose(manifest, target, rules_text)
    except compose.ComposeError as exc:
        msg = str(exc)
        return [cell(kind, name, DRIFT, msg.split("修复：", 1)[0].rstrip("。"),
                     msg.split("修复：", 1)[1] if "修复：" in msg else msg)]
    names = plugins.target_plugins(target)
    patch = next((n for n in names if plugins.visibility(plugins.table(manifest).get(n)) == plugins.LOCAL), None)
    try:
        if path.is_symlink():
            return [cell(kind, name, DRIFT, f"{tilde(path)} 是符号链接（指向 {os.readlink(path)}），合成的规则文件应是普通文件",
                         f"先确认链接指向的文件没有要保留的改动，再运行 {fix}")]
        if not path.exists():
            return [cell(kind, name, MISSING, f"{tilde(path)} 不存在", fix)]
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [cell(kind, name, UNKNOWN, f"读不了 {tilde(path)}（{type(exc).__name__}）")]
    order = " → ".join(names)
    if current != text:
        head = render.diff_summary(current, text, Path(path), limit=0).split("\n", 1)[0]
        return [cell(kind, name, DRIFT, f"{tilde(path)} 与插件 {order} 合成的结果不一致：{head.rstrip('：')}",
                     f"手改的内容先搬进规则源 {render._tilde((manifest.get('sources') or {}).get('rules_src', ''))}，"
                     f"再运行 {fix}")]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    if patch:
        main = cell(kind, name, CUSTOMIZED, f"{tilde(path)} 与插件 {order} 合成的结果一致（含补丁层 {patch}），哈希 {digest}",
                    layer="env")
    else:
        main = cell(kind, name, SYNCED, f"{tilde(path)} 与插件 {order} 合成的结果一致，哈希 {digest}")
    return [main] + _report_cells(kind, manifest, report)


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    return None  # the composed file is on the user's own machine; env_errors refuses a remote one
