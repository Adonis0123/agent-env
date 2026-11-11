"""kind = "pulled": a remote-exec target that pulls its plugins itself (engine/pull.py). One remote round trip
reads the joined rules file, the puller's last status, its timer and the installed kit; the cells say whether
the rules the agent reads equal the plugins built here, and whether the puller keeps them current.

Cells (habit rules):
  <name, default the output's file name>  the joined rules vs `pull.joined_rules` (synced, or drift with why:
                                          not pulled yet, or the sources changed after the last publish)
  定时拉取                                 timer active, last run ok and recent, kit and pull.conf current

Whether the plugins were published is read from this machine's working clones of the plugin repositories.
A machine that does not publish (the manifest's `publish_from` names another one, plugins.published_away)
keeps no such clone: there the rules cell only says that what was pulled differs from the sources, and that
publishing is done on that machine; it never offers `plugin publish` here.
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import plugins as source
import pull
import remote
from cells import DRIFT, MISSING, SYNCED, UNKNOWN, cell, habit_of, name_of

KIND = "pulled"
TIMER_ROW = "定时拉取"


def validate(exp: dict, target: dict, label: str) -> list:
    if "pull" not in target:
        return [(f"{label} 是 pulled，但落点没有 pull 表", "给落点写 pull = { dir = \"~/...\", output = \"~/...\" }，或删掉这条期望")]
    return []


def env_errors(target: dict, remote_spec) -> list:
    if remote_spec is None:
        return [(f"落点「{target.get('id')}」有 pulled 期望，但环境 {target.get('env')!r} 不是 remote-exec",
                 "pulled 只在远程主机自己拉取时核对：删掉这条期望，或把环境 transport 写成 \"remote-exec\"")]
    return []


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    return [cell(habit_of(exp), name_of(exp, TIMER_ROW), UNKNOWN, "pulled 期望只在 remote-exec 环境核对")]


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    p = pull.spec_of(target)
    if p is None:
        raise remote.RemoteError("落点没有 pull 表。修复：bin/agent-env check")
    q = remote.shell_path
    u = spec.get("remote_user", "")
    files = [dest for _, dest, _ in pull.KIT] + [pull.CONF]
    frag = (f"p={q(spec, p['output'])}\n"
            'if [ -L "$p" ]; then echo state=symlink\n'
            'elif [ -f "$p" ]; then echo state=file; echo sha=$(_ae_sha "$p")\n'
            "else echo state=absent; fi\n"
            f"sed 's/^/status./' {q(spec, pull.STATUS)} 2>/dev/null\n"
            + "".join(f"f={q(spec, d)}; [ -f \"$f\" ] && echo \"kit.{i}=$(_ae_sha \"$f\")\"\n" for i, d in enumerate(files))
            + f"echo \"timer=$(runuser -u {u} -- env XDG_RUNTIME_DIR=/run/user/$(id -u {u}) "
            f"systemctl --user is-active {pull.UNIT}.timer 2>&1 | head -n 1)\"\n")
    return frag, lambda lines: _parse(manifest, spec, target, exp, remote._kv(lines), rules_text)


def _deliver_fix(tid: str) -> str:
    return f"bin/agent-env deliver --target {tid} --dry-run 看步骤，用户确认后加 --yes（装好拉取并立刻拉一次）"


def _published_rev(name: str) -> str:
    """HEAD of this machine's working clone of the plugin repository ('' when there is none)."""
    clone = source.clone_dir(name)
    if not (clone / ".git").exists():
        return ""
    try:
        res = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _age_minutes(stamp: str):
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def _rules_cell(manifest, spec, target, exp, kv, rules_text) -> dict:
    tid, kind = target["id"], habit_of(exp)
    out = pull.spec_of(target)["output"]
    name = name_of(exp, Path(out).name)
    where = f"远程 {out}"
    if rules_text is None:
        return cell(kind, name, UNKNOWN, "规则源读不了，算不出期望内容")
    try:
        want = remote.sha256(pull.joined_rules(manifest, target, rules_text))
    except source.PluginError as exc:
        msg = str(exc)
        return cell(kind, name, DRIFT, msg.split("修复：", 1)[0].rstrip("。"), msg.split("修复：", 1)[-1])
    state, got = kv.get("state"), kv.get("sha", "")
    plist = "、".join(pull.names(manifest, target))
    if state == "absent":
        return cell(kind, name, MISSING, f"{where} 不存在", _deliver_fix(tid))
    if state == "symlink":
        return cell(kind, name, DRIFT, f"{where} 是符号链接，拉取不会覆盖它", "先在远程确认链接指向，再决定是否改成普通文件")
    if state != "file" or len(got) != 64:
        return cell(kind, name, UNKNOWN, f"读不到 {where} 的哈希（远程输出不完整）")
    if got == want:
        return cell(kind, name, SYNCED, f"{where} 与插件 {plist} 按顺序拼接的结果一致，哈希 {got[:12]}")
    if kv.get("status.result") is None:
        return cell(kind, name, DRIFT, f"{where} 还是本机投递的旧规则（{got[:12]}），远程还没拉取过插件", _deliver_fix(tid))
    away = source.published_away(manifest, target.get("env"))
    if away:  # this machine keeps no clone of the plugin repositories: it cannot tell pushed from not pushed
        return cell(kind, name, DRIFT, f"{where}（{got[:12]}）与规则源现在生成的内容不一致（{want[:12]}）",
                    f"到 {away['name']} 上让 AI 发布（发布只在那台机器上做）；远程下次拉取时更新")
    behind = [n for n in pull.names(manifest, target)
              if _published_rev(n) and kv.get(f"status.plugin.{n}") != _published_rev(n)]
    if behind:
        return cell(kind, name, DRIFT, f"{where}（{got[:12]}）还没拉到插件 {'、'.join(behind)} 的最新提交，本机期望 {want[:12]}",
                    f"等下一次定时拉取（每 30 分钟），或 {_deliver_fix(tid)}")
    return cell(kind, name, DRIFT, f"{where}（{got[:12]}）与本机现在生成的插件不一致（{want[:12]}）：源改了还没发布",
                "bin/agent-env plugin publish --all 演练看要推送什么，用户确认后加 --yes；远程下次拉取时更新")


def _timer_cell(manifest, spec, target, exp, kv) -> dict:
    tid, kind = target["id"], habit_of(exp)
    timer, result = kv.get("timer", "").strip(), kv.get("status.result")
    if result is None and timer != "active":
        return cell(kind, TIMER_ROW, MISSING, "远程还没装定时拉取（没有拉取状态，定时器没在运行）", _deliver_fix(tid))
    try:
        files = pull.kit_files(manifest, spec, target)
    except (OSError, KeyError) as exc:
        return cell(kind, TIMER_ROW, UNKNOWN, f"算不出本机的拉取脚本（{type(exc).__name__}）")
    stale_kit = [Path(dest).name for i, (data, dest, _) in enumerate(files) if kv.get(f"kit.{i}") != pull.sha(data)]
    if timer != "active":
        return cell(kind, TIMER_ROW, DRIFT, f"定时器 {pull.UNIT}.timer 没在运行（{timer or '无输出'}）", _deliver_fix(tid))
    if stale_kit:
        return cell(kind, TIMER_ROW, DRIFT, f"远程的 {'、'.join(stale_kit)} 和本机不一致（插件列表、仓库或脚本改过）",
                    _deliver_fix(tid))
    when, age = kv.get("status.time", ""), _age_minutes(kv.get("status.time", ""))
    if result != "ok":
        err = kv.get("status.error") or "没写原因"
        return cell(kind, TIMER_ROW, DRIFT, f"最后一次拉取 {when} 结果是 {result}：{err}",
                    f"远程看 journalctl --user -u {pull.UNIT}；凭证或网络好了以后等下一次定时拉取，或 {_deliver_fix(tid)}")
    if age is None or age > pull.STALE_MINUTES:
        return cell(kind, TIMER_ROW, DRIFT, f"最后一次成功拉取是 {when or '时间不明'}，超过 {pull.STALE_MINUTES} 分钟没拉了",
                    f"远程看 systemctl --user list-timers 和 journalctl --user -u {pull.UNIT}")
    return cell(kind, TIMER_ROW, SYNCED, f"定时器在运行，最后一次拉取 {when}（{int(age)} 分钟前）成功", layer="env")


def _parse(manifest, spec, target, exp, kv, rules_text) -> list:
    return [_rules_cell(manifest, spec, target, exp, kv, rules_text), _timer_cell(manifest, spec, target, exp, kv)]
