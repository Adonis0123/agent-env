"""kind = "generated": the target's output file(s) must equal what render produces for it.

Locally that is `render --check` as cells (plus skills.disabled when the target declares it); in a
remote-exec environment the remote file's hash is compared with the one rendered here.
"""
import json
from pathlib import Path

import remote
import render
from cells import CUSTOMIZED, DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

from .base import tilde

KIND = "generated"


def validate(exp: dict, target: dict, label: str) -> list:
    if target.get("render") != "generated":
        return [(f"{label} 是 generated，但落点没有 render = \"generated\"",
                 "给落点写 render = \"generated\" 和 output，或换成 link / contains")]
    return []


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is not None and not isinstance(remote_spec.get("remote_home"), str):
        return [(f"落点「{target.get('id')}」要投递生成文件，但环境没有 remote_home",
                 "在环境表写 remote_home = \"/home/<用户>\"（远程 ~/ 的位置）")]
    return []


# ── this machine ──────────────────────────────────────────────────────────

def _render_fix(target_id: str) -> str:
    return f"bin/agent-env render --target {target_id} --write"


def _fix_of(message: str) -> str:
    return message.split("修复：", 1)[1] if "修复：" in message else message


def _file_cell(kind, name, path, text, target, ok_state, ok_layer) -> dict:
    """One generated file compared with the rendered text."""
    tid = target["id"]
    try:
        if path.is_symlink():
            return cell(kind, name, DRIFT, f"{tilde(path)} 是符号链接，生成文件应是普通文件",
                        "先确认链接指向，再把清单 output / also_output 改成链接目标的真实路径")
        if not path.exists():
            return cell(kind, name, MISSING, f"{tilde(path)} 不存在", _render_fix(tid))
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return cell(kind, name, UNKNOWN, f"读不了生成文件 {tilde(path)}（{type(exc).__name__}）")
    if current == text:
        detail = f"{tilde(path)} 与生成结果一致"
        if ok_state == CUSTOMIZED:
            detail += "（含本落点档案）"
        return cell(kind, name, ok_state, detail, layer=ok_layer)
    head = render.diff_summary(current, text, path, limit=0).split("\n", 1)[0]
    return cell(kind, name, DRIFT, f"{tilde(path)} 与生成结果不一致：{head.rstrip('：')}",
                f"{_render_fix(tid)}（专属内容的改动先搬进档案 "
                f"{tilde(target.get('supplement', ''))}）" if target.get("supplement")
                else _render_fix(tid))


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    """`render --check` as habit cells: the generated file, plus skills.disabled when declared."""
    tid = target["id"]
    kind = habit_of(exp)
    default_name = Path(str(target.get("output", "output"))).name
    name = name_of(exp, default_name)
    ok_state, ok_layer = (CUSTOMIZED, "env") if target.get("supplement") else (SYNCED, exp.get("layer", "host"))
    cells = []
    if rules_text is None:
        cells.append(cell(kind, name, UNKNOWN, "规则源读不了，无法生成期望内容"))
    else:
        try:
            text = render.render_rules(manifest, rules_text, tid)
            paths = render.output_paths(target)
            for i, path in enumerate(paths):
                # Extra outputs get their own cell; named by file, or by full path if the name repeats.
                pname = name if i == 0 else (path.name if path.name != name else tilde(path))
                cells.append(_file_cell(kind, pname, path, text, target, ok_state, ok_layer))
        except render.RenderError as exc:
            cells.append(cell(kind, name, DRIFT, str(exc).split("修复：", 1)[0].rstrip("。"),
                              _fix_of(str(exc))))
    level, msg = render.check_skills_disabled(target)
    if level:
        state = {"PASS": SYNCED, "FAIL": DRIFT}.get(level, UNKNOWN)
        detail = msg.split("修复：", 1)[0].rstrip("。")
        cells.append(cell("skills", "skills.disabled", state, detail,
                          _fix_of(msg) if state == DRIFT else "", "env"))
    return cells


# ── remote-exec host ──────────────────────────────────────────────────────

def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    p = remote.shell_path(spec, str(target.get("output", "")))
    frag = (f"p={p}\n"
            'if [ -L "$p" ]; then echo state=symlink\n'
            'elif [ -f "$p" ]; then echo state=file; echo sha=$(_ae_sha "$p"); echo bytes=$(wc -c < "$p" | tr -d " ")\n'
            'elif [ -e "$p" ]; then echo state=other\n'
            "else echo state=absent; fi\n")
    return frag, lambda lines: _parse(manifest, target, exp, remote._kv(lines), rules_text)


def _last_delivery(tid: str) -> str:
    try:
        rec = json.loads(remote.delivery_path(tid).read_text(encoding="utf-8"))
        return f"；最后投递 {rec.get('delivered_at', '?')}（{str(rec.get('sha256', ''))[:12]}）"
    except (OSError, ValueError, AttributeError):
        return "；本机没有投递记录"


def _deliver_fix(target: dict) -> str:
    tid = target["id"]
    fix = f"bin/agent-env deliver --target {tid} --dry-run 看步骤，用户确认后加 --yes 投递"
    if target.get("supplement"):
        fix += f"（远程文件里的专属改动先搬进档案 {tilde(target['supplement'])}）"
    return fix


def _parse(manifest: dict, target: dict, exp: dict, kv: dict, rules_text) -> dict:
    tid, kind = target["id"], habit_of(exp)
    out = str(target.get("output", ""))
    name = name_of(exp, Path(out).name or "output")
    where = f"远程 {out}"
    if rules_text is None:
        return cell(kind, name, UNKNOWN, "规则源读不了，无法生成期望内容")
    try:
        want = remote.sha256(render.render_rules(manifest, rules_text, tid))
    except render.RenderError as exc:
        msg = str(exc)
        return cell(kind, name, DRIFT, msg.split("修复：", 1)[0].rstrip("。"),
                    msg.split("修复：", 1)[1] if "修复：" in msg else msg)
    state, last = kv.get("state"), _last_delivery(tid)
    if state == "absent":
        return cell(kind, name, MISSING, f"{where} 不存在{last}", _deliver_fix(target))
    if state == "symlink":
        return cell(kind, name, DRIFT, f"{where} 是符号链接，生成文件应是普通文件",
                    "先在远程确认链接指向，再把清单 output 改成链接目标的真实路径")
    if state == "other":
        return cell(kind, name, DRIFT, f"{where} 不是普通文件", "先在远程确认它是什么，再决定是否投递")
    got = kv.get("sha", "")
    if state != "file" or len(got) != 64:
        return cell(kind, name, UNKNOWN, f"读不到 {where} 的哈希（远程输出不完整）")
    if got == want:
        ok, layer = (CUSTOMIZED, "env") if target.get("supplement") else (SYNCED, exp.get("layer", "host"))
        extra = "（含本落点档案）" if target.get("supplement") else ""
        return cell(kind, name, ok, f"{where} 与本机生成结果一致{extra}，哈希 {got[:12]}{last}", layer=layer)
    return cell(kind, name, DRIFT, f"{where} 与本机生成结果不一致：远程 {got[:12]}，本机 {want[:12]}{last}",
                _deliver_fix(target))
