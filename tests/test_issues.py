import io
import json
import re
import sys
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import issues  # noqa: E402
from test_agent_env import Base  # noqa: E402

NOW = datetime(2026, 11, 9, 8, 0, 0, tzinfo=timezone.utc)
KEBAB = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")

MANIFEST = {
    "environments": {"mac": {"platform": "darwin"}, "wsl": {"platform": "linux"},
                     "ecs-box": {"transport": "remote-exec"}},
    "host_names": {"claude-code": "Claude Code", "codex": "Codex", "grok-bot": "Grok Bot", "hermes": "Hermes"},
    "env_names": {"mac": "Mac", "wsl": "WSL", "ecs-box": "云主机"},
    "eval_case_names": {"no-commit-after-fix": "修完不擅自提交", "external-send-asks": "对外发送前先问你"},
    "targets": [
        {"id": "mac/claude-code", "host": "claude-code", "env": "mac",
         "expect": [{"kind": "contains", "habit": "rules", "name": "AGENTS.md"}]},
        {"id": "mac/codex", "host": "codex", "env": "mac",
         "expect": [{"kind": "link", "habit": "rules", "name": "AGENTS.md"}]},
        {"id": "mac/hermes", "host": "hermes", "env": "mac",
         "expect": [{"kind": "generated", "habit": "rules", "name": "AGENTS.md"}]},
        {"id": "grok-bot/bot", "host": "grok-bot", "env": "grok-bot",
         "expect": [{"kind": "delivered", "habit": "rules", "name": "云端规则"}]},
        {"id": "ecs-box/hermes", "host": "hermes", "env": "ecs-box", "display_name": "小 helper",
         "expect": [{"kind": "generated", "habit": "rules", "name": "AGENTS.md"}]},
        {"id": "wsl/claude-code", "host": "claude-code", "env": "wsl"},
    ],
}


def cell(kind, name, state, fix="some fix", detail="d"):
    return {"kind": kind, "name": name, "state": state, "layer": "host", "detail": detail, "fix": fix}


def target(tid, host, env, habits, hv=None):
    return {"id": tid, "host": host, "env": env, "roles": ["cli"], "verified": True,
            "host_version": hv, "habits": habits}


def env_status(env, targets, results=(), source="live", generated_at="2026-11-09T07:00:00Z", error=None):
    s = {"schema": 1, "env": env, "source": source, "generated_at": generated_at, "engine_rev": "x",
         "summary": {}, "check": {}, "targets": targets, "eval": {"ran_at": None, "results": list(results)}}
    if error:
        s["error"] = error
    return s


def ev(tid, case, state):
    return {"target": tid, "case": case, "state": state, "runs": 1, "passed": int(state == "pass"), "detail": ""}


def mac():
    return env_status("mac", [
        target("mac/claude-code", "claude-code", "mac", [cell("rules", "AGENTS.md", "drift", "在 x 里加上 y"),
                                                          cell("rules", "x", "synced", ""),
                                                          cell("rules", "y", "customized", ""),
                                                          cell("rules", "z", "unknown", "")],
               hv={"recorded": "1.0", "current": "1.1", "changed": True}),
        target("mac/codex", "codex", "mac", [cell("rules", "AGENTS.md", "missing", "ln -s ~/a ~/b"),
                                             cell("mcp", "kimi-cu", "drift", "claude mcp add kimi-cu")]),
        target("mac/hermes", "hermes", "mac", [cell("rules", "AGENTS.md", "drift",
                                                    "bin/agent-env render --target mac/hermes --write")]),
        target("grok-bot/bot", "grok-bot", "grok-bot", [cell("rules", "云端规则", "missing",
                                                          "bin/agent-env plugin pointer --target grok-bot/bot ...")]),
    ], results=[ev("mac/codex", "external-send-asks", "fail"), ev("mac/codex", "no-commit-after-fix", "pass"),
                ev("mac/claude-code", "no-commit-after-fix", "err"),
                ev("mac/claude-code", "external-send-asks", "err"), ev("mac/claude-code", "x", "skip")])


def remote_ok():
    return env_status("ecs-box", [target("ecs-box/hermes", "hermes", "ecs-box", [
        cell("rules", "AGENTS.md", "missing",
             "bin/agent-env deliver --target ecs-box/hermes --dry-run 看步骤，用户确认后加 --yes 投递")])],
        source="branch")


def build(envs, errors=()):
    return issues.build(envs, list(errors), MANIFEST, NOW)


def by_cat(found, category):
    return [i for i in found if i["category"] == category]


class CategoryTests(unittest.TestCase):
    def setUp(self):
        stale = env_status("wsl", [], source="branch", generated_at="2026-10-30T00:00:00Z")
        login = env_status("ecs-box", [], source="branch",
                           error={"message": "环境 ecs-box 登录无效，远程检查没有跑", "fix": "你本人重新登录"})
        self.found = build([mac(), stale, login])

    def test_every_category_present(self):
        cats = {i["category"] for i in self.found}
        self.assertEqual(cats, {"habit", "eval", "eval_error", "host_version", "env_unreadable", "env_stale"})

    def test_fields(self):
        keys = {"id", "env", "target", "category", "state", "actor", "title_zh", "impact_zh", "steps",
                "ai_task", "ref"}
        for i in self.found:
            self.assertTrue(keys <= set(i), i)
            self.assertIn(i["actor"], ("ai", "user", "other_env", "notice"))
            self.assertIn(i["ai_task"], ("fix", "prepare", "logs", None))
            for s in i["steps"]:
                self.assertEqual(set(s), {"who", "text_zh", "command", "confirm"})

    def test_stale_and_case_names(self):
        stale = by_cat(self.found, "env_stale")[0]
        self.assertEqual((stale["env"], stale["actor"]), ("wsl", "other_env"))
        self.assertIn("10 天", stale["title_zh"])
        # the dry run (`status`, read-only) is the copyable step; the push is a confirm step with no command
        self.assertEqual([(s["command"], s["confirm"]) for s in stale["steps"]],
                         [("bin/agent-env status", False), (None, True),
                          ("bin/agent-env status --json --all --fetch", False)])
        self.assertIn("bin/agent-env status --commit --push", stale["steps"][1]["text_zh"])
        self.assertRegex(stale["steps"][0]["text_zh"], r"^在 \S+ 上")
        for i in self.found:
            for s in i["steps"]:
                self.assertNotRegex(s["command"] or "", r"status\b.*--push", i["id"])
        fail = by_cat(self.found, "eval")[0]
        self.assertIn("对外发送前先问你", fail["title_zh"])
        self.assertEqual(fail["ref"]["case"], "external-send-asks")

    def test_fresh_branch_not_stale(self):
        fresh = env_status("wsl", [], source="branch", generated_at="2026-11-05T00:00:00Z")
        self.assertEqual(build([fresh]), [])


class ActorTableTests(unittest.TestCase):
    def test_table(self):
        cases = [("env_unreadable", "branch", "other_env", None), ("env_unreadable", "remote_login", "user", None),
                 ("env_unreadable", "remote", "notice", None), ("env_stale", "branch", "other_env", None),
                 ("habit", "delivered", "user", "fix"), ("habit", "generated", "ai", "fix"),
                 ("habit", "mcp", "ai", "fix"), ("habit", "link", "ai", "fix"), ("habit", "contains", "ai", "fix"),
                 ("eval", "*", "ai", "logs"), ("eval_error", "*", "notice", None),
                 ("host_version", "*", "ai", "fix"),
                 # what only the user can finish stays theirs; AI prepares it
                 ("habit", "service", "user", "prepare"), ("habit", "secrets", "user", "prepare"),
                 ("habit", "token", "user", "prepare"), ("habit", "portal-service", "user", "prepare")]
        for cat, src, actor, task in cases:
            self.assertEqual(issues.actor_of(cat, src), (actor, task), (cat, src))

    def test_habit_sources_from_real_cells(self):
        found = {i["id"]: i for i in build([mac(), remote_ok()])}
        self.assertEqual(found["grok-bot/bot:rules:云端规则"]["actor"], "user")
        self.assertEqual(found["mac/hermes:rules:AGENTS.md"]["actor"], "ai")
        self.assertEqual(found["mac/codex:mcp:kimi-cu"]["actor"], "ai")
        self.assertEqual(found["mac/codex:rules:AGENTS.md"]["actor"], "ai")
        self.assertEqual(found["mac/claude-code:rules:AGENTS.md"]["actor"], "ai")
        remote = found["ecs-box/hermes:rules:AGENTS.md"]
        self.assertEqual(remote["actor"], "user")  # remote generated = remote write, user confirms
        self.assertTrue(any(s["confirm"] for s in remote["steps"]))

    def test_habit_source_reads_expect_kind(self):
        t = {"id": "mac/codex", "env": "mac"}
        # an unnamed contains cell whose fix happens to start like a link fix
        c = cell("rules", "settings.json", "drift", "ln -s is mentioned here")
        self.assertEqual(issues.habit_source(MANIFEST, t, c), "link")  # old report: guessed from the fix text
        self.assertEqual(issues.habit_source(MANIFEST, t, dict(c, expect_kind="contains")), "contains")
        self.assertEqual(issues.habit_source(MANIFEST, t, dict(c, expect_kind="broken-links")), "broken-links")
        remote = {"id": "ecs-box/hermes", "env": "ecs-box"}  # generated on a remote-exec host is a delivery
        self.assertEqual(issues.habit_source(MANIFEST, remote, dict(cell("rules", "x", "drift", ""),
                                                                    expect_kind="generated")), "delivered")
        found = build([env_status("mac", [target("mac/codex", "codex", "mac", [dict(c, expect_kind="contains")])])])
        self.assertEqual(found[0]["title_zh"], "Codex 的 settings.json 少了一项必要配置")

    def test_unreadable_sources(self):
        no_branch = build([], [{"env": "wsl", "message": "读不到 status/wsl 分支", "fix": "在 wsl 跑"}])[0]
        self.assertEqual(no_branch["actor"], "other_env")
        timeout = build([env_status("ecs-box", [], error={"message": "远程执行超时", "fix": "重跑"})])[0]
        self.assertEqual(timeout["actor"], "notice")

    def test_env_error_not_duplicated_from_errors(self):
        e = env_status("ecs-box", [], error={"message": "远程执行超时", "fix": "重跑"})
        found = build([e], [{"env": "ecs-box", "message": "远程执行超时", "fix": "重跑"}])
        self.assertEqual(len(found), 1)


PLUGIN_MANIFEST = {
    "environments": {"mac": {"platform": "darwin"}, "grok-bot": {"transport": "cloud-hosted", "driven_from": "mac"}},
    "host_names": {"grok-bot": "Grok Bot"},
    "plugins": {"pub": {"visibility": "public"}},
    "targets": [{"id": "grok-bot/bot", "host": "grok-bot", "env": "grok-bot", "plugins": ["pub"],
                 "expect": [{"kind": "delivered", "habit": "rules", "name": "指路规则"}]}],
}


class AiCarriesItThroughTests(unittest.TestCase):
    """A host upgrade and an undelivered cloud rule are handed to AI: its steps say what AI does itself, where
    it stops for the user, and which step is the write the user confirms."""

    def test_host_upgrade_is_an_ai_fix_that_checks_then_records_on_confirm(self):
        hv = {i["id"]: i for i in build([mac()])}["mac/claude-code:host_version:claude-code"]
        self.assertEqual((hv["actor"], hv["ai_task"]), ("ai", "fix"))
        steps = hv["steps"]
        self.assertEqual([(s["who"], s["confirm"]) for s in steps],
                         [("ai", False), ("ai", False), ("ai", False), ("ai", True), ("ai", False)])
        for word in ("联网查", "1.0 到 1.1", "发布说明和官方文档", "一手来源", "规则文件和加载顺序"):
            self.assertIn(word, steps[0]["text_zh"])
        self.assertIn("在 Mac 上实测清单里 mac/claude-code 登记的加载方式", steps[1]["text_zh"])
        self.assertEqual(steps[1]["command"], "bin/agent-env doctor")
        self.assertRegex(steps[2]["text_zh"], r"^有变化就停在这一步：报告.*不改登记的版本")
        # the only write: the recorded version, shown to the user first, never a copyable command
        self.assertIn("manifest/habits.toml 里 mac/claude-code 的 host_version.recorded 改成 1.1", steps[3]["text_zh"])
        self.assertIsNone(steps[3]["command"])
        self.assertEqual(steps[4]["command"], "bin/agent-env doctor")

    def test_host_upgrade_on_a_remote_machine_is_checked_there(self):
        remote = env_status("ecs-box", [target("ecs-box/hermes", "hermes", "ecs-box", [],
                                               hv={"recorded": "0.23", "current": "0.24", "changed": True})])
        steps = build([remote])[0]["steps"]
        self.assertIn("在云主机上实测", steps[1]["text_zh"])
        self.assertEqual([steps[1]["command"], steps[4]["command"]], ["bin/agent-env doctor --env ecs-box"] * 2)

    def test_cloud_delivery_the_user_only_pastes(self):
        paste = {i["id"]: i for i in build([mac()])}["grok-bot/bot:rules:云端规则"]
        self.assertEqual((paste["actor"], paste["ai_task"]), ("user", "fix"))
        self.assertEqual([(s["who"], s["command"], s["confirm"]) for s in paste["steps"]],
                         [("ai", "bin/agent-env deliver --target grok-bot/bot", False), ("user", None, False),
                          ("ai", "bin/agent-env deliver --target grok-bot/bot --record", True),
                          ("ai", "bin/agent-env doctor", False)])
        self.assertIn("复制到剪贴板", paste["steps"][0]["text_zh"])
        self.assertIn("再告诉 AI「已粘贴」", paste["steps"][1]["text_zh"])

    def pointer(self, manifest):
        c = dict(cell("rules", "指路规则", "missing", "bin/agent-env plugin pointer --target grok-bot/bot ..."),
                 expect_kind="delivered")
        status = env_status("grok-bot", [target("grok-bot/bot", "grok-bot", "grok-bot", [c])])
        return issues.build([status], [], manifest, NOW)[0]

    def test_pointer_rule_ai_builds_shows_waits_for_the_paste_then_records(self):
        ready = dict(PLUGIN_MANIFEST, plugins={"pub": {"visibility": "public", "repo": "https://example.com/pub"}})
        found = self.pointer(ready)
        self.assertEqual((found["actor"], found["ai_task"]), ("user", "fix"))
        self.assertEqual([(s["who"], s["command"], s["confirm"]) for s in found["steps"]],
                         [("ai", "bin/agent-env plugin build --all", False),
                          ("ai", "bin/agent-env plugin pointer --target grok-bot/bot", False),
                          ("user", None, False),
                          ("ai", "bin/agent-env plugin pointer --target grok-bot/bot --record", True),
                          ("ai", "bin/agent-env doctor --env grok-bot", False)])
        self.assertEqual(sum(s["who"] == "user" for s in found["steps"]), 1, "the paste is the user's only step")
        self.assertIn("在 Grok Bot 里粘贴发送", found["steps"][2]["text_zh"])

    def test_pointer_rule_stops_first_when_a_plugin_has_no_repository(self):
        steps = self.pointer(PLUGIN_MANIFEST)["steps"]
        self.assertEqual([s["who"] for s in steps], ["ai", "user", "ai", "ai", "user", "ai", "ai"])
        self.assertRegex(steps[0]["text_zh"], r"^先查前置条件：插件 pub 在清单里还没写 repo.*停在这一步.*不往下做$")
        self.assertIn("先建好插件仓 pub", steps[1]["text_zh"])
        self.assertEqual([steps[0]["command"], steps[1]["command"]], [None, None])


class OrderMergeCountTests(unittest.TestCase):
    def setUp(self):
        missing_env = [{"env": "wsl", "message": "读不到 status/wsl 分支", "fix": "x"}]
        self.envs = [mac(), remote_ok()]
        self.found = build(self.envs, missing_env)

    def test_order(self):
        rank = [issues.ORDER[(i["category"], i["state"])] for i in self.found]
        self.assertEqual(rank, sorted(rank))
        self.assertEqual(self.found[0]["category"], "env_unreadable")
        self.assertEqual(self.found[-1]["category"], "eval_error")

    def test_eval_errors_merged(self):
        errs = by_cat(self.found, "eval_error")
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["ref"]["case"], "no-commit-after-fix,external-send-asks")
        self.assertIn("2 项", errs[0]["title_zh"])

    def test_ids_unique_and_stable(self):
        ids = [i["id"] for i in self.found]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [i["id"] for i in build(self.envs, [{"env": "wsl", "message": "m", "fix": "x"}])])

    def test_counts_sum_per_env(self):
        per_env = {}
        for i in self.found:
            per_env[i["env"]] = per_env.get(i["env"], 0) + 1
        self.assertEqual(sum(per_env.values()), len(self.found))
        self.assertEqual(per_env, {"wsl": 1, "mac": 8, "ecs-box": 1})

    def test_quiet_states_make_no_issue(self):
        quiet = env_status("mac", [target("mac/codex", "codex", "mac",
                                          [cell("rules", n, s, "") for n, s in
                                           (("a", "synced"), ("b", "customized"), ("c", "unknown"))])],
                           results=[ev("mac/codex", "x", "pass"), ev("mac/codex", "y", "skip")])
        self.assertEqual(build([quiet]), [])


class WordingTests(unittest.TestCase):
    def setUp(self):
        self.found = build([mac(), remote_ok()], [{"env": "wsl", "message": "m", "fix": "x"}])

    def test_no_kebab_case_in_titles(self):
        for i in self.found:
            self.assertIsNone(KEBAB.search(i["title_zh"]), i["title_zh"])
            self.assertIsNone(KEBAB.search(i["impact_zh"]), i["impact_zh"])

    def test_display_names(self):
        titles = " ".join(i["title_zh"] for i in self.found)
        self.assertIn("小 helper 的", titles)
        self.assertIn("Grok Bot 的云端规则还没投递", titles)
        self.assertEqual(issues.host_name(MANIFEST, "wsl/claude-code"), "WSL 上的 Claude Code")

    def test_yes_never_in_command(self):
        for i in self.found:
            for s in i["steps"]:
                if s["command"] is not None:
                    self.assertNotRegex(s["command"], r"(^|\s)--yes(\s|$)", i["id"])
        self.assertIsNone(issues.step("ai", "x", "bin/agent-env deliver --target a --yes")["command"])

    def test_glossary_covers_all_keys(self):
        g = issues.glossary()
        need = {"synced", "customized", "drift", "missing", "unknown", "rules", "skills", "hooks", "mcp",
                "ai", "user", "other_env", "notice", "pass", "fail", "err", "skip",
                "safety", "env", "role", "host"}
        self.assertTrue(need <= set(g), need - set(g))
        for v in g.values():
            self.assertTrue(v["label_zh"] and v["explain_zh"])

    def test_case_zh_annotation(self):
        s = issues.annotate_eval(mac(), MANIFEST)
        zh = {r["case"]: r["case_zh"] for r in s["eval"]["results"]}
        self.assertEqual(zh["external-send-asks"], "对外发送前先问你")
        self.assertEqual(zh["x"], "x")  # undefined: falls back to the raw name


class StatusCommandTests(Base):
    """status --json carries issues; --all adds top-level issues and glossary."""

    def test_single_and_all(self):
        self.write_manifest([self.target([{"kind": "link", "habit": "rules", "name": "AGENTS.md",
                                           "path": "~/host/AGENTS.md", "to": "~/src/AGENTS.md"}])])
        out = io.StringIO()
        agent_env.main(["status", "--json", "--manifest", str(self.manifest), "--repo", str(self.repo),
                        "--env", "test"], out=out, err=io.StringIO())
        single = json.loads(out.getvalue())
        self.assertEqual([i["category"] for i in single["issues"]], ["habit"])
        out = io.StringIO()
        agent_env.main(["status", "--json", "--all", "--manifest", str(self.manifest), "--repo", str(self.repo),
                        "--env", "test"], out=out, err=io.StringIO())
        a = json.loads(out.getvalue())
        self.assertIn("glossary", a)
        self.assertEqual(len(a["issues"]), sum(1 for i in a["issues"] if i["env"] in ("test", "other")))
        self.assertEqual({i["category"] for i in a["issues"]}, {"habit", "env_unreadable"})
        self.assertEqual(len(a["environments"][0]["issues"]), 1)


ORIGIN_MANIFEST = {
    "sources": {"rules": "/src/AGENTS.md", "docs": "/src/docs", "hooks": "/src/scripts", "skills": "/src/skills",
                "company_skills": "/repo/company-skills", "eval": "/src/scripts/eval.zsh"},
    "environments": {"mac": {"platform": "darwin"}, "ecs-box": {"transport": "remote-exec"}},
    "targets": [
        {"id": "mac/codex", "host": "codex", "env": "mac",
         "expect": [{"kind": "link", "habit": "rules", "name": "AGENTS.md", "path": "~/.codex/AGENTS.md",
                     "to": "/src/AGENTS.md"}]},
        {"id": "mac/claude-code", "host": "claude-code", "env": "mac",
         "expect": [{"kind": "contains", "habit": "rules", "name": "AGENTS.md", "text": "@AGENTS.md"},
                    {"kind": "portal", "habit": "skills", "registry": "~/portal.json"}]},
        {"id": "mac/cursor", "host": "cursor", "env": "mac",
         "expect": [{"kind": "contains", "habit": "hooks", "name": "global-rules", "text": "rules.sh"}]},
        {"id": "mac/hermes", "host": "hermes", "env": "mac", "render": "generated", "output": "~/.hermes.md",
         "also_output": ["~/.hermes/AGENTS.md"], "company_skills": ["metrics-query"],
         "expect": [{"kind": "generated", "habit": "rules", "name": ".hermes.md"},
                    {"kind": "company-skills", "habit": "skills"}]},
        {"id": "ecs-box/hermes", "host": "hermes", "env": "ecs-box",
         "expect": [{"kind": "login", "habit": "rules", "name": "登录"},
                    {"kind": "service", "habit": "skills", "name": "SearXNG"},
                    {"kind": "contains", "habit": "rules", "name": "卡片标题", "text": "title: x"},
                    {"kind": "broken-links", "habit": "skills", "name": "断链"},
                    {"kind": "generated", "habit": "rules", "name": "AGENTS.md"}]},
    ],
}


class OriginTests(unittest.TestCase):
    """cell_origin: which source a habit cell comes from, for the panel's relation graph."""

    def origin(self, tid, kind, name):
        env = tid.split("/")[0]
        return issues.cell_origin(ORIGIN_MANIFEST, {"id": tid, "env": env}, {"kind": kind, "name": name, "fix": ""})

    def test_shared_sources(self):
        self.assertEqual(self.origin("mac/codex", "rules", "AGENTS.md"), "rules")  # link into the rules file
        self.assertEqual(self.origin("mac/claude-code", "rules", "AGENTS.md"), "rules")  # @AGENTS.md import
        self.assertEqual(self.origin("mac/cursor", "hooks", "global-rules"), "hooks")
        self.assertEqual(self.origin("mac/hermes", "rules", ".hermes.md"), "rules")  # generated
        self.assertEqual(self.origin("mac/hermes", "rules", "AGENTS.md"), "rules")  # its also_output copy
        self.assertEqual(self.origin("ecs-box/hermes", "rules", "AGENTS.md"), "rules")  # delivered remotely
        self.assertEqual(self.origin("mac/hermes", "skills", "metrics-query"), "company_skills")

    def test_portal_and_manifest(self):
        self.assertEqual(self.origin("mac/claude-code", "skills", "门户·Editors"), "portal")
        self.assertEqual(self.origin("mac/claude-code", "skills", "门户·服务"), "portal")
        self.assertEqual(self.origin("mac/codex", "mcp", "kimi-cu"), "manifest")
        self.assertEqual(self.origin("mac/hermes", "skills", "skills.disabled"), "manifest")

    def test_portal_cells_map_to_their_part(self):
        """expect_kind portal is narrowed by name; old reports (no expect_kind) get the same answer from the
        manifest; a portal cell whose expectation left the manifest falls back to the name's shape."""
        t = {"id": "mac/claude-code", "env": "mac"}
        parts = {"门户": "portal-registry", "门户·服务": "portal-service", "门户·Editors": "portal"}
        for name, part in parts.items():
            c = {"kind": "skills", "name": name, "fix": ""}
            self.assertEqual(issues.habit_source(ORIGIN_MANIFEST, t, dict(c, expect_kind="portal")), part, name)
            self.assertEqual(issues.habit_source(ORIGIN_MANIFEST, t, c), part, f"old report: {name}")
            gone = {"id": "mac/codex", "env": "mac"}
            self.assertEqual(issues.habit_source(ORIGIN_MANIFEST, gone, dict(c, expect_kind="portal")), part, name)

    def test_facts_of_the_machine_itself_have_no_source(self):
        for name, kind in (("登录", "rules"), ("SearXNG", "skills"), ("卡片标题", "rules"), ("断链", "skills")):
            self.assertIsNone(self.origin("ecs-box/hermes", kind, name), name)
        self.assertIsNone(self.origin("mac/accounts", "rules", "账号目录（sideby）"), "a target the manifest does not list")


class RelationsTests(Base):
    """status --json --all carries `relations`: sources, environment names, declared agents, cell origins."""

    def test_relations_in_all_status(self):
        self.write_manifest([self.target([{"kind": "link", "habit": "rules", "name": "AGENTS.md",
                                           "path": "~/host/AGENTS.md", "to": str(self.root / "AGENTS.md")}]),
                             self.target([], id="other/codex", host="codex", env="other", display_name="小 c")])
        live = self.doctor()
        a = agent_env.all_status(self.manifest, "test", self.repo, live=live)
        rel = a["relations"]
        self.assertEqual(list(rel["sources"])[:3], ["rules", "docs", "hooks"])
        self.assertNotIn("eval", rel["sources"])
        self.assertTrue(rel["sources"]["manifest"].endswith("manifest.toml"))
        self.assertEqual(rel["env_names"], {"test": "test", "other": "other"})
        # an environment that never reported still lists its agents, with the display name
        self.assertEqual([(t["id"], t["env"], t["name"]) for t in rel["targets"]],
                         [("test/host", "test", "generic"), ("other/codex", "other", "小 c")])
        cell = a["environments"][0]["targets"][0]["habits"][0]
        self.assertEqual(cell["origin"], "rules")
        self.assertNotIn("origin", live["targets"][0]["habits"][0], "the live status (what --commit stores) is unchanged")

    def test_portal_path_per_machine(self):
        # each machine's portal reads its own tool list; a Mac agent must not be shown the WSL list's path
        self.write_manifest([self.target([{"kind": "portal", "habit": "skills", "registry": "~/mac-portal.json"}]),
                             self.target([{"kind": "portal", "habit": "skills", "registry": "~/wsl-portal.json"}],
                                         id="other/claude-code", host="claude-code", env="other")])
        rel = agent_env.all_status(self.manifest, "test", self.repo)["relations"]
        self.assertEqual(rel["env_sources"], {"test": {"portal": "~/mac-portal.json"}, "other": {"portal": "~/wsl-portal.json"}})
        self.assertEqual(rel["sources"]["portal"], "~/mac-portal.json；~/wsl-portal.json", "the source node lists both")


class AiFixFixtureTests(unittest.TestCase):
    """panel/fixtures/v8-ai-fix.json (the rows AI carries through next to the rows only the user can finish)
    holds what the engine emits for the fixture's own environments, under the demo manifest."""

    def test_fixture_issues_are_the_engines(self):
        repo = Path(__file__).resolve().parent.parent
        with open(repo / "examples" / "demo" / "manifest" / "habits.toml", "rb") as fh:
            manifest = tomllib.load(fh)
        data = json.loads((repo / "panel" / "fixtures" / "v8-ai-fix.json").read_text(encoding="utf-8"))
        envs = [{k: v for k, v in e.items() if k != "issues"} for e in data["environments"]]
        want = issues.build(envs, data["errors"], manifest, datetime(2026, 11, 10, 9, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(data["issues"], want)
        for e in data["environments"]:
            self.assertEqual(e["issues"], [i for i in want if i["env"] == e["env"]], e["env"])
        self.assertEqual([(i["category"], i["actor"], i["ai_task"]) for i in want],
                         [("habit", "user", "fix"), ("habit", "user", "prepare"), ("habit", "user", "prepare"),
                          ("host_version", "ai", "fix"), ("host_version", "ai", "fix")])


if __name__ == "__main__":
    unittest.main()
