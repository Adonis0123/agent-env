"""Issues (待办) and glossary for `agent-env status --json` (docs/status-schema.md).

The panel never infers who should act or how to word a problem: everything it shows
comes from here. Pure functions over status dicts plus the manifest, so they can be
tested without a machine. Who acts is a data table (ACTOR_RULES); wording is a data
table (TEMPLATES); host and case display names come from the manifest
([host_names], [env_names], [eval_case_names], target `display_name`).
"""
import os
import re
from datetime import datetime, timezone

from cells import habit_of
from expect import portal

STALE_DAYS = 7

# Sort order from the contract; habit issues sort by their state.
ORDER = {("env_unreadable", "unreadable"): 0, ("env_stale", "stale"): 1, ("habit", "missing"): 2,
         ("habit", "drift"): 3, ("eval", "fail"): 4, ("host_version", "changed"): 5, ("eval_error", "err"): 6}

# (category, source, actor, ai_task). First match wins; "*" matches any source.
# source for habits = which expectation produced the cell (see habit_source);
# for env issues = branch | cloud | remote_login | remote | repo.
ACTOR_RULES = [
    ("env_unreadable", "remote_login", "user", None),   # login expired: only the user can log in again
    ("env_unreadable", "remote", "notice", None),       # remote check failed for another reason; rerun from here
    ("env_unreadable", "repo", "notice", None),         # status branches themselves unreadable (fetch / git)
    ("env_unreadable", "*", "other_env", None),         # another machine never reported
    ("env_stale", "remote", "notice", None),            # remote-exec: re-checked from this machine
    ("env_stale", "*", "other_env", None),
    # cloud / remote: one step is the user's (paste into the app, or OK the dry run); AI does the rest,
    # stops at that step and carries on once the user says it is done
    ("habit", "delivered", "user", "fix"),
    ("habit", "service", "user", "prepare"),            # restarting a remote service needs the user
    ("habit", "secrets", "user", "prepare"),            # credential files hold values only the user can restore
    ("habit", "token", "user", "prepare"),              # only the user can create a token and hand it to the bot
    ("habit", "pulled", "user", "fix"),                 # a host that pulls: publish and the remote install need the user's OK
    ("habit", "plugins", "ai", "fix"),                  # a stale plugin build: AI rebuilds it
    ("habit", "portal-service", "user", "prepare"),     # restarting the portal service: the user confirms
    ("habit", "portal", "ai", "fix"),                   # a portal tool's file is gone: AI finds where it went
    ("habit", "portal-registry", "ai", "fix"),
    ("habit", "generated", "ai", "fix"),
    ("habit", "composed", "ai", "fix"),                 # a stale composed rules file: AI writes it again
    ("habit", "mcp", "ai", "fix"),                      # AI runs the plan; writes are confirm steps
    ("habit", "link", "ai", "fix"),
    ("habit", "contains", "ai", "fix"),
    ("habit", "*", "ai", "fix"),
    ("eval", "*", "ai", "logs"),                        # a rule was not kept: AI reads the replies
    ("eval_error", "*", "notice", None),                # the runner itself failed (UNVERIFIED)
    ("host_version", "*", "ai", "fix"),                 # AI re-checks how the host loads habits; recording is a confirm step
]

KIND_ZH = {"rules": "规则", "skills": "Skills", "hooks": "Hooks", "mcp": "MCP"}
NAME_ZH = {"skills.disabled": "禁用 Skills 列表"}

# (category, state, source) -> (title, impact). Placeholders: {who} host display name,
# {what} habit display name, {kind} habit kind label, {env} environment display name,
# {cases} case names, {n} count, {days}, {date}, {current}, {recorded}.
TEMPLATES = {
    ("habit", "missing", "delivered"): ("{who}的{what}还没投递", "{who}还没收到这份规则，新加的规则它不知道"),
    ("habit", "drift", "delivered"): ("{who}的{what}有更新还没投递", "{who}还在用上次投递的旧规则，新改的规则它不知道"),
    ("habit", "missing", "generated"): ("{who}的{what}还没生成", "{who}启动时读不到这份规则"),
    ("habit", "drift", "generated"): ("{who}的{what}和清单生成的不一致",
                                      "{who}读到的规则和源不同，可能缺新规则，或带着手改的内容"),
    ("habit", "missing", "composed"): ("{who}的{what}还没生成", "{who}启动时读不到共享规则"),
    ("habit", "drift", "composed"): ("{who}的{what}和应该生成的内容不一致",
                                     "{who}读到的规则和规则源不同，可能缺新改的规则，或带着手改的内容"),
    ("habit", "missing", "mcp"): ("{who}少了一个 MCP 工具", "{who}用不了清单里要它有的工具"),
    ("habit", "drift", "mcp"): ("{who}的 MCP 配置和清单不一致", "工具可能重复注册、连不上或用错配置"),
    ("habit", "missing", "link"): ("{who}的{what}链接不见了", "{who}读不到共享规则"),
    ("habit", "drift", "link"): ("{who}的{what}没有链到共享规则", "{who}读的是另一份规则，改共享规则对它不生效"),
    ("habit", "missing", "contains"): ("{who}的{what}文件不存在", "{who}缺这项配置，相关规则不会生效"),
    ("habit", "drift", "contains"): ("{who}的{what}少了一项必要配置", "{who}缺这项配置，相关规则不会生效"),
    ("habit", "drift", "service"): ("{who}的后台服务没在运行", "{who}现在收不到消息，也不会回复"),
    ("habit", "missing", "service"): ("{who}的后台服务没装好", "{who}现在收不到消息，也不会回复"),
    ("habit", "drift", "broken-links"): ("{who}那台机器上有失效的链接", "依赖这些链接的 Skills 可能跑不起来"),
    ("habit", "missing", "broken-links"): ("{who}那台机器上要查的目录不存在", "失效链接这一项没法检查"),
    ("habit", "drift", "secrets"): ("{who}的密钥文件权限不对", "别的用户可能读得到这些密钥"),
    ("habit", "missing", "secrets"): ("{who}缺少登记的密钥", "用到这些密钥的 Skills 和定时任务会失败"),
    ("habit", "missing", "plugins"): ("{who}要拉的{what}还没生成", "{who}拉不到这个插件的内容"),
    ("habit", "drift", "plugins"): ("{who}要拉的{what}比源过期", "{who}下次拉到的还是旧内容，新改的规则和技能它不知道"),
    ("habit", "missing", "pulled"): ("{who}还没开始自己拉取插件", "{who}读的还是旧规则，插件更新了它也拿不到"),
    ("habit", "drift", "pulled"): ("{who}拉到的{what}不是最新", "{who}还在用旧规则，新改的规则和技能它不知道"),
    ("habit", "missing", "token"): ("{who}还没有读私有插件的 token", "{who}拉不到个人插件，只能用公开插件"),
    ("habit", "drift", "token"): ("{who}读私有插件的 token 过期了", "{who}拉不到个人插件的更新，只能用旧版"),
    ("habit", "missing", "portal"): ("{who}的门户里「{what}」工具文件找不到", "在门户里打开这个工具会报错或空白"),
    ("habit", "missing", "portal-registry"): ("{who}的门户工具清单不存在", "门户里只剩内置页，其他工具都打不开"),
    ("habit", "drift", "portal-service"): ("{who}的门户服务没在运行", "门户和里面的工具页面都打不开"),
    ("habit", "missing", "*"): ("{who}缺少{kind}「{what}」", "{who}少了这一项，相关习惯不会生效"),
    ("habit", "drift", "*"): ("{who}的{kind}「{what}」和清单不一致", "{who}的实际做法可能和你定的规则不一样"),
    ("eval", "fail", "*"): ("{who}没通过规则测试：{cases}", "{who}没守住这些规则，平时用也可能照样犯"),
    ("eval_error", "err", "*"): ("{who}的规则测试运行器出错（{n} 项）：{cases}",
                                 "这几项这次没测到，结论记为未核实（UNVERIFIED）"),
    ("host_version", "changed", "*"): ("{who}升级到了 {current}（清单登记的是 {recorded}）",
                                       "新版本可能改了规则的加载方式，要让 AI 重新核对一遍"),
    ("env_unreadable", "unreadable", "branch"): ("{env}还没上报状态", "面板看不到{env}上各个 agent 的情况"),
    ("env_unreadable", "unreadable", "remote_login"): ("{env}登录过期，这次没检查成",
                                                       "{env}上各项都显示未知，问题可能被漏掉"),
    ("env_unreadable", "unreadable", "remote"): ("{env}这次没检查成", "{env}上各项都显示未知，问题可能被漏掉"),
    ("env_unreadable", "unreadable", "repo"): ("读不到各机器上报的状态", "面板只能看到本机的情况"),
    # a cloud-hosted environment never reports by itself: its checks run on the machine that drives it
    ("env_unreadable", "unreadable", "cloud"): ("{driver}还没上报{env}的检查结果",
                                                "面板看不到{env}的插件、指路规则和 token 情况"),
    ("env_stale", "stale", "cloud"): ("{driver}已经 {days} 天没上报{env}的检查结果",
                                      "面板上{env}的结果停在 {date}，之后的变化看不到"),
    ("env_stale", "stale", "*"): ("{env}已经 {days} 天没上报状态", "面板上{env}的结果停在 {date}，之后的变化看不到"),
}

GLOSSARY = {
    "synced": ("已同步", "和源一致，不用管"),
    "customized": ("已定制", "按这台机器或这个 agent 的需要有意改过，是预期内的"),
    "drift": ("不一致", "和清单期望的不一样，要修"),
    "missing": ("缺失", "应该有但没有，要补"),
    "unknown": ("未知", "没法核实，比如云端内容读不到或机器连不上"),
    "rules": ("规则", "告诉 agent 怎么做事的说明文件，如 AGENTS.md"),
    "skills": ("Skills", "agent 按需加载的技能包"),
    "hooks": ("Hooks", "agent 做事前后自动跑的检查脚本"),
    "mcp": ("MCP", "agent 能调用的外部工具服务"),
    "ai": ("AI 能修", "让 AI 自己处理，写操作前会先问你"),
    "user": ("要你处理", "要你亲手做或确认，比如登录、在 app 里存规则"),
    "other_env": ("去另一台机器", "要到对应的机器上跑命令"),
    "notice": ("提醒", "只是告诉你一声，通常不用动手"),
    "pass": ("通过", "这次规则测试里 agent 守住了规则"),
    "fail": ("没过", "agent 没守住规则"),
    "err": ("出错", "规则测试工具自己出错，这次没测到，结论未核实"),
    "skip": ("跳过", "这个用例不适用，或这个 agent 没法做规则测试"),
    "safety": ("安全层", "安全底线相关的定制，优先级最高"),
    "env": ("环境层", "因为所在机器不同而做的定制"),
    "role": ("角色层", "因为 agent 角色不同（命令行、机器人、定时任务）而做的定制"),
    "host": ("宿主层", "某个 agent 自己特有的定制"),
}

KEBAB_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")
YES_RE = re.compile(r"(^|\s)--yes(\s|$)")


def glossary() -> dict:
    return {k: {"label_zh": a, "explain_zh": b} for k, (a, b) in GLOSSARY.items()}


# ── names ─────────────────────────────────────────────────────────────────

def _table(manifest: dict, key: str) -> dict:
    t = manifest.get(key)
    return {k: v for k, v in t.items() if isinstance(v, str)} if isinstance(t, dict) else {}


def _targets(manifest: dict) -> list:
    ts = manifest.get("targets")
    return [t for t in ts if isinstance(t, dict)] if isinstance(ts, list) else []


def _find(manifest: dict, target_id: str) -> dict:
    return next((t for t in _targets(manifest) if t.get("id") == target_id), {})


def _readable(raw: str, fallback: str) -> str:
    return fallback if KEBAB_RE.search(raw or "") else raw


def env_name(manifest: dict, env: str) -> str:
    return _table(manifest, "env_names").get(env) or _readable(env, "一台机器")


def host_name(manifest: dict, target_id: str, host: str = "", env: str = "") -> str:
    """Display name of a target: its display_name, else [host_names]; a host that also runs
    in an earlier-listed environment gets that environment's name in front."""
    t = _find(manifest, target_id)
    if isinstance(t.get("display_name"), str) and t["display_name"]:
        return t["display_name"]
    host = host or t.get("host", "")
    env = env or t.get("env", "")
    name = _table(manifest, "host_names").get(host) or _readable(host, "这个 agent")
    first = next((x.get("env") for x in _targets(manifest) if x.get("host") == host), env)
    return name if not env or first == env else f"{env_name(manifest, env)} 上的 {name}"


def case_name(manifest: dict, case: str) -> str:
    return _table(manifest, "eval_case_names").get(case, case)


def annotate_eval(status: dict, manifest: dict) -> dict:
    """Add eval.results[].case_zh in place (the raw case name stays in `case`). Results of a target the
    manifest no longer lists in this environment (a renamed or moved target's leftovers) are taken out
    and named in eval.ignored_targets, so they make no row and no issue; the stored file is not touched."""
    ev = status.get("eval")
    if isinstance(ev, dict) and isinstance(ev.get("results"), list) and isinstance(manifest.get("targets"), list):
        env = str(status.get("env", ""))
        known = {t.get("id") for t in _targets(manifest)
                 if t.get("env") == env or str(t.get("id", "")).startswith(env + "/")}
        gone = sorted({str(r.get("target")) for r in ev["results"] if isinstance(r, dict) and r.get("target") not in known})
        if gone:
            ev["results"] = [r for r in ev["results"] if not isinstance(r, dict) or r.get("target") in known]
            ev["ignored_targets"] = gone
    for r in (status.get("eval") or {}).get("results", []) or []:
        if isinstance(r, dict) and isinstance(r.get("case"), str):
            r["case_zh"] = "-" if r["case"] == "-" else case_name(manifest, r["case"])
    return status


# ── classification ────────────────────────────────────────────────────────

def _env_spec(manifest: dict, env: str) -> dict:
    envs = manifest.get("environments")
    spec = envs.get(env) if isinstance(envs, dict) else None
    return spec if isinstance(spec, dict) else {}


def is_remote(manifest: dict, env: str) -> bool:
    return _env_spec(manifest, env).get("transport") == "remote-exec"


def is_cloud(manifest: dict, env: str) -> bool:
    return _env_spec(manifest, env).get("transport") == "cloud-hosted"


def doctor_cmd(manifest: dict, env: str) -> str:
    """The doctor command that re-checks `env`: a cloud-hosted or remote one is never inferred."""
    return f"bin/agent-env doctor --env {env}" if env and (is_cloud(manifest, env) or is_remote(manifest, env)) \
        else "bin/agent-env doctor"


def habit_source(manifest: dict, target: dict, cell: dict) -> str:
    """Which check produced a cell: its `expect_kind` (an expectation kind such as delivered | generated |
    link | contains | service | broken-links | secrets | login | company-skills | portal, or mcp | accounts). A portal
    cell is narrowed by its name to portal-registry | portal-service | portal (one tool). A generated
    file in a remote-exec environment is delivered there (remote write), so it counts as delivered."""
    src = cell.get("expect_kind")
    if not isinstance(src, str) or not src:
        src = _guess_source(manifest, target, cell)
    elif src == "portal":
        src = _portal_part(manifest, target, cell)
    if src == "generated" and is_remote(manifest, target.get("env", "")):
        src = "delivered"
    return src


def _guess_source(manifest: dict, target: dict, cell: dict) -> str:
    """Fallback for cells without `expect_kind`: reports written before the field existed, which status
    branches from machines still on an older engine keep sending. Matches the manifest by name, then
    guesses from the fix text; drop it once no such branch is left."""
    t = _find(manifest, target.get("id", ""))
    src = None
    for exp in t.get("expect", []) or []:
        if not isinstance(exp, dict) or habit_of(exp) != cell.get("kind"):
            continue
        if exp.get("kind") == "portal":
            src = portal.part(exp, str(cell.get("name", "")))
            if src:
                break
        elif exp.get("name") == cell.get("name"):
            src = exp.get("kind")
            break
    if src is not None:
        return src
    fix = str(cell.get("fix", ""))
    if cell.get("name") == "skills.disabled":
        return "generated"
    if cell.get("kind") == "mcp":
        return "mcp"
    if "agent-env deliver" in fix:
        return "delivered"
    if "agent-env render" in fix:
        return "generated"
    if fix.startswith("ln -s"):
        return "link"
    return "other"


# Where a habit reaches an agent from, for the panel's relation graph (read-only). Keys of the manifest's
# [sources] (eval is a test runner, not a habit, so it is left out), plus "portal" (the Local Tools registry a
# portal expectation reads) and "manifest" (what the manifest itself lists: MCP servers, skills.disabled).
ORIGIN_SOURCES = ("rules", "docs", "hooks", "skills", "company_skills")


def _basename(path) -> str:
    return str(path or "").rstrip("/").rsplit("/", 1)[-1]


def _under(path: str, root: str) -> bool:
    p, r = os.path.normpath(os.path.expanduser(path)), os.path.normpath(os.path.expanduser(root))
    return p == r or p.startswith(r + os.sep)


def cell_origin(manifest: dict, target: dict, cell: dict):
    """Which source a habit cell comes from: a [sources] key, "portal", "manifest", or None for a fact of the
    machine itself (a login, a service, broken links, a setting only that machine has, the account folders)."""
    t = _find(manifest, target.get("id", ""))
    kind, name = cell.get("kind"), str(cell.get("name", ""))
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), dict) else {}
    if kind == "mcp" or name == "skills.disabled":
        return "manifest"
    src = habit_source(manifest, target, cell)
    if src.startswith("portal"):
        return "portal"
    if kind == "skills" and name in [str(x) for x in t.get("company_skills", []) or []]:
        return "company_skills"
    outputs = {_basename(t.get("output"))} | {_basename(x) for x in t.get("also_output", []) or []}
    if src == "token":
        return "manifest"
    if kind == "rules" and (src in ("generated", "delivered", "plugins", "pulled", "composed")
                            or (t.get("render") == "generated" and name in outputs)):
        return "rules"
    exp = next((e for e in t.get("expect", []) or [] if isinstance(e, dict)
                and habit_of(e) == kind and e.get("name") == name), None)
    if exp is None or exp.get("kind") not in ("link", "contains"):
        return None
    if exp.get("kind") == "link" and isinstance(exp.get("to"), str):
        hit = next((k for k in ORIGIN_SOURCES if isinstance(sources.get(k), str) and _under(exp["to"], sources[k])), None)
        if hit:
            return hit
    rules_file = _basename(sources.get("rules"))
    if rules_file and (name == rules_file or rules_file in str(exp.get("text", ""))):
        return "rules"
    return "hooks" if kind == "hooks" and sources.get("hooks") else None


def _portal_exp(manifest: dict, target: dict, cell: dict):
    """The target's portal expectation that made this cell, or None once the manifest no longer has it."""
    name = str(cell.get("name", ""))
    for exp in _find(manifest, target.get("id", "")).get("expect", []) or []:
        if (isinstance(exp, dict) and exp.get("kind") == "portal" and habit_of(exp) == cell.get("kind")
                and portal.part(exp, name)):
            return exp
    return None


def _portal_part(manifest: dict, target: dict, cell: dict) -> str:
    """Which part of the portal check a cell (expect_kind portal) is: the registry, the service or one tool."""
    return portal.part(_portal_exp(manifest, target, cell), str(cell.get("name", "")))


def actor_of(category: str, source: str):
    for cat, src, actor, task in ACTOR_RULES:
        if cat == category and src in ("*", source):
            return actor, task
    return "notice", None


def _sync_command(manifest: dict, env: str):
    """[environments.<env>] sync_command (compose.sync_command, read here without importing compose)."""
    envs = manifest.get("environments") if isinstance(manifest.get("environments"), dict) else {}
    spec = envs.get(env)
    v = spec.get("sync_command") if isinstance(spec, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() and "\n" not in v else None


def _template(category, state, source):
    return TEMPLATES.get((category, state, source)) or TEMPLATES.get((category, state, "*"))


def _cmd(text):
    """A fix usable as a copyable command: ASCII only, no --yes. Else None."""
    text = (text or "").strip()
    if text and text.isascii() and "\n" not in text and not YES_RE.search(text):
        return text
    return None


def step(who, text_zh, command=None, confirm=False) -> dict:
    if command is not None and YES_RE.search(command):  # never a copyable real write
        text_zh, command = f"{text_zh}（命令：{command}）", None
    return {"who": who, "text_zh": spaced(text_zh), "command": command, "confirm": confirm}


def _habit_steps(source, tid, who, cell, remote=False, pending=None, recheck="bin/agent-env doctor",
                 sync=None) -> list:
    """`pending`: None for a target without plugins, else the plugins of its pointer rule that have no
    repository yet. `recheck`: the doctor command for the target's environment. `sync`: the environment's
    sync_command, when its own sync writes composed files."""
    fix = str(cell.get("fix", ""))
    if source == "delivered" and pending is not None:  # a plugin target stores only its pointer rule
        # AI does everything but the paste; a plugin without a repository is a precondition it stops at
        names = "、".join(pending)
        first = ([step("ai", f"先查前置条件：插件{names}在清单里还没写 repo，这时指路规则里是占位符，"
                             "存进去也拉不到，记录也会被拒绝。停在这一步，说明缺哪个插件仓，不往下做"),
                  step("user", f"先建好插件仓{names}（或给出已有的仓库地址），在清单 [plugins.<名字>] 写上 repo；"
                               "写好后让 AI 从下一步接着做")] if pending else [])
        return first + [
            step("ai", "把插件生成到最新（只写本机，不提交不推送）", "bin/agent-env plugin build --all"),
            step("ai", "生成指路规则（写到待投递文件），把全文显示出来，并复制到剪贴板",
                 f"bin/agent-env plugin pointer --target {tid}"),
            step("user", f"在 {who} 里粘贴发送，让它存进长期记忆，等它回复已存好，再告诉 AI「已粘贴」"),
            step("ai", "你说已存好后，记录这次投递", f"bin/agent-env plugin pointer --target {tid} --record", True),
            step("ai", "重新体检确认", recheck)]
    if source == "token":
        return [step("user", fix or "按清单登记的 token 名字续期，并更新清单里的到期日"),
                step("ai", "你更新清单后重新体检确认", recheck)]
    if source == "pulled":  # a remote host pulls the plugins itself: publish, then (re)install or run the puller
        return [step("ai", "先演练发布，看插件有没有改动还没推送（生成、扫描、本地提交，不推送）",
                     "bin/agent-env plugin publish --all"),
                step("user", "演练里有待推送的提交时，确认推送；确认后由 AI 加 --yes 运行（会推送到插件仓）", None, True),
                step("ai", "再演练远程拉取的安装步骤（装脚本和定时器、先试拉到预览文件）",
                     f"bin/agent-env deliver --target {tid} --dry-run"),
                step("user", f"看过演练后确认；确认后由 AI 运行 bin/agent-env deliver --target {tid} --yes"
                     "（改远程文件，预览和本机一致才打开定时器）", None, True),
                step("ai", "重新体检确认", recheck)]
    if source == "plugins":
        return [step("ai", "按源重新生成插件目录（只写本机，不提交不推送）", _cmd(fix) or "bin/agent-env plugin build --all"),
                step("ai", "重新体检确认", recheck)]
    if source == "delivered":
        if remote:  # remote-exec target: deliver writes the remote file
            return [step("ai", "先演练投递，看会在远程做哪些事", f"bin/agent-env deliver --target {tid} --dry-run"),
                    step("user", f"看过演练后确认投递；确认后由 AI 运行 bin/agent-env deliver --target {tid} --yes"
                         "（会改远程文件）", None, True)]
        return [step("ai", "生成要投递的内容（写到待投递文件），把全文显示出来，并复制到剪贴板",
                     f"bin/agent-env deliver --target {tid}"),
                step("user", f"在 {who} 里粘贴发送，等它回复已存好，再告诉 AI「已粘贴」"),
                step("ai", "你说已存好后，记录这次投递", f"bin/agent-env deliver --target {tid} --record", True),
                step("ai", "重新体检确认", recheck)]
    if source == "generated":
        return [step("ai", "先看生成结果和现有文件差在哪", f"bin/agent-env render --target {tid} --check"),
                step("ai", "写入新生成的文件（会先留快照，可回滚）", _cmd(fix) or f"bin/agent-env render --target {tid} --write")]
    if source == "composed":
        if sync:  # the environment's own sync (an assembler reading the rules source from a clone) writes it
            return [step("ai", "重新同步并组装宿主目录（会按插件重新合成规则文件，旧文件先备份）", sync),
                    step("ai", "重新体检确认", recheck)]
        return [step("ai", "先看合成结果和现有文件差在哪", f"bin/agent-env render --target {tid} --check"),
                step("ai", "手改过的内容先搬进规则源，再写入合成的文件（会先留快照，可回滚）",
                     f"bin/agent-env render --target {tid} --write"),
                step("ai", "重新体检确认", recheck)]
    if source == "mcp":
        return [step("ai", "先列出修复计划（只读，不改配置）",
                     f"python3 engine/mcp.py manifest/habits.toml --target {tid} --plan"),
                step("ai", f"你确认后按计划改配置：{fix}", None, True)]
    if source == "service":
        return [step("ai", "先看远程服务日志找原因"),
                step("user", f"确认后重启服务：{fix}", None, True)]
    command = _cmd(fix)
    return [step("ai", "按修法处理" if command else f"按修法处理：{fix}", command),
            step("ai", "重新体检确认", recheck)]


def _version_steps(tid, who, hv, where, recheck) -> list:
    """A host upgrade: AI re-checks what the manifest records about how the host loads habits, the way the
    agent-env skill's onboarding table (第 3 节) says to check each item: the vendor's own release notes or
    documentation, then a test on the machine. Nothing changed: recording the version is a confirm step.
    Something changed: AI stops and reports, the recorded version stays."""
    recorded, current = hv.get("recorded") or "?", hv.get("current") or "?"
    return [
        step("ai", f"联网查{who}从{recorded}到{current}的发布说明和官方文档（只认一手来源），按 agent-env skill "
                   "第 3 节那张表逐项看有没有变：规则文件和加载顺序、skill 目录、hook 格式、MCP 配置位置、"
                   "自动导入别家配置、规则体积上限"),
        step("ai", f"在{where}上实测清单里{tid}登记的加载方式还成立：先跑体检，再按那张表的「怎么查」"
                   "用宿主自己的 inspect / debug 输出或日志核实", recheck),
        step("ai", "有变化就停在这一步：报告哪一项变了、出处链接、清单或适配规则要改哪里，不改登记的版本；"
                   "没变化再做下一步"),
        step("ai", f"给你看核对结论，你同意后把清单 manifest/habits.toml 里{tid}的 host_version.recorded 改成{current}",
             None, True),
        step("ai", "重新体检确认", recheck)]


CJK = "\u3400-\u9fff"


def spaced(text: str) -> str:
    """A space between CJK and Latin letters or digits: 「Grok Bot 的云端规则」."""
    text = re.sub(rf"([A-Za-z0-9.)])([{CJK}])", r"\1 \2", text)
    return re.sub(rf"([{CJK}])([A-Za-z0-9(])", r"\1 \2", text)


def _pointer_pending(manifest: dict, tid: str):
    """None when the target has no plugins; else its pushed plugins that have no `repo` yet."""
    t = _find(manifest, tid)
    if not isinstance(t.get("plugins"), list):
        return None
    table = manifest.get("plugins") if isinstance(manifest.get("plugins"), dict) else {}
    return [n for n in t["plugins"] if isinstance(n, str) and isinstance(table.get(n), dict)
            and table[n].get("visibility") in ("public", "personal", "company") and not table[n].get("repo")]


def _issue(id_, env, target, category, state, source, names, steps, ref) -> dict:
    actor, task = actor_of(category, source)
    title, impact = _template(category, state, source)
    return {"id": id_, "env": env, "target": target, "category": category, "state": state, "actor": actor,
             "title_zh": spaced(title.format(**names)), "impact_zh": spaced(impact.format(**names)), "steps": steps,
             "ai_task": task, "ref": ref}


def _names(**kw) -> dict:
    base = {"who": "", "what": "", "kind": "", "env": "", "cases": "", "n": 0, "days": 0, "date": "",
            "current": "", "recorded": "", "driver": ""}
    base.update(kw)
    return base


# ── building ──────────────────────────────────────────────────────────────

def _parse_time(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _env_problem(manifest, env, message, fix, category, extra=None):
    """An env_unreadable / env_stale issue."""
    remote = is_remote(manifest, env)
    if not env:
        source = "repo"
    elif remote and category == "env_unreadable" and re.search(r"登录|login|cloudsso", f"{message} {fix}", re.I):
        source = "remote_login"
    elif remote:
        source = "remote"
    elif is_cloud(manifest, env):
        source = "cloud"
    else:
        source = "branch"
    en = env_name(manifest, env) if env else ""
    driver = _env_spec(manifest, env).get("driven_from") if source == "cloud" else None
    dn = env_name(manifest, driver) if isinstance(driver, str) and driver else "负责检查它的那台机器"
    names = _names(env=en, driver=dn, **(extra or {}))
    state = "unreadable" if category == "env_unreadable" else "stale"
    if source == "remote_login":
        steps = [step("user", f"按提示重新登录：{fix}"),
                 step("ai", "登录后重新检查并记下结果", f"bin/agent-env status --env {env} --commit")]
    elif source in ("remote", "repo") and env:
        steps = [step("ai", "看错误原因，处理后从本机重新检查", f"bin/agent-env status --env {env} --commit")]
    elif source == "repo":
        steps = [step("ai", f"按提示处理：{fix}"),
                 step("ai", "重新读取各机器的状态", "bin/agent-env status --json --all --fetch")]
    elif source == "cloud":
        # nothing runs on the cloud-hosted environment itself: the machine that drives it checks and reports
        steps = [step("other_env", f"在 {dn} 上跑一遍{en}的体检看结果（只读，不提交也不推送）",
                      f"bin/agent-env status --env {env}"),
                 step("other_env", f"看过结果后确认，再在 {dn} 上运行 bin/agent-env status --env {env} --commit --push"
                                   "（会提交并推送状态分支）", None, True),
                 step("ai", "回到本机拉取最新状态", "bin/agent-env status --json --all --fetch")]
    else:
        # `status` alone only checks: it is the dry run; the write (--commit --push) is a confirm step, never copyable
        steps = [step("other_env", f"在 {en} 上先跑一遍体检看结果（只读，不提交也不推送）", "bin/agent-env status"),
                 step("other_env", "看过结果后确认，再运行 bin/agent-env status --commit --push（会提交并推送状态分支）",
                      None, True),
                 step("ai", "回到本机拉取最新状态", "bin/agent-env status --json --all --fetch")]
    ref = {"kind": None, "name": env or None, "case": None, "detail": message}
    return _issue(f"{env or '-'}:{category}:{env or '-'}", env, None, category, state, source, names, steps, ref)


def env_issues(status: dict, manifest: dict, now=None) -> list:
    """Issues of one environment status (unsorted)."""
    env = status.get("env", "")
    out = []
    err = status.get("error")
    if isinstance(err, dict):
        out.append(_env_problem(manifest, env, str(err.get("message", "")), str(err.get("fix", "")),
                                "env_unreadable"))
    elif status.get("source") == "branch":
        when, now = _parse_time(status.get("generated_at")), now or datetime.now(timezone.utc)
        if when is not None and (now - when).days >= STALE_DAYS:
            out.append(_env_problem(manifest, env, f"最后上报 {status.get('generated_at')}", "", "env_stale",
                                    {"days": (now - when).days, "date": when.strftime("%Y-%m-%d")}))
    for t in status.get("targets", []) or []:
        tid = t.get("id", "")
        who = host_name(manifest, tid, t.get("host", ""), t.get("env", ""))
        for c in t.get("habits", []) or []:
            if c.get("state") not in ("drift", "missing"):
                continue
            source = habit_source(manifest, t, c)
            raw = str(c.get("name", ""))
            shown = portal.tool_label(_portal_exp(manifest, t, c), raw) if source == "portal" else raw
            what = NAME_ZH.get(raw) or _readable(shown, KIND_ZH.get(c.get("kind"), "配置"))
            names = _names(who=who, what=what, kind=KIND_ZH.get(c.get("kind"), ""))
            ref = {"kind": c.get("kind"), "name": raw, "case": None, "detail": c.get("detail", "")}
            tenv = t.get("env", "") or env
            out.append(_issue(f"{tid}:{c.get('kind')}:{raw}", env, tid, "habit", c["state"], source, names,
                              _habit_steps(source, tid, who, c, is_remote(manifest, tenv),
                                           _pointer_pending(manifest, tid), doctor_cmd(manifest, tenv),
                                           _sync_command(manifest, tenv)), ref))
        hv = t.get("host_version")
        if isinstance(hv, dict) and hv.get("changed"):
            names = _names(who=who, current=hv.get("current") or "?", recorded=hv.get("recorded") or "?")
            steps = _version_steps(tid, who, hv, env_name(manifest, t.get("env", "") or env),
                                   doctor_cmd(manifest, t.get("env", "") or env))
            ref = {"kind": "host_version", "name": t.get("host", ""), "case": None}
            out.append(_issue(f"{tid}:host_version:{t.get('host', '')}", env, tid, "host_version", "changed",
                              "*", names, steps, ref))
    groups = {}
    for r in (status.get("eval") or {}).get("results", []) or []:
        if isinstance(r, dict) and r.get("state") in ("fail", "err"):
            groups.setdefault((r.get("target", ""), r["state"]), []).append(r)
    for (tid, state), rows in groups.items():
        category = "eval" if state == "fail" else "eval_error"
        t = next((x for x in status.get("targets", []) or [] if x.get("id") == tid), {})
        who = host_name(manifest, tid, t.get("host", ""), t.get("env", "") or env)
        cases = [str(r.get("case", "")) for r in rows]
        names = _names(who=who, cases="、".join(case_name(manifest, c) for c in cases), n=len(rows))
        if state == "fail":
            steps = [step("ai", "看这次规则测试的回复，找出哪条规则没守住、为什么"),
                     step("ai", "改好规则或用例后重新做规则测试", f"bin/agent-env eval --target {tid}")]
        else:
            steps = [step("ai", "看运行器的报错日志，确认是宿主超时、崩溃还是运行器本身的问题"),
                     step("ai", "处理后重新做规则测试", f"bin/agent-env eval --target {tid}")]
        ref = {"kind": "eval", "name": None, "case": ",".join(cases),
               "detail": " | ".join(str(r.get("detail", "")) for r in rows)}
        out.append(_issue(f"{tid}:{category}:{state}", env, tid, category, state, "*", names, steps, ref))
    return out


def sort_issues(issues: list) -> list:
    seen, out = {}, []
    for i in sorted(issues, key=lambda i: (ORDER.get((i["category"], i["state"]), 99), i["env"] or "",
                                           i["target"] or "", i["id"])):
        n = seen[i["id"]] = seen.get(i["id"], 0) + 1
        if n > 1:
            i = dict(i, id=f"{i['id']}#{n}")
        out.append(i)
    return out


def build(environments: list, errors: list, manifest: dict, now=None) -> list:
    """All issues: per-environment ones, plus errors of environments that have no status."""
    have = {e.get("env") for e in environments}
    out = []
    for e in environments:
        out += env_issues(e, manifest, now)
    for err in errors:
        if err.get("env") not in have or not err.get("env"):
            out.append(_env_problem(manifest, err.get("env", ""), str(err.get("message", "")),
                                    str(err.get("fix", "")), "env_unreadable"))
    return sort_issues(out)
