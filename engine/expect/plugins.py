"""kind = "plugins": each plugin in the target's `plugins` list must be built from its current sources
(the built directory equals what `bin/agent-env plugin build` would write), and every section or skill a
later plugin carries again is reported as 已覆盖 (customized) with where it came from (for a target composed
on this machine, kind "composed" reports that instead). The built
directories live on this machine, so the check runs here for every environment type.
"""
import plugins as source  # the top-level engine/plugins.py
from cells import CUSTOMIZED, DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of

from .base import tilde

KIND = "plugins"


def validate(exp: dict, target: dict, label: str) -> list:
    if "plugins" not in target:
        return [(f"{label} 是 plugins，但落点没有 plugins 列表", "给落点写 plugins = [\"public\", ...]，或删掉这条期望")]
    return []


def _fix(name: str) -> str:
    return f"bin/agent-env plugin build --plugin {name}"


def plugin_cell(manifest: dict, name: str, rules_text, kind: str = "rules") -> dict:
    spec = source.table(manifest).get(name)
    vis = source.label(source.visibility(spec))
    row = f"插件 {name}（{vis}）"
    if not isinstance(spec, dict):
        return cell(kind, row, DRIFT, f"清单里没有插件「{name}」", f"在清单加 [plugins.{name}]，或从落点 plugins 删掉它")
    try:
        want = source.render_plugin(manifest, name, rules_text)
    except source.PluginError as exc:
        msg = str(exc)
        return cell(kind, row, DRIFT, msg.split("修复：", 1)[0].rstrip("。"),
                    msg.split("修复：", 1)[1] if "修复：" in msg else msg)
    except (OSError, UnicodeDecodeError) as exc:
        return cell(kind, row, UNKNOWN, f"读不了插件的源（{type(exc).__name__}）")
    dest = source.output_dir(manifest, name)
    have = source.tree_files(dest)
    if have is None:
        return cell(kind, row, MISSING, f"{tilde(dest)} 还没生成", _fix(name))
    if have == want:
        return cell(kind, row, SYNCED, f"{tilde(dest)} 与源一致（{len(want)} 个文件，哈希 {source.files_hash(want)[:12]}）")
    added, removed, changed = source.diff_counts(have, want)
    return cell(kind, row, DRIFT, f"{tilde(dest)} 比源过期：要新增 {added} 个、删除 {removed} 个、更新 {changed} 个文件",
                _fix(name))


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    kind = habit_of(exp)
    out = [plugin_cell(manifest, n, rules_text, kind) for n in source.target_plugins(target)]
    if "rules_output" in target:
        return out  # composed on this machine: kind "composed" reports what the merge took over, more precisely
    for what, item, earlier, later in source.overrides(manifest, target, rules_text):
        by = source.table(manifest).get(later)
        who = "补丁层" if source.visibility(by) == source.LOCAL else "插件"
        out.append(cell(kind, f"已覆盖：{what}「{item}」", CUSTOMIZED,
                        f"已覆盖：{what}「{item}」原来自插件 {earlier}，由{who} {later} 覆盖；上游再改不会自动跟进",
                        layer="env"))
    return out


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    return check_local(manifest, target, exp, rules_text)  # built on this machine, checked here
