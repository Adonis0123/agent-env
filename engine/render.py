#!/usr/bin/env python3
"""Render the rule file a target receives: its profile (supplement) followed by
the shared sections selected for its roles; for a plugin target with `rules_output`, its plugins
composed on this machine (engine/compose.py).

Usage: python3 render.py <manifest> --target <id> [--check | --write]
  default  print the rendered text to stdout
  --check  compare with every installed `output` / `also_output` file (and the host's disabled
           skills list when the target declares one); exit 1 on drift
  --write  snapshot the installed file, then replace it atomically

No user-specific paths live here; every path comes from the manifest.
Requires Python 3.11+ (tomllib).
"""
import sys

if sys.version_info < (3, 11):
    print("render 需要 Python 3.11+（依赖 tomllib），当前是 %d.%d；"
          "请改用 `uv run --python 3.12 python3 render.py ...` 运行。"
          % sys.version_info[:2], file=sys.stderr)
    sys.exit(2)

import argparse  # noqa: E402
import difflib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontmatter  # noqa: E402
from state import state_dir  # noqa: E402
from habits import (LIMIT_UNITS, MANIFEST_PATH_KEY, find_target, limit_unit,  # noqa: E402
                    load_manifest, measure, normalize, read_rules, select_sections, unclosed_fence)  # noqa: E402

GENERATED = "generated"
REMOTE_EXEC = "remote-exec"


class RenderError(Exception):
    """A render, check or write failure; the message already says how to fix it."""


def _tilde(path) -> str:
    """Show a path under the current home directory as `~/...` (stable across shells)."""
    path = str(path)
    home = os.path.expanduser("~")
    if not home or home == "~":
        return path
    for h in (home.rstrip(os.sep), os.path.realpath(home).rstrip(os.sep)):
        if path == h or path.startswith(h + os.sep):
            return "~" + path[len(h):]
    return path


def _regen_command(manifest: dict, target_id: str) -> str:
    engine = _tilde(Path(__file__).resolve())
    manifest_path = _tilde(manifest.get(MANIFEST_PATH_KEY, "<manifest>"))
    return f"python3 {engine} {manifest_path} --target {target_id} --write"


def _is_delivered(target: dict) -> bool:
    return any(isinstance(e, dict) and e.get("kind") == "delivered" for e in target.get("expect", []) or [])


def remote_spec(manifest: dict, target: dict):
    """The [environments.<env>] table when the target lives on a remote-exec host, else None.

    Such a target's `output` is a path on the remote host: this machine never reads or
    writes it; `agent-env deliver` sends it and `agent-env doctor` checks it remotely.
    """
    envs = manifest.get("environments")
    spec = envs.get(target.get("env")) if isinstance(envs, dict) else None
    return spec if isinstance(spec, dict) and spec.get("transport") == REMOTE_EXEC else None


def generated_note(manifest: dict, target: dict) -> str:
    """One line naming the sources and the regenerate command; no timestamp.

    Only a `render = "generated"` target (a local file this engine writes) gets
    source paths; any other target's text may leave this machine (delivered to
    a cloud app or public), so its note carries no path, only the repo command.
    """
    if remote_spec(manifest, target) is not None:
        # The file lives on another host: name no local path, only the delivery command.
        roles = ", ".join(target.get("roles", []))
        profile = f"上方是落点 {target['id']} 的档案，下方" if target.get("supplement") else "内容"
        return (f"<!-- 本文件由 agent-env 生成，不要手改。{profile}是落点 {target['id']} 按角色（{roles}）"
                f"挑出的共享章节。改源文件后在 agent-env 仓库运行：bin/agent-env deliver --target {target['id']} -->\n")
    if target.get("render") != GENERATED:
        roles = ", ".join(target.get("roles", []))
        how = (f"改源文件后运行：bin/agent-env deliver --target {target['id']}，再在该 app 里重新投递"
               if _is_delivered(target) else "改源文件后在 agent-env 仓库里重新生成")
        profile = f"上方是落点 {target['id']} 的档案，下方" if target.get("supplement") else "内容"
        return (f"<!-- 本文件由 agent-env 生成，不要手改。{profile}是落点 {target['id']} 按角色（{roles}）"
                f"挑出的共享章节。{how} -->\n")
    rules = _tilde(manifest.get("sources", {}).get("rules", "<rules>"))
    roles = ", ".join(target.get("roles", []))
    supplement = target.get("supplement")
    profile = f"本落点专属内容改 {_tilde(supplement)}；" if isinstance(supplement, str) else ""
    return (f"<!-- 本文件由 agent-env 生成，不要手改。上方是落点 {target['id']} 的档案，"
            f"下方是从 {rules} 按角色（{roles}）挑出的共享章节。{profile}"
            f"改源文件后运行：{_regen_command(manifest, target['id'])} -->\n")


def compose(manifest: dict, rules_text: str, target_id: str) -> str:
    """Profile text + generated note + selected shared sections, without the size check."""
    target = find_target(manifest, target_id)
    if "plugins" in target:
        # A plugin target has no single rule text: it reads each plugin's AGENTS.md in order. Composing
        # one by roles here would bypass the plugins' visibility, so this path does not exist for it.
        raise RenderError(f"落点「{target_id}」用插件，没有按角色拼出来的单份规则。"
                          f"修复：插件内容用 bin/agent-env plugin build --all 生成，"
                          f"指路规则用 bin/agent-env plugin pointer --target {target_id} 输出")
    fence_line = unclosed_fence(rules_text)
    if fence_line:
        raise RenderError(f"规则源第 {fence_line} 行开启的代码块没有闭合，之后的章节会被丢掉。"
                          f"修复：在规则源里补上闭合围栏，再重新生成")
    supplement = target.get("supplement")
    profile = ""
    if supplement is not None:
        try:
            profile = normalize(Path(supplement).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, TypeError) as exc:
            raise RenderError(f"落点「{target_id}」的档案读不了：{supplement}（{exc}）。"
                              f"修复：确认 supplement 指向存在的 UTF-8 文件（相对清单目录）") from exc
        if profile and not profile.endswith("\n"):
            profile += "\n"
        profile += "\n"
    return (profile + generated_note(manifest, target) + "\n" + select_sections(manifest, rules_text, target_id)
            + skills_block(manifest, target))


def skill_description(skill_md: Path) -> str:
    """The `description` of a SKILL.md front matter, folded to one line ('' if none or unreadable)."""
    try:
        value = frontmatter.scalars(skill_md.read_text(encoding="utf-8")).get("description")
    except frontmatter.FrontMatterError:
        return ""
    return " ".join(value.split()) if isinstance(value, str) else ""


def skills_block(manifest: dict, target: dict) -> str:
    """For a target with `skills = [...]`: the selected shared skills, one line each."""
    names = target.get("skills")
    if not names:
        return ""
    root = manifest.get("sources", {}).get("skills")
    if not isinstance(root, str):
        raise RenderError(f"落点「{target['id']}」选了 skills，但清单 [sources] 没有 skills。"
                          f"修复：在 [sources] 写 skills = \"~/.agents/skills\"（共享 skill 源）")
    lines = ["", "## 选中的 skill", "",
             f"下面 {len(names)} 个 skill 来自共享 skill 源，请装进本落点的 skill 库：", ""]
    for name in names:
        md = Path(root) / name / "SKILL.md"
        try:
            desc = skill_description(md)
        except (OSError, UnicodeDecodeError) as exc:
            raise RenderError(f"落点「{target['id']}」选的 skill「{name}」读不了：{_tilde(md)}（{type(exc).__name__}）。"
                              f"修复：确认共享 skill 源里有它，或从清单 skills 里删掉") from exc
        lines.append(f"- `{name}`：{desc}" if desc else f"- `{name}`")
    return "\n".join(lines) + "\n"


def render_rules(manifest: dict, rules_text: str, target_id: str) -> str:
    """compose() plus the target's rule_limit_bytes check (in rule_limit_unit); never truncates."""
    text = compose(manifest, rules_text, target_id)
    target = find_target(manifest, target_id)
    limit = target.get("rule_limit_bytes", 0)
    unit = limit_unit(target)
    label = LIMIT_UNITS.get(unit, unit)
    size = measure(text, unit)
    if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < size:
        raise RenderError(f"落点「{target_id}」生成结果 {size} {label}，超过上限 {limit} {label} {size - limit} {label}。"
                          f"修复：精简档案 {_supplement_label(manifest, target_id)}，"
                          f"或在清单 [rule_sections] 里把部分章节移出该落点的角色")
    return text


def _size_label(text: str, target: dict) -> str:
    chars = measure(text, "chars")
    size = f"{chars} 字符 / {measure(text, 'bytes')} B"
    limit = target.get("rule_limit_bytes", 0)
    if isinstance(limit, int) and limit > 0:
        unit = limit_unit(target)
        size += f"，上限 {limit} {LIMIT_UNITS.get(unit, unit)}"
    return size


def _supplement_label(manifest: dict, target_id: str) -> str:
    supplement = find_target(manifest, target_id).get("supplement")
    return _tilde(supplement) if isinstance(supplement, str) else "（未配置档案）"


def output_path(target: dict) -> Path:
    out = target.get("output")
    if not isinstance(out, str) or not out:
        raise RenderError(f"落点「{target['id']}」没有配置 output。"
                          f"修复：在清单该落点写 output = \"~/...\"（生成文件路径）")
    return Path(os.path.expanduser(out))


def output_paths(target: dict) -> list:
    """`output` followed by every `also_output` path; all receive the same rendered text."""
    paths = [output_path(target)]
    extra = target.get("also_output", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) and x for x in extra):
        raise RenderError(f"落点「{target['id']}」的 also_output={extra!r} 不是路径列表。"
                          f"修复：写成 also_output = [\"~/...\"]，或删掉该字段")
    for x in extra:
        path = Path(os.path.expanduser(x))
        if path not in paths:
            paths.append(path)
    return paths


# ── skills.disabled in a host's YAML config (read-only, line based) ─────────

_ITEM_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):(.*)$")


def _scalar(raw: str):
    """A plain or quoted YAML scalar; None if it looks like anything else."""
    raw = raw.strip()
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    if not raw or any(c in raw for c in "[]{}&*!|>#,") or raw.startswith(("'", '"')):
        return None
    return raw


def read_skills_disabled(config_text: str):
    """The `skills.disabled` list from a YAML config, or None if it cannot be
    read with simple line rules (the caller reports UNVERIFIED, never guesses).

    Handles: no `skills:` key or no `disabled:` key (empty list), `disabled: []`,
    a flow list of plain scalars, and a block list of `- item` lines.
    """
    lines = normalize(config_text).split("\n")
    start = None
    for i, line in enumerate(lines):
        m = _KEY_RE.match(line)
        if m and not m.group(1) and m.group(2) == "skills":
            if m.group(3).strip() and not m.group(3).strip().startswith("#"):
                return None  # inline mapping such as `skills: {...}`
            if start is not None:
                return None  # duplicate key
            start = i + 1
    if start is None:
        return []
    block = []
    for line in lines[start:]:
        if line.strip() and not line.startswith((" ", "\t")) and not line.startswith("#"):
            break
        block.append(line)
    child_indent = None
    found = None
    i = 0
    while i < len(block):
        line = block[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            return None
        indent = len(line) - len(line.lstrip())
        if child_indent is None:
            child_indent = indent
        m = _KEY_RE.match(line)
        if indent == child_indent and m and m.group(2) == "disabled":
            if found is not None:
                return None
            rest = m.group(3).strip()
            if rest.startswith("#"):
                rest = ""
            if rest:
                if " #" in rest:
                    rest = rest.split(" #", 1)[0].rstrip()
                if not (rest.startswith("[") and rest.endswith("]")):
                    return None
                inner = rest[1:-1].strip()
                items = [] if not inner else [_scalar(x) for x in inner.split(",")]
                if any(x is None for x in items):
                    return None
                found = items
                i += 1
                continue
            items = []
            i += 1
            while i < len(block):
                sub = block[i]
                if not sub.strip() or sub.lstrip().startswith("#"):
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent < child_indent or (sub_indent == child_indent and not sub.lstrip().startswith("-")):
                    break
                im = _ITEM_RE.match(sub)
                if not im:
                    return None
                value = _scalar(im.group(1))
                if value is None:
                    return None
                items.append(value)
                i += 1
            found = items
            continue
        i += 1
    return [] if found is None else found


def check_skills_disabled(target: dict):
    """Return (level, message) comparing manifest skills_disabled with the host config.

    level is "PASS", "FAIL", "UNVERIFIED" or None (target declares nothing).
    """
    expected = target.get("skills_disabled")
    if expected is None:
        return None, ""
    config = target.get("skills_config")
    if not isinstance(config, str):
        return "FAIL", ("清单写了 skills_disabled 但没有 skills_config。"
                        "修复：在该落点写 skills_config = \"~/...\"（宿主配置文件）")
    path = Path(os.path.expanduser(config))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return "UNVERIFIED", f"读不了 {_tilde(path)}（{type(exc).__name__}），无法核对 skills.disabled"
    actual = read_skills_disabled(text)
    if actual is None:
        return "UNVERIFIED", (f"{_tilde(path)} 的 skills.disabled 写法超出简单行解析，没有核对。"
                              f"修复：改成 `disabled:` 下逐行 `- name` 的写法，或人工核对")
    if sorted(actual) == sorted(expected):
        return "PASS", f"skills.disabled 与清单一致：{sorted(actual)}"
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    return "FAIL", (f"skills.disabled 与清单不一致：配置缺 {missing}，配置多出 {extra}。"
                    f"修复：在宿主里调整 {_tilde(path)}，或把清单 skills_disabled 改成当前值")


# ── install ────────────────────────────────────────────────────────────────

SNAPSHOT_META = "snapshot.json"


def snapshot(path: Path, target_id: str, now=None) -> Path | None:
    """Copy the file about to be replaced to snapshots/<UTC time>/<target id>/."""
    snaps = snapshot_files([path], target_id, now)
    return snaps[0] if snaps else None


def snapshot_files(paths, target_id: str, now=None) -> list:
    """Copy every existing file in `paths` into one snapshots/<UTC time>/<target id>/ folder,
    so `agent-env rollback` restores them together. Returns the copies."""
    existing = [Path(p) for p in paths if Path(p).exists()]
    if not existing:
        return []
    now = now or datetime.now(timezone.utc)
    root = state_dir() / "snapshots"
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest_dir = root / stamp / target_id
    n = 1
    while dest_dir.exists():
        dest_dir = root / f"{stamp}-{n}" / target_id
        n += 1
    dest_dir.mkdir(parents=True)
    files, dests = {}, []
    for path in existing:
        name, k = path.name, 1
        while name in files or name == SNAPSHOT_META:  # two outputs may share a basename
            name, k = f"{path.name}.{k}", k + 1
        dest = dest_dir / name
        shutil.copy2(path, dest)
        files[name] = str(path)
        dests.append(dest)
    # Where each copy came from, so `agent-env rollback` can put it back.
    meta = {"target": target_id, "file": dests[0].name, "path": files[dests[0].name], "files": files}
    (dest_dir / SNAPSHOT_META).write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    return dests


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def diff_summary(old: str, new: str, path: Path, limit: int = 40) -> str:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     f"installed {_tilde(path)}", "rendered", lineterm="", n=1))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    head = diff[:limit]
    more = f"\n…（另有 {len(diff) - limit} 行 diff 未显示）" if len(diff) > limit else ""
    return f"已安装文件比生成结果少 {added} 行、多 {removed} 行：\n" + "\n".join(head) + more


def _load(manifest_arg, target_id):
    manifest = load_manifest(manifest_arg)
    try:
        target = find_target(manifest, target_id)
    except KeyError:
        ids = [t.get("id") for t in manifest.get("targets", []) if isinstance(t, dict)]
        raise RenderError(f"清单里没有落点「{target_id}」。修复：--target 改成 {ids} 之一")
    rules_text = read_rules(manifest)
    if rules_text is None:
        sources = manifest.get("sources", {})
        where = sources.get("rules_src") or sources.get("rules")
        raise RenderError(f"规则源读不了：{where}。修复：bin/agent-env check 看原因；[sources] rules_src 指向规则源目录，"
                          f"或 rules 指向存在的 UTF-8 文件")
    return manifest, target, rules_text


def _composed(manifest: dict, target: dict, rules_text: str) -> tuple:
    """(text, [path]) of a target whose plugins are composed on this machine (engine/compose.py)."""
    import compose  # compose imports plugins, which imports this module
    try:
        return compose.render_text(manifest, target, rules_text), [compose.output_path(target)]
    except compose.ComposeError as exc:
        raise RenderError(str(exc)) from exc


def run(argv=None, out=sys.stdout, err=sys.stderr) -> int:
    ap = argparse.ArgumentParser(description="按清单生成落点的规则文件")
    ap.add_argument("manifest")
    ap.add_argument("--target", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="与已安装文件比对，不同就退出 1")
    mode.add_argument("--write", action="store_true", help="快照旧文件后原子写入")
    args = ap.parse_args(argv)
    try:
        manifest, target, rules_text = _load(args.manifest, args.target)
        composed = "rules_output" in target and "plugins" in target
        if composed:
            text, paths = _composed(manifest, target, rules_text)
        else:
            text = render_rules(manifest, rules_text, args.target)
        if not (args.check or args.write):
            out.write(text)
            return 0
        if remote_spec(manifest, target) is not None:
            raise RenderError(f"落点「{args.target}」在远程主机上，本机没有它的文件，--check / --write 不适用。"
                              f"修复：核对用 bin/agent-env doctor --env {target.get('env')}，"
                              f"写入用 bin/agent-env deliver --target {args.target} --dry-run（确认后加 --yes）")
        if not composed and target.get("render") != GENERATED:
            raise RenderError(f"落点「{args.target}」没有声明 render = \"{GENERATED}\"，引擎不负责它的文件。"
                              f"修复：确认要由引擎接管后，在清单该落点写 render = \"{GENERATED}\" 和 output，"
                              f"或（插件落点）写 rules_output")
        if not composed:
            paths = output_paths(target)
            regen, moved = _regen_command(manifest, args.target), "专属内容的改动先搬进档案"
        else:
            import compose  # compose imports plugins, which imports this module
            regen, moved = compose.regen_command(manifest, target), "手改的内容先搬进规则源"
        for path in paths:
            if path.is_symlink():
                raise RenderError(f"{_tilde(path)} 是符号链接，原子写入会把链接换成普通文件。"
                                  f"修复：先确认链接指向，再把 output / also_output 改成链接目标的真实路径")
        current = {path: path.read_text(encoding="utf-8") if path.exists() else None for path in paths}
        if args.check:
            code = 0
            for path in paths:
                if current[path] is None:
                    print(f"FAIL {_tilde(path)} 不存在。修复：运行 {regen}", file=err)
                    code = 1
                elif current[path] != text:
                    print(f"FAIL {_tilde(path)} 与生成结果不一致（漂移）。"
                          f"修复：确认差异后运行 {regen}；"
                          f"{moved}\n{diff_summary(current[path], text, path)}", file=err)
                    code = 1
                else:
                    print(f"PASS {_tilde(path)} 与生成结果一致（{_size_label(text, target)}）", file=out)
            level, msg = check_skills_disabled(target)
            if level:
                print(f"{level} {msg}", file=out if level == "PASS" else err)
                if level == "FAIL":
                    code = 1
            return code
        stale = [path for path in paths if current[path] != text]
        for path in paths:
            if path not in stale:
                print(f"{_tilde(path)} 已是最新，未写入", file=out)
        if not stale:
            return 0
        existing = [path for path in stale if path.exists()]
        snaps = dict(zip(existing, snapshot_files(existing, args.target)))
        for path in stale:
            atomic_write(path, text)
            snap = snaps.get(path)
            where = f"旧文件快照：{_tilde(snap)}" if snap else "原先没有该文件，无快照"
            print(f"已写入 {_tilde(path)}（{_size_label(text, target)}）；{where}", file=out)
        return 0
    except RenderError as exc:
        print(f"FAIL {exc}", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(run())
