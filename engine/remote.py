"""Remote-exec environments: a host this machine reaches only through a command
prefix (for example a cloud provider's "run command" API). Doctor checks run
there as one shell script; deliveries are rendered here and sent in pieces.

Manifest contract:

  [environments.<name>]
  transport = "remote-exec"
  exec = [...]          # reads a POSIX sh script on stdin, runs it on the remote host,
                        # prints the script's stdout, exits with the script's exit code
  send = [...]          # appended args: <local file> <remote dir> <remote file name>
  remote_home = "/..."  # what `~/` means in this environment's paths (needed by deliver)
  remote_user = "..."   # owns delivered files and its `systemctl --user` services
  timeout = 180         # seconds for each exec / send call (default 180)
  chunk_bytes = 24000   # largest piece one send call carries (default 24000)

Command items starting with `./` or `../` resolve against the manifest's
directory, `~/` against this machine's home. Nothing provider-, account- or
user-specific lives in this file. Requires Python 3.11+.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import render
from state import state_dir
from cells import SYNCED, UNKNOWN, cell, habit_of, name_of, tag
from habits import MANIFEST_PATH_KEY

DEFAULT_TIMEOUT = 180
DEFAULT_CHUNK = 24000

PROLOGUE = """set +e
export LC_ALL=C
_ae_sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
"""


class RemoteError(Exception):
    """The remote host could not be reached or answered nonsense; the message says how to fix it."""


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def delivery_path(target_id: str) -> Path:
    return state_dir() / "deliveries" / f"{target_id}.json"


# ── commands ──────────────────────────────────────────────────────────────

def resolve_cmd(manifest: dict, cmd) -> list:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        raise RemoteError(f"命令前缀 {cmd!r} 不是非空字符串列表。修复：在清单里写成 [\"程序\", \"参数\", ...]")
    base = Path(manifest.get(MANIFEST_PATH_KEY, ".")).parent
    out = []
    for item in cmd:
        if item.startswith(("./", "../")):
            item = os.path.normpath(base / item)
        elif item.startswith("~/"):
            item = os.path.expanduser(item)
        out.append(item)
    return out


def run(cmd: list, stdin: str = "", timeout: float = DEFAULT_TIMEOUT):
    """(exit code, stdout, stderr). Kills the whole process group on timeout so a helper's
    children (API clients, sleeps) cannot keep the pipes open; raises TimeoutError."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise TimeoutError(f"{timeout} 秒没有结束")
    return proc.returncode, out, err


def _timeout(spec: dict) -> float:
    t = spec.get("timeout", DEFAULT_TIMEOUT)
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else DEFAULT_TIMEOUT


def _tail(text: str, n: int = 300) -> str:
    text = " ".join((text or "").split())
    return text[-n:] if text else "（无输出）"


def exec_script(manifest: dict, spec: dict, env: str, script: str) -> tuple[int, str]:
    """Run a script remotely; (exit code, stdout). RemoteError when the transport itself fails."""
    cmd = resolve_cmd(manifest, spec.get("exec"))
    try:
        rc, out, err = run(cmd, PROLOGUE + script, _timeout(spec))
    except TimeoutError as exc:
        raise RemoteError(f"远程执行超时（{exc}）。修复：确认环境 {env} 的主机在运行、网络可达，"
                          f"必要时加大清单 [environments.{env}] timeout 后重跑")
    except OSError as exc:
        raise RemoteError(f"远程执行命令起不来：{cmd[0]}（{type(exc).__name__}）。"
                          f"修复：确认清单 [environments.{env}] exec 指向存在的程序")
    if rc == 255:
        raise RemoteError(f"远程执行通道失败（退出码 255）：{_tail(err)}。"
                          f"修复：先确认登录有效、主机在运行，再重跑")
    return rc, out


# ── remote paths ──────────────────────────────────────────────────────────

def remote_path(spec: dict, path: str) -> str:
    """A literal remote path (needs remote_home for `~/`)."""
    if path.startswith("~/"):
        home = spec.get("remote_home")
        if not isinstance(home, str) or not home.startswith("/"):
            raise RemoteError("远程路径用了 ~/ 但环境没有 remote_home。修复：在清单环境表里写 remote_home = \"/home/<用户>\"")
        return home.rstrip("/") + path[1:]
    return path


def shell_path(spec: dict, path: str) -> str:
    """A shell word for a remote path: literal when remote_home is known, else under "$HOME"."""
    if path.startswith("~/") and not isinstance(spec.get("remote_home"), str):
        return '"$HOME"' + shlex.quote(path[1:])
    return shlex.quote(remote_path(spec, path))


# ── doctor ────────────────────────────────────────────────────────────────

def _kv(lines: list) -> dict:
    d = {}
    for line in lines:
        k, sep, v = line.partition("=")
        if sep and k not in d:
            d[k] = v
    return d


def doctor_targets(manifest: dict, env: str, spec: dict, targets: list, rules_text):
    """(target statuses, environment error or None). One remote round trip for all checks; each
    expectation's fragment and parse come from its kind module (engine/expect)."""
    import expect  # the kind modules import this one; loaded lazily to avoid a cycle
    nonce = secrets.token_hex(6)
    marker = f"@@AE-{nonce}"
    plans = []  # (target index, expect index, section number, expectation, kind, parse function)
    results = [[None] * len(t.get("expect", []) or []) for t in targets]
    script, n = [], 0
    gate = None  # the first not-synced cell of a gating kind (login): nothing runs remotely
    for ti, t in enumerate(targets):
        for ei, exp in enumerate(t.get("expect", []) or []):
            k, kind = expect.of(exp), exp.get("kind") if isinstance(exp, dict) else None
            if k is None or k.check_remote is None:
                c = [cell(habit_of(exp) if isinstance(exp, dict) else "rules", str(kind or "expect"),
                          UNKNOWN, f"远程环境不支持期望 kind={kind!r}（先跑 bin/agent-env check）")]
                results[ti][ei] = tag(c, kind) if k is not None else c
                continue
            try:
                plan = k.check_remote(manifest, spec, env, t, exp, rules_text)
            except RemoteError as exc:
                results[ti][ei] = tag([cell(habit_of(exp), name_of(exp, kind), UNKNOWN, str(exc))], kind)
                continue
            if isinstance(plan, list):  # checked on this machine, not in the round trip
                results[ti][ei] = tag(plan, kind)
                if getattr(k, "GATES_REMOTE", False) and gate is None:
                    gate = next((c for c in plan if c["state"] != SYNCED), None)
                continue
            frag, parse = plan
            script.append(f"printf '%s\\n' '{marker} BEGIN {n}'\n( {frag} ) 2>&1\nprintf '{marker} END {n} %s\\n' \"$?\"\n")
            plans.append((ti, ei, n, exp, kind, parse))
            n += 1

    error = None
    sections = {}
    if plans and gate is not None:
        error = {"message": f"环境 {env} 登录无效，远程检查没有跑", "fix": gate["fix"] or "重新登录后重跑 doctor"}
    elif plans:
        try:
            rc, out = exec_script(manifest, spec, env, "".join(script) + "exit 0\n")
            current = None
            for line in out.splitlines():
                if line.startswith(marker + " BEGIN "):
                    current = line.split()[2]
                    sections[current] = []
                elif line.startswith(marker + " END "):
                    current = None
                elif current is not None:
                    sections[current].append(line)
            if not sections:
                raise RemoteError(f"远程执行返回了退出码 {rc}，但输出里没有任何检查结果：{_tail(out)}。"
                                  f"修复：确认 exec 前缀把脚本原样交给远程 sh，并把它的输出打印出来")
        except RemoteError as exc:
            msg = str(exc)
            error = {"message": msg.split("修复：", 1)[0].rstrip("。"),
                     "fix": msg.split("修复：", 1)[1] if "修复：" in msg else msg}
    for ti, ei, i, exp, kind, parse in plans:
        if error is not None:
            c = cell(habit_of(exp), name_of(exp, kind), UNKNOWN, f"远程没有检查：{error['message']}")
        elif str(i) not in sections:
            c = cell(habit_of(exp), name_of(exp, kind), UNKNOWN, "远程输出里缺这一项的结果")
        else:
            try:
                c = parse(sections[str(i)])
            except Exception as exc:  # one bad cell never sinks the environment
                c = cell(habit_of(exp), name_of(exp, kind), UNKNOWN, f"检查出错：{type(exc).__name__}: {exc}")
        results[ti][ei] = tag(c if isinstance(c, list) else [c], kind)
    statuses = []
    for t, cells in zip(targets, results):
        cells = [x for c in cells for x in (c if isinstance(c, list) else [c])]  # one expect may yield many
        statuses.append({"id": t["id"], "host": t.get("host", ""), "env": t.get("env", ""),
                         "roles": list(t.get("roles", [])), "verified": t.get("verified") is True,
                         "host_version": None, "habits": cells})
    return statuses, error


# ── deliver ───────────────────────────────────────────────────────────────

def plan_delivery(spec: dict, target: dict, text: str, stamp: str) -> dict:
    """The remote steps of one delivery, as data (printed by --dry-run, run by --yes)."""
    out = target.get("output")
    if not isinstance(out, str) or not out:
        raise RemoteError(f"落点「{target['id']}」没有 output。修复：在清单写 output = \"~/...\"（远程路径）")
    if not isinstance(spec.get("send"), list):
        raise RemoteError(f"环境 {target.get('env')} 没有 send 命令前缀。修复：在清单环境表里写 send = [...]")
    dest = remote_path(spec, out)
    home = remote_path(spec, "~/x")[:-2]
    folder, base = os.path.dirname(dest), os.path.basename(dest)
    backup_dir = f"{folder}/.agent-env-backups"
    staging = f"{home}/.cache/agent-env/incoming/{stamp}"
    data = text.encode("utf-8")
    size = spec.get("chunk_bytes", DEFAULT_CHUNK)
    size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else DEFAULT_CHUNK
    chunks = [data[i:i + size] for i in range(0, len(data), size)] or [b""]
    owner = spec.get("remote_user")
    chown = f"chown {shlex.quote(owner)}: " if isinstance(owner, str) else ""
    q = shlex.quote
    snapshot = (f"p={q(dest)}\n"
                'if [ -L "$p" ]; then echo "ERR $p 是符号链接，不覆盖"; exit 3; fi\n'
                f"mkdir -p {q(staging)} || exit 4\n"
                + (f"chown -R {q(owner)}: {q(home + '/.cache/agent-env')}\n" if chown else "")
                +
                'if [ -f "$p" ]; then\n'
                f"  mkdir -p {q(backup_dir)} && {chown + q(backup_dir) + ' && ' if chown else ''}"
                f"cp -p \"$p\" {q(backup_dir + '/' + base + '.' + stamp)} || exit 4\n"
                f"  echo backup={backup_dir}/{base}.{stamp}\n"
                "else echo backup=none; fi\n")
    want = hashlib.sha256(data).hexdigest()
    tmp = f"{folder}/.{base}.agent-env.tmp"
    install = (f"s={q(staging)}\np={q(dest)}\n"
               'cat "$s"/part.* > "$s/assembled" || exit 4\n'
               'got=$(_ae_sha "$s/assembled")\n'
               f"if [ \"$got\" != {q(want)} ]; then echo \"sha_mismatch=$got\"; exit 5; fi\n"
               f"mkdir -p {q(folder)} && cp \"$s/assembled\" {q(tmp)} && chmod 644 {q(tmp)} "
               f"{'&& ' + chown + q(tmp) + ' ' if chown else ''}&& mv -f {q(tmp)} \"$p\" || exit 6\n"
               'echo "installed_sha=$(_ae_sha "$p")"\n'
               'rm -rf -- "$s"\n')
    return {"dest": dest, "staging": staging, "backup_dir": backup_dir, "sha256": want, "bytes": len(data),
            "chunks": chunks, "snapshot": snapshot, "install": install}


def format_plan(manifest: dict, spec: dict, target: dict, plan: dict) -> str:
    send = " ".join(shlex.quote(x) for x in resolve_cmd(manifest, spec.get("send")))
    ex = " ".join(shlex.quote(x) for x in resolve_cmd(manifest, spec.get("exec")))
    lines = [f"投递 {target['id']} → 远程 {plan['dest']}（{plan['bytes']} B，sha256 {plan['sha256'][:12]}，"
             f"{len(plan['chunks'])} 片）", "",
             "1. 本机渲染规则（已完成，内容即 bin/agent-env render --target " + target["id"] + " 的输出）",
             f"2. 远程快照旧文件到 {plan['backup_dir']}/，建暂存目录 {plan['staging']}（经 {ex}）："]
    lines += ["     " + x for x in plan["snapshot"].rstrip().splitlines()]
    lines.append(f"3. 分片发送 {len(plan['chunks'])} 片到 {plan['staging']}/：")
    for i, c in enumerate(plan["chunks"]):
        lines.append(f"     {send} <本机临时文件 part.{i:04d}> {plan['staging']} part.{i:04d}   # {len(c)} B")
    lines.append(f"4. 远程拼接、校验 sha256、原子替换 {plan['dest']}，删暂存目录：")
    lines += ["     " + x for x in plan["install"].rstrip().splitlines()]
    lines.append(f"5. 本机核对远程哈希等于 {plan['sha256'][:12]}，写投递记录 {render._tilde(delivery_path(target['id']))}")
    return "\n".join(lines) + "\n"


def deliver(manifest: dict, spec: dict, target: dict, text: str, yes: bool, out, err) -> int:
    tid, env = target["id"], target.get("env")
    stamp = now_stamp()
    plan = plan_delivery(spec, target, text, stamp)
    out.write(format_plan(manifest, spec, target, plan))
    if not yes:
        print(f"未执行（演练）。远程写入要先给用户看上面的步骤，用户确认后运行：bin/agent-env deliver --target {tid} --yes",
              file=err)
        return 0
    rc, res = exec_script(manifest, spec, env, plan["snapshot"])
    kv = _kv(res.splitlines())
    if rc != 0 or "backup" not in kv:
        raise RemoteError(f"第 2 步远程快照失败（退出码 {rc}）：{_tail(res)}。修复：按输出排查后重跑；远程文件没有改动")
    backup = None if kv["backup"] == "none" else kv["backup"]
    print(f"已快照：{backup or '远程原先没有该文件'}", file=out)
    send = resolve_cmd(manifest, spec.get("send"))
    with tempfile.TemporaryDirectory(prefix="agent-env-deliver.") as tmp:
        for i, chunk in enumerate(plan["chunks"]):
            name = f"part.{i:04d}"
            local = Path(tmp) / name
            local.write_bytes(chunk)
            try:
                code, _, e = run(send + [str(local), plan["staging"], name], "", _timeout(spec))
            except (TimeoutError, OSError) as exc:
                raise RemoteError(f"第 3 步发送 {name} 失败：{exc}。修复：确认登录和网络后重跑；远程文件没有改动")
            if code != 0:
                raise RemoteError(f"第 3 步发送 {name} 失败（退出码 {code}）：{_tail(e)}。修复：确认登录和网络后重跑；远程文件没有改动")
            print(f"已发送 {name}（{len(chunk)} B）", file=out)
    rc, res = exec_script(manifest, spec, env, plan["install"])
    kv = _kv(res.splitlines())
    if rc != 0 or kv.get("installed_sha") != plan["sha256"]:
        raise RemoteError(f"第 4 步远程安装或校验失败（退出码 {rc}）：{_tail(res)}。"
                          f"修复：退出码 5 表示分片不全，重跑投递；退出码 6 表示替换失败，远程文件保持原样")
    rec = {"target": tid, "sha256": plan["sha256"], "bytes": plan["bytes"], "delivered_at":
           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "transport": "remote-exec",
           "remote_path": plan["dest"], "backup": backup}
    render.atomic_write(delivery_path(tid), json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    print(f"已投递并校验：{plan['dest']} 哈希 {plan['sha256'][:12]}；记录 {render._tilde(delivery_path(tid))}", file=out)
    return 0
