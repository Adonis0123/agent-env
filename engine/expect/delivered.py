"""kind = "delivered": a cloud-hosted target (the user stores its rules in an app). The cloud copy cannot be
read, so the check compares the hash of what render produces now with the last recorded delivery
(bin/agent-env deliver --record). Not used in remote-exec environments: there `generated` compares hashes.
"""
import json

import plugins
import remote
import render
from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import tilde

KIND = "delivered"
NO_RULES = "规则源读不了，无法生成投递内容。修复：在 [sources] 里把 rules 指向存在的 UTF-8 文件"


def validate(exp: dict, target: dict, label: str) -> list:
    return []


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is not None:
        return [(f"落点「{target.get('id')}」在远程环境里，不用 delivered 期望",
                 "改成 kind = \"generated\"：doctor 会比对远程文件哈希")]
    return []


def text(manifest: dict, target_id: str, rules_text) -> str:
    """What the target should receive; render.RenderError (message says how to fix it) when it cannot be made."""
    target = render.find_target(manifest, target_id)
    if "plugins" in target:  # a plugin target receives only its pointer rule (指路规则)
        try:
            return plugins.pointer_text(manifest, target)
        except plugins.PluginError as exc:
            raise render.RenderError(str(exc))
    if rules_text is None:
        raise render.RenderError(NO_RULES)
    return render.render_rules(manifest, rules_text, target_id)


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    tid = target["id"]
    kind, name = habit_of(exp), name_of(exp, "云端规则")
    layer = exp.get("layer", "host")
    redeliver = (f"bin/agent-env deliver --target {tid} 输出要投递的内容，在该 app 里由你确认存入后，"
                 f"运行 bin/agent-env deliver --target {tid} --record")
    if "plugins" in target:
        redeliver = (f"bin/agent-env plugin pointer --target {tid} 输出指路规则，由你在该 app 里存进它的 memory 并确认后，"
                     f"运行 bin/agent-env plugin pointer --target {tid} --record")
    try:
        current = remote.sha256(text(manifest, tid, rules_text))
    except render.RenderError as exc:
        return [cell(kind, name, UNKNOWN, f"算不出当前内容：{exc}", layer=layer)]
    path = remote.delivery_path(tid)
    if not path.exists():
        return [cell(kind, name, MISSING, f"没有投递记录；当前内容哈希 {current[:12]}。云端内容本身读不到",
                     redeliver, layer)]
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        old, when = rec["sha256"], rec.get("delivered_at", "?")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [cell(kind, name, UNKNOWN, f"投递记录 {tilde(path)} 读不了（{type(exc).__name__}）", layer=layer)]
    if old == current:
        return [cell(kind, name, SYNCED, f"最后投递 {when}，内容哈希 {old[:12]}，与当前一致（云端内容本身读不到）",
                     layer=layer)]
    return [cell(kind, name, DRIFT, f"最后投递 {when}，内容哈希 {str(old)[:12]}；当前 {current[:12]}，内容变了要重新投递",
                 redeliver, layer)]


check_remote = None  # a remote-exec environment reports this kind as unsupported (habits_check rejects it there)
