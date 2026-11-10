"""Pull (拉取): a remote-exec target whose host pulls its plugins itself (docs/plugins-design.md, "Pull").

The host runs `agent-env-pull` (features/plugin-pull/) from a systemd user timer: it git-pulls each plugin of
the target into <dir>/<name> and joins their AGENTS.md, in the target's order, into `output` (the file the
agent reads). This machine only builds and publishes the plugins, installs the puller (`bin/agent-env
deliver --target <id>`), and checks the result (`kind = "pulled"`, engine/expect/pulled.py).

Manifest contract (on a [[targets]] of a remote-exec environment that has `plugins`):

  pull = { dir = "~/.local/share/agent-env/plugins",   # one checkout per plugin: <dir>/<name>
           output = "~/.hermes/workspace/AGENTS.md",    # the joined rules
           env_file = "~/.hermes/.env",                 # optional: git credentials read from here ...
           env_keys = ["GIT_TOKEN"],                    # ... only these variables, never printed
           skills_config = "~/.hermes/config.yaml" }    # optional: Hermes config whose skills.external_dirs
                                                        # gets the checkouts' skills/ (set once, backed up)

The joined file is byte-for-byte what `joined_rules` computes here: the header line, a blank line, then each
plugin's AGENTS.md, separated by one blank line. The puller replaces it only when it changes, so a run that
finds nothing new leaves the file (and its mtime) alone.

Delivery keeps the old rules working until the new path is proven: it installs the kit, runs the puller once
into a preview file, and enables the timer (which then writes the real file) only when the preview equals
what this machine expects. Every file the delivery replaces is copied first to
~/.local/state/agent-env/backups/<UTC stamp>/ on the host, with `paths.txt` naming where each came from.
No user-specific path lives in this file. Requires Python 3.11+.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import plugins
import remote
import render

KIT_DIR = Path(__file__).resolve().parent.parent / "features" / "plugin-pull"
UNIT = "agent-env-pull"
# (kit file, remote path, mode): installed as the remote user
KIT = (("agent-env-pull", "~/.local/bin/agent-env-pull", "755"),
       ("agent-env-pull.service", f"~/.config/systemd/user/{UNIT}.service", "644"),
       ("agent-env-pull.timer", f"~/.config/systemd/user/{UNIT}.timer", "644"))
CONF = "~/.config/agent-env/pull.conf"
STATE = "~/.local/state/agent-env"
STATUS = f"{STATE}/pull.status"
PREVIEW = f"{STATE}/AGENTS.preview.md"
PREVIEW_STATUS = f"{STATE}/pull.preview.status"
BACKUPS = f"{STATE}/backups"
FIELDS = ("dir", "output", "env_file", "env_keys", "skills_config")
STALE_MINUTES = 90  # the timer runs every 30 minutes; three misses in a row is a problem


class PullError(Exception):
    """A failure whose message already says how to fix it (修复：...)."""


# ── manifest ──────────────────────────────────────────────────────────────

def spec_of(target: dict):
    p = target.get("pull")
    return p if isinstance(p, dict) else None


def target_errors(manifest: dict, target: dict, env_spec: dict) -> list:
    """(message, fix) pairs for a target's `pull` table and its environment (called by plugins.target_errors)."""
    tid = target.get("id")
    remote_exec = env_spec.get("transport") == plugins.REMOTE_EXEC
    if "pull" not in target:
        if remote_exec and "plugins" in target:
            return [(f"落点「{tid}」在远程环境里挂了插件，却没写 pull，没人去拉",
                     "写 pull = { dir = \"~/.local/share/agent-env/plugins\", output = \"~/<agent 读的规则文件>\" }")]
        return []
    if not remote_exec:
        return [(f"落点「{tid}」写了 pull，但环境 {target.get('env')} 不是 remote-exec",
                 "pull 只用于远程主机自己拉取插件：删掉 pull，或把环境的 transport 写成 \"remote-exec\"")]
    p = target["pull"]
    if not isinstance(p, dict):
        return [(f"落点「{tid}」的 pull 不是表", "写成 pull = { dir = \"~/...\", output = \"~/...\" }")]
    errs = []
    extra = sorted(str(k) for k in set(p) - set(FIELDS))
    if extra:
        errs.append((f"落点「{tid}」的 pull 有未知字段 {extra}", f"只用 {list(FIELDS)}；密钥值不写进清单"))
    for key in ("dir", "output", "env_file", "skills_config"):
        v = p.get(key)
        if key in ("dir", "output") or key in p:
            if not isinstance(v, str) or not v.startswith("~/") or any(c in v for c in "\n\r\t '\"$`\\"):
                errs.append((f"落点「{tid}」的 pull.{key} 不是 ~/ 开头的普通路径",
                             f"写成 {key} = \"~/...\"（远程 ~ 是环境的 remote_home；不带空格、引号、变量）"))
    keys = p.get("env_keys", [])
    if "env_keys" in p and (not plugins._str_list(keys) or not all(plugins.ENV_VAR_RE.match(k) for k in keys)):
        errs.append((f"落点「{tid}」的 pull.env_keys 不是变量名列表", "写成 env_keys = [\"GIT_TOKEN\"]，只写名字"))
    if ("env_keys" in p) != ("env_file" in p):
        errs.append((f"落点「{tid}」的 pull.env_file 和 env_keys 要一起写", "两个都写，或都删掉"))
    if not isinstance(env_spec.get("remote_home"), str) or not isinstance(env_spec.get("remote_user"), str):
        errs.append((f"落点「{tid}」要在远程拉取，但环境没有 remote_home 或 remote_user",
                     "在环境表写 remote_home = \"/home/<用户>\" 和 remote_user = \"<用户>\"（拉取以这个用户跑）"))
    table = plugins.table(manifest)
    for n in plugins.target_plugins(target):
        if isinstance(table.get(n), dict) and not table[n].get("repo"):
            errs.append((f"落点「{tid}」要拉的插件「{n}」还没有仓库", f"建好仓库后在 [plugins.{n}] 写 repo"))
    return errs


def names(manifest: dict, target: dict) -> list:
    return plugins.pointer_plugins(manifest, target)


def header(manifest: dict, target: dict) -> str:
    return (f"<!-- 本文件由 agent-env-pull 定时拉取插件 {'、'.join(names(manifest, target))} 后按顺序拼成，"
            f"后面的覆盖前面的。不要手改：改规则在 agent-env 改源、发布插件，这里下次拉取时更新。 -->")


def joined_rules(manifest: dict, target: dict, rules_text) -> str:
    """The joined rules exactly as agent-env-pull writes them from the current plugin builds."""
    parts = []
    for n in names(manifest, target):
        data = plugins.render_plugin(manifest, n, rules_text)["AGENTS.md"][0]
        parts.append(data.decode("utf-8"))
    return header(manifest, target) + "\n\n" + "\n".join(parts)


def skill_roots(manifest: dict, env_spec: dict, target: dict) -> list:
    """Remote skills/ directories of the target's plugins that carry skills, in plugin order."""
    p = spec_of(target) or {}
    out = []
    for n in names(manifest, target):
        spec = plugins.table(manifest).get(n, {})
        if spec.get("skills") or spec.get("company_skills"):
            out.append(remote.remote_path(env_spec, p["dir"].rstrip("/") + f"/{n}/skills"))
    return out


def conf_text(manifest: dict, env_spec: dict, target: dict) -> str:
    p = spec_of(target) or {}
    lines = ["# agent-env-pull 配置：由 agent-env 生成（bin/agent-env deliver），不要手改",
             f"output={remote.remote_path(env_spec, p['output'])}",
             f"dir={remote.remote_path(env_spec, p['dir'])}",
             f"header={header(manifest, target)}"]
    if p.get("env_file"):
        lines += [f"env_file={remote.remote_path(env_spec, p['env_file'])}", f"env_keys={' '.join(p['env_keys'])}"]
    table = plugins.table(manifest)
    lines += [f"plugin={n} {table[n]['repo']}" for n in names(manifest, target)]
    return "\n".join(lines) + "\n"


def kit_files(manifest: dict, env_spec: dict, target: dict) -> list:
    """[(data, remote path, mode)] the delivery installs."""
    out = []
    for name, dest, mode in KIT:
        out.append(((KIT_DIR / name).read_bytes(), remote.remote_path(env_spec, dest), mode))
    out.append((conf_text(manifest, env_spec, target).encode("utf-8"), remote.remote_path(env_spec, CONF), "644"))
    return out


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── deliver: install the puller, prove it on a preview, then switch on the timer ──

def _user_cmd(env_spec: dict) -> str:
    u = shlex.quote(env_spec["remote_user"])
    return (f"U={u}; R=/run/user/$(id -u \"$U\")\n"
            "uctl() { runuser -u \"$U\" -- env XDG_RUNTIME_DIR=\"$R\" systemctl --user \"$@\"; }\n")


def _run_as_user(env_spec: dict, extra_env: str = "") -> str:
    home = shlex.quote(env_spec["remote_home"])
    return (f"runuser -u \"$U\" -- env HOME={home} XDG_RUNTIME_DIR=\"$R\" "
            f"PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin {extra_env}"
            f"{home}/.local/bin/agent-env-pull")


def plan(manifest: dict, env_spec: dict, target: dict, rules_text, stamp: str) -> dict:
    files = kit_files(manifest, env_spec, target)
    want = joined_rules(manifest, target, rules_text).encode("utf-8")
    home = env_spec["remote_home"].rstrip("/")
    q = shlex.quote
    staging = f"{home}/.cache/agent-env/incoming/{stamp}"
    backup = remote.remote_path(env_spec, f"{BACKUPS}/{stamp}")
    p = spec_of(target)
    output = remote.remote_path(env_spec, p["output"])
    preview = remote.remote_path(env_spec, PREVIEW)
    owner = q(env_spec["remote_user"])
    snap = [_user_cmd(env_spec), f"mkdir -p {q(staging)} || exit 4", f"chown -R {owner}: {q(home + '/.cache/agent-env')}",
            f"b={q(backup)}", "runuser -u \"$U\" -- mkdir -p \"$b\" && chmod 700 \"$b\" || exit 4", ": > \"$b/paths.txt\"",
            "save() {  # <path>: copy it into the backup folder before it is replaced",
            '  if [ -L "$1" ]; then echo "ERR $1 是符号链接，不覆盖"; exit 3; fi',
            '  if [ -f "$1" ]; then cp -p "$1" "$b/$(basename "$1")" && printf "%s\\t%s\\n" "$(basename "$1")" "$1" >> "$b/paths.txt" || exit 4; fi',
            "}"]
    snap += [f"save {q(dest)}" for _, dest, _ in files]
    snap += [f"save {q(output)}", f"chown -R {owner}: \"$b\"", "echo backup=$b", "cat \"$b/paths.txt\" | sed 's/^/saved /'"]
    install = [_user_cmd(env_spec), f"s={q(staging)}"]
    for i, (data, dest, mode) in enumerate(files):
        folder, tmp = str(Path(dest).parent), f"{Path(dest).parent}/.{Path(dest).name}.agent-env.tmp"
        install += [f"got=$(_ae_sha \"$s/f{i}\")",
                    f"[ \"$got\" = {q(sha(data))} ] || {{ echo \"sha_mismatch f{i}=$got\"; exit 5; }}",
                    f"runuser -u \"$U\" -- mkdir -p {q(folder)} || exit 6",
                    f"cp \"$s/f{i}\" {q(tmp)} && chmod {mode} {q(tmp)} && chown {owner}: {q(tmp)} && mv -f {q(tmp)} {q(dest)} || exit 6",
                    f"echo installed={q(dest)}"]
    install += ['rm -rf -- "$s"', "uctl daemon-reload || exit 6",
                f"rm -f {q(preview)}",
                _run_as_user(env_spec, f"AGENT_ENV_PULL_OUTPUT={q(preview)} "
                                       f"AGENT_ENV_PULL_STATUS={q(remote.remote_path(env_spec, PREVIEW_STATUS))} "),
                'echo "preview_rc=$?"',
                f"[ -f {q(preview)} ] && echo \"preview_sha=$(_ae_sha {q(preview)})\""]
    activate = [_user_cmd(env_spec)]
    roots = skill_roots(manifest, env_spec, target)
    if p.get("skills_config") and roots:
        activate.append(skills_config_script(remote.remote_path(env_spec, p["skills_config"]), roots, backup))
    status = remote.remote_path(env_spec, STATUS)
    activate += [f"uctl enable --now {UNIT}.timer || exit 8",
                 f"uctl start {UNIT}.service; echo \"start_rc=$?\"",
                 f"echo \"timer=$(uctl is-active {UNIT}.timer)\"",
                 f"sed 's/^/status./' {q(status)} 2>/dev/null",
                 f"[ -f {q(output)} ] && echo \"output_sha=$(_ae_sha {q(output)})\"",
                 f"rm -f {q(preview)}"]
    return {"files": files, "staging": staging, "backup": backup, "output": output, "preview": preview,
            "want": sha(want), "bytes": len(want), "roots": roots,
            "snapshot": "\n".join(snap) + "\n", "install": "\n".join(install) + "\n", "activate": "\n".join(activate) + "\n"}


def skills_config_script(cfg: str, roots: list, backup: str) -> str:
    """sh that puts `roots` into a Hermes config.yaml's skills.external_dirs. It only rewrites the exact default
    `skills:` / `  external_dirs: []` pair (backing the file up first); any other value is left alone and
    reported (exit 7), because merging someone's own list by text is a guess."""
    q = shlex.quote
    present = " && ".join(f"grep -qxF {q('  - ' + r)} \"$cfg\"" for r in roots)
    items = "".join(f"print \"  - {r}\"; " for r in roots)
    awk = ('BEGIN{p=""} { if ($0=="  external_dirs: []" && p=="skills:") { print "  external_dirs:"; '
           + items + "} else print; p=$0 }")
    return (f"cfg={q(cfg)}\n"
            'if [ -L "$cfg" ] || [ ! -f "$cfg" ]; then echo "skills_config=absent"; exit 7; fi\n'
            f"if {present}; then echo skills_config=already\n"
            "elif [ \"$(grep -c '^skills:$' \"$cfg\")\" = 1 ] && [ \"$(grep -c '^  external_dirs: \\[\\]$' \"$cfg\")\" = 1 ] "
            "&& grep -A1 '^skills:$' \"$cfg\" | grep -qx '  external_dirs: \\[\\]'; then\n"
            f"  b={q(backup)}; cp -p \"$cfg\" \"$b/$(basename \"$cfg\")\" && "
            "printf '%s\\t%s\\n' \"$(basename \"$cfg\")\" \"$cfg\" >> \"$b/paths.txt\" || exit 7\n"
            '  chown --reference="$b" "$b/paths.txt" 2>/dev/null\n'
            '  t="$cfg.agent-env.tmp"; cp -p "$cfg" "$t" || exit 7\n'
            f"  awk {q(awk)} \"$cfg\" > \"$t\" && mv -f \"$t\" \"$cfg\" || {{ rm -f \"$t\"; exit 7; }}\n"
            "  echo skills_config=set\n"
            "else echo skills_config=unexpected; exit 7; fi\n")


def format_plan(manifest: dict, env_spec: dict, target: dict, pl: dict) -> str:
    send = " ".join(shlex.quote(x) for x in remote.resolve_cmd(manifest, env_spec.get("send")))
    tid = target["id"]
    lines = [f"让 {tid} 自己拉取插件 {'、'.join(names(manifest, target))}：装拉取脚本和 systemd 用户定时器，"
             f"先试拉到预览文件，和本机期望（{pl['bytes']} B，sha256 {pl['want'][:12]}）一致才打开定时器", "",
             f"1. 远程备份要替换的文件到 {pl['backup']}/（paths.txt 记原位置），建暂存目录：",
             *["     " + x for x in pl["snapshot"].rstrip().splitlines()],
             f"2. 发送 {len(pl['files'])} 个文件到 {pl['staging']}/：",
             *[f"     {send} <本机临时文件 f{i}> {pl['staging']} f{i}   # → {dest}（{mode}，{len(d)} B）"
               for i, (d, dest, mode) in enumerate(pl["files"])],
             f"3. 校验后装好、daemon-reload，以远程用户试拉一次到预览文件 {pl['preview']}（{pl['output']} 不动）：",
             *["     " + x for x in pl["install"].rstrip().splitlines()],
             f"4. 本机核对预览哈希等于 {pl['want'][:12]}；不一致就停在这里，定时器不打开，规则文件不动",
             "5. 打开定时器并立刻跑一次（写真正的规则文件，旧文件先备份）"
             + ("，把插件的 skills/ 加进 skills.external_dirs" if pl["roots"] else "") + "：",
             *["     " + x for x in pl["activate"].rstrip().splitlines()],
             f"6. 本机核对规则文件哈希，写投递记录 {render._tilde(remote.delivery_path(tid))}"]
    return "\n".join(lines) + "\n"


def rollback_hint(env_spec: dict, pl: dict) -> str:
    u = env_spec["remote_user"]
    return (f"回滚（远程以 root）：runuser -u {u} -- env XDG_RUNTIME_DIR=/run/user/$(id -u {u}) systemctl --user "
            f"disable --now {UNIT}.timer；再按 {pl['backup']}/paths.txt 把每个备份 cp -p 回原位置")


def deliver(manifest: dict, env_spec: dict, target: dict, rules_text, yes: bool, out, err) -> int:
    tid, env = target["id"], target.get("env")
    errs = target_errors(manifest, target, env_spec)
    if errs:
        raise PullError(f"{errs[0][0]}。修复：{errs[0][1]}")
    if not isinstance(env_spec.get("send"), list):
        raise PullError(f"环境 {env} 没有 send 命令前缀。修复：在清单环境表里写 send = [...]")
    try:
        pl = plan(manifest, env_spec, target, rules_text, remote.now_stamp())
    except plugins.PluginError as exc:
        raise PullError(str(exc))
    out.write(format_plan(manifest, env_spec, target, pl))
    if not yes:
        print(f"未执行（演练）。远程写入要先给用户看上面的步骤，用户确认后运行：bin/agent-env deliver --target {tid} --yes",
              file=err)
        return 0
    rc, res = remote.exec_script(manifest, env_spec, env, pl["snapshot"])
    kv = remote._kv(res.splitlines())
    if rc != 0 or "backup" not in kv:
        raise PullError(f"第 1 步远程备份失败（退出码 {rc}）：{remote._tail(res)}。修复：按输出排查后重跑；远程文件没有改动")
    print(f"已备份到 {kv['backup']}", file=out)
    send = remote.resolve_cmd(manifest, env_spec.get("send"))
    with tempfile.TemporaryDirectory(prefix="agent-env-pull.") as tmp:
        for i, (data, dest, _) in enumerate(pl["files"]):
            local = Path(tmp) / f"f{i}"
            local.write_bytes(data)
            try:
                code, _, e = remote.run(send + [str(local), pl["staging"], f"f{i}"], "", remote._timeout(env_spec))
            except (TimeoutError, OSError) as exc:
                raise PullError(f"第 2 步发送 {dest} 失败：{exc}。修复：确认登录和网络后重跑；远程文件没有改动")
            if code != 0:
                raise PullError(f"第 2 步发送 {dest} 失败（退出码 {code}）：{remote._tail(e)}。修复：确认登录和网络后重跑；远程文件没有改动")
            print(f"已发送 f{i} → {dest}（{len(data)} B）", file=out)
    rc, res = remote.exec_script(manifest, env_spec, env, pl["install"])
    kv = remote._kv(res.splitlines())
    if rc != 0:
        raise PullError(f"第 3 步安装失败（退出码 {rc}）：{remote._tail(res)}。修复：退出码 5 是分片不全，重跑；"
                        f"6 是安装失败，看输出。规则文件 {pl['output']} 没动。{rollback_hint(env_spec, pl)}")
    got = kv.get("preview_sha", "")
    if got != pl["want"]:
        raise PullError(f"第 4 步预览不一致：远程试拉结果 {got[:12] or '没有生成'}（拉取退出码 {kv.get('preview_rc', '?')}），"
                        f"本机期望 {pl['want'][:12]}。定时器没打开，规则文件 {pl['output']} 没动。"
                        f"修复：先 bin/agent-env plugin publish --all 看插件是否都推送了最新内容（推送要用户确认 --yes），"
                        f"再重跑；拉取报错看远程 {remote.remote_path(env_spec, PREVIEW_STATUS)}")
    print(f"预览与本机期望一致（{got[:12]}），打开定时器", file=out)
    rc, res = remote.exec_script(manifest, env_spec, env, pl["activate"])
    kv = remote._kv(res.splitlines())
    if rc != 0 or kv.get("output_sha") != pl["want"]:
        raise PullError(f"第 5 步没完成（退出码 {rc}，skills_config={kv.get('skills_config', '-')}，"
                        f"start_rc={kv.get('start_rc', '?')}，规则文件哈希 {kv.get('output_sha', '?')[:12]}）："
                        f"{remote._tail(res)}。修复：退出码 7 是 skills.external_dirs 不是默认的 []，手工合并后重跑；"
                        f"其他看远程 journalctl --user -u {UNIT}。{rollback_hint(env_spec, pl)}")
    rec = {"target": tid, "transport": "remote-exec", "mode": "pull", "sha256": pl["want"], "bytes": pl["bytes"],
           "delivered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "remote_path": pl["output"],
           "backup": kv.get("backup") or pl["backup"], "skills_config": kv.get("skills_config"),
           "files": {dest: sha(data) for data, dest, _ in pl["files"]}}
    render.atomic_write(remote.delivery_path(tid), json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    print(f"已打开定时拉取：{pl['output']} 哈希 {pl['want'][:12]}，定时器 {kv.get('timer', '?')}，"
          f"skills.external_dirs {kv.get('skills_config', '未设置')}；记录 {render._tilde(remote.delivery_path(tid))}。"
          f"{rollback_hint(env_spec, pl)}", file=out)
    return 0
