"""Stage 4 (docs/plugins-design.md): the rules source split into one file per owner ([sources] rules_src,
habits.join_rules_src), and the rules file of a target on one of the user's own machines composed there from
its plugins (engine/compose.py): the merge, its 已覆盖 report, the header, `render --check/--write`, the
`composed` expectation, the check, the issue steps and the plugins tab."""
import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import compose  # noqa: E402
import habits  # noqa: E402
import habits_check  # noqa: E402
import issues  # noqa: E402
import plugins  # noqa: E402
import plugins_view as pv  # noqa: E402
import render  # noqa: E402
from test_issues import NOW, cell, env_status, target  # noqa: E402
from test_issues import MANIFEST as ISSUE_MANIFEST  # noqa: E402
from test_plugins import PERSONAL, PUBLIC, PluginBase  # noqa: E402

PREAMBLE = "# Title\n\nIntro line. Details live in `docs/`.\n\n"
SHARED = "## Shared\n\nshared body，细节见 `docs/x.md`。\n\n"
CLI = "## Cli only\n\ncli body\n"
SRC_FILES = {"public.md": PREAMBLE + SHARED, "personal.md": CLI}
OMIT = [" Details live in `docs/`.", "，细节见 `docs/x.md`"]
PUB = dict(PUBLIC, omit=OMIT)
LOC = {"visibility": "local", "description": "patch layer", "rules_file": "patch.md"}
PATCH = "## Shared\n\npatch line\n\n## Own\n\nown body\n"
COMPOSED = {"kind": "composed", "habit": "rules", "name": "AGENTS.md 合成"}
TID = "test/host"


class ComposeBase(PluginBase):
    def compose_setup(self, names, specs=None, files=None, envs=None, expect=None, more_targets=(), **extra):
        self.src = self.root / "agents-md"
        self.src.mkdir(exist_ok=True)
        for f in self.src.iterdir():
            f.unlink()
        for name, text in (SRC_FILES if files is None else files).items():
            (self.src / name).write_text(text, encoding="utf-8")
        (self.root / "patch.md").write_text(PATCH, encoding="utf-8")
        t = self.target([COMPOSED] if expect is None else expect, plugins=names, rules_output="~/out/AGENTS.md", **extra)
        self.setup(specs or {"pub": PUB, "per": PERSONAL, "loc": LOC}, targets=[t, *more_targets],
                   environments=envs or {"test": {"platform": "nomatch"}})
        text = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(text.replace("[sources]\n", f'[sources]\nrules_src = "{self.src}"\n', 1), encoding="utf-8")
        self.m = habits.load_manifest(self.manifest)
        self.t = self.m["targets"][0]
        self.out = self.home / "out" / "AGENTS.md"
        return self.m

    def text(self):
        return habits.read_rules(self.m)

    def body(self):
        return compose.compose(self.m, self.t, self.text())

    def render(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        rc = render.run([str(self.manifest), "--target", TID, *flags], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def composed_cells(self):
        return [c for c in self.cells(self.doctor()) if c.get("expect_kind") == "composed"]


class JoinTests(ComposeBase):
    def test_joined_in_registry_order_byte_for_byte(self):
        self.compose_setup(["pub", "per"])
        self.assertEqual(self.text(), PREAMBLE + SHARED + CLI)
        # the file a section lives in does not decide its place: [rule_sections] does
        self.compose_setup(["pub", "per"], files={"a.md": CLI, "public.md": PREAMBLE + SHARED})
        self.assertEqual(self.text(), PREAMBLE + SHARED + CLI)

    def test_unregistered_sections_go_last_in_file_order(self):
        self.compose_setup(["pub", "per"], files={"public.md": PREAMBLE + SHARED, "personal.md": "## Zed\n\nz\n" + CLI})
        self.assertEqual(self.text(), PREAMBLE + SHARED + CLI + "## Zed\n\nz\n")

    def test_a_section_without_a_trailing_newline_still_ends_its_line(self):
        self.compose_setup(["pub", "per"], files={"public.md": PREAMBLE + SHARED, "personal.md": CLI.rstrip("\n")})
        self.assertEqual(self.text(), PREAMBLE + SHARED + CLI)

    def test_nothing_is_joined_when_a_file_fails(self):
        cases = {
            "两次": {"public.md": PREAMBLE + SHARED, "personal.md": CLI + SHARED},
            "说明区只能有一份": {"public.md": PREAMBLE + SHARED, "personal.md": "stray text\n\n" + CLI},
            "没有闭合": {"public.md": PREAMBLE + SHARED, "personal.md": CLI + "```\nopen\n"},
            "没有 .md 文件": {},
        }
        for what, files in cases.items():
            with self.subTest(what):
                self.compose_setup(["pub", "per"], files=files)
                text, _, errs = habits.join_rules_src(self.m)
                self.assertIsNone(text)
                self.assertTrue(any(what in msg for msg, _ in errs), errs)
                self.assertIsNone(habits.read_rules(self.m))

    def test_missing_directory_and_plain_rules_file(self):
        self.compose_setup(["pub", "per"])
        m = dict(self.m, sources=dict(self.m["sources"], rules_src=str(self.root / "nope")))
        self.assertIn("规则源目录不存在", habits.join_rules_src(m)[2][0][0])
        m["sources"].pop("rules_src")
        self.assertEqual(habits.read_rules(m), self.rules(), "without rules_src the [sources] rules file is read")


class MergeTests(ComposeBase):
    def test_without_a_patch_layer_omit_cuts_as_in_the_published_plugin(self):
        self.compose_setup(["pub", "per"])
        body, report = self.body()
        self.assertEqual(body, "# Title\n\nIntro line.\n\n## Shared\n\nshared body。\n\n" + CLI)
        self.assertEqual(report, [])

    def test_patch_layer_keeps_omit_spans_appends_and_adds_its_own_section(self):
        self.compose_setup(["pub", "per", "loc"])
        body, report = self.body()
        self.assertEqual(body, PREAMBLE + SHARED.rstrip("\n") + "\n\npatch line\n\n" + CLI + "\n## Own\n\nown body\n")
        self.assertEqual(report, [
            {"what": "kept", "section": habits.PREAMBLE_KEY, "plugin": "pub", "by": "loc", "items": [1]},
            {"what": "kept", "section": "Shared", "plugin": "pub", "by": "loc", "items": [2]},
            {"what": "appended", "section": "Shared", "plugin": "pub", "by": "loc"}])

    def test_a_section_taken_again_is_carried_or_replaced(self):
        again = {"visibility": "public", "description": "again", "sections": ["Shared"]}
        self.compose_setup(["pub", "again", "per"], specs={"pub": PUB, "again": again, "per": PERSONAL, "loc": LOC})
        body, report = self.body()
        self.assertEqual(report, [{"what": "replaced", "section": "Shared", "plugin": "pub", "by": "again"}])
        self.assertIn(SHARED, body, "the later plugin's text (no omit) replaces the cut one, in the same place")
        self.assertLess(body.index("## Shared"), body.index("## Cli only"))
        self.compose_setup(["pub", "again", "loc"], specs={"pub": PUB, "again": again, "per": PERSONAL, "loc": LOC})
        self.assertIn({"what": "carried", "section": "Shared", "plugin": "pub", "by": "again"}, self.body()[1])

    def test_same_inputs_same_bytes(self):
        self.compose_setup(["pub", "per", "loc"])
        first = compose.render_text(self.m, self.t, self.text())
        self.assertEqual(compose.render_text(habits.load_manifest(self.manifest), self.t, self.text()), first)

    def test_header_says_where_to_edit_and_how_to_write_again(self):
        self.compose_setup(["pub", "per", "loc"])
        head, blank, _ = compose.render_text(self.m, self.t, self.text()).split("\n", 2)
        self.assertEqual(blank, "")
        self.assertTrue(head.startswith("<!-- 生成文件，不要手改。规则源：") and head.endswith(" -->"), head)
        self.assertIn("/agents-md/ 的 public.md（插件 pub）、personal.md（per）；另有 ", head)
        self.assertIn("patch.md（loc）", head)
        self.assertIn("改完在 agent-env 仓库运行 bin/agent-env render --target test/host --write", head)
        self.compose_setup(["pub", "per", "loc"], envs={"test": {"platform": "nomatch", "sync_command": "env-sync run"}})
        head = compose.header(self.m, self.t, "~/.claude/agents-md")
        self.assertIn("规则源：~/.claude/agents-md/ 的 ", head)
        self.assertIn("在写入方改好并推送，这里同步时（env-sync run）重新生成", head)
        self.assertEqual(compose.regen_command(self.m, self.t), "env-sync run")

    def test_the_clone_route_of_wsl_assembler(self):
        self.compose_setup(["pub", "per", "loc"])
        self.assertIs(compose.target_for(self.m, "test", self.out), self.t)
        self.assertIsNone(compose.target_for(self.m, "other", self.out))
        self.assertIsNone(compose.target_for(self.m, "test", self.home / "elsewhere.md"))
        clone = self.root / "clone"
        clone.mkdir()
        (clone / "public.md").write_text(PREAMBLE + "## Shared\n\nfrom the clone\n\n", encoding="utf-8")
        (clone / "personal.md").write_text(CLI, encoding="utf-8")
        text = compose.render_from(self.m, self.t, clone, "~/.claude/agents-md")
        self.assertIn("from the clone", text)
        self.assertIn("规则源：~/.claude/agents-md/ 的 ", text.split("\n", 1)[0])
        (clone / "personal.md").write_text(CLI + "## Shared\n\nx\n", encoding="utf-8")
        with self.assertRaisesRegex(compose.ComposeError, "两次.*修复："):
            compose.render_from(self.m, self.t, clone, "~/x")

    def test_target_errors(self):
        self.compose_setup(["pub", "per"])
        t = dict(self.t)
        self.assertEqual(compose.target_errors(self.m, t, {}), [])
        self.assertIn("不能在本机合成", compose.target_errors(self.m, t, {"transport": "remote-exec"})[0][0])
        self.assertIn("同时写了 rules_output 和 pull", compose.target_errors(self.m, dict(t, pull={}), {})[0][0])
        self.assertIn("不是 ~/ 开头", compose.target_errors(self.m, dict(t, rules_output="/abs/x.md"), {})[0][0])
        self.assertIn("有 rules_output 但没有 plugins",
                      plugins.target_errors(self.m, {"id": "x", "rules_output": "~/a.md"})[0][0])
        twin = self.target([], id="test/twin", plugins=["pub"], rules_output="~/out/AGENTS.md")
        self.compose_setup(["pub", "per"], more_targets=[twin])
        self.assertIn("都往 ~/out/AGENTS.md 合成规则", compose.target_errors(self.m, self.t, {})[0][0])


class RenderTests(ComposeBase):
    def test_check_write_and_up_to_date(self):
        self.compose_setup(["pub", "per", "loc"])
        rc, _, err = self.render("--check")
        self.assertEqual(rc, 1)
        self.assertIn("不存在。修复：运行 bin/agent-env render --target test/host --write", err)
        rc, out, _ = self.render("--write")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), compose.render_text(self.m, self.t, self.text()))
        self.assertEqual(self.render("--check")[0], 0)
        self.assertIn("已是最新", self.render("--write")[1])
        self.out.write_text(self.out.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
        rc, _, err = self.render("--check")
        self.assertEqual(rc, 1)
        self.assertIn("手改的内容先搬进规则源", err)
        rc, out, _ = self.render()
        self.assertEqual((rc, out), (0, compose.render_text(self.m, self.t, self.text())), "no flag prints the file")


class ExpectTests(ComposeBase):
    def test_missing_then_customized_with_one_override_row_per_section(self):
        self.compose_setup(["pub", "per", "loc"])
        [c] = self.composed_cells()
        self.assertEqual((c["state"], c["fix"]), ("missing", "bin/agent-env render --target test/host --write"))
        self.render("--write")
        cells = self.composed_cells()
        self.assertEqual([(c["name"], c["state"]) for c in cells],
                         [("AGENTS.md 合成", "customized"), ("已覆盖：章节「说明区」", "customized"),
                          ("已覆盖：章节「Shared」", "customized")])
        self.assertIn("与插件 pub → per → loc 合成的结果一致（含补丁层 loc），哈希 ", cells[0]["detail"])
        self.assertIn("omit 第 1 项", cells[1]["detail"])
        self.assertIn("omit 第 2 项", cells[2]["detail"])
        self.assertIn("；补丁层 loc 往插件 pub 的这一章追加了自己的内容", cells[2]["detail"])

    def test_synced_without_a_patch_layer(self):
        self.compose_setup(["pub", "per"])
        self.render("--write")
        [c] = self.composed_cells()
        self.assertEqual(c["state"], "synced")

    def test_drift_and_symlink(self):
        self.compose_setup(["pub", "per", "loc"])
        self.render("--write")
        self.out.write_text("hand written\n", encoding="utf-8")
        c = self.composed_cells()[0]
        self.assertEqual(c["state"], "drift")
        self.assertIn("手改的内容先搬进规则源", c["fix"])
        self.assertTrue(c["fix"].endswith("再运行 bin/agent-env render --target test/host --write"), c["fix"])
        real = self.home / "real.md"
        real.write_text("x\n", encoding="utf-8")
        self.out.unlink()
        self.out.symlink_to(real)
        c = self.composed_cells()[0]
        self.assertEqual(c["state"], "drift")
        self.assertIn("是符号链接", c["detail"])

    def test_plugins_kind_leaves_the_override_rows_to_composed(self):
        exp = [COMPOSED, {"kind": "plugins", "habit": "rules"}]
        again = {"visibility": "public", "description": "again", "sections": ["Shared"]}
        self.compose_setup(["pub", "again", "loc"], specs={"pub": PUB, "again": again, "per": PERSONAL, "loc": LOC},
                           expect=exp)
        rows = [c for c in self.cells(self.doctor()) if c.get("expect_kind") == "plugins"]
        self.assertEqual([c["name"] for c in rows], ["插件 pub（公开）", "插件 again（公开）", "插件 loc（本机）"])


class CheckTests(ComposeBase):
    def results(self):
        return habits_check.run_checks(self.manifest).results

    def test_valid_setup_passes(self):
        self.compose_setup(["pub", "per", "loc"])
        self.assertEqual(self.fails(), [])
        passed = [r["message"] for r in self.results() if r["level"] == "PASS"]
        self.assertTrue(any("2 个文件，3 个章节" in p for p in passed), passed)
        self.assertTrue(any("本机合成到 ~/out/AGENTS.md" in p for p in passed), passed)

    def test_public_file_holds_exactly_the_public_sections(self):
        self.compose_setup(["pub", "per"], files={"public.md": PREAMBLE + SHARED + CLI})
        self.assertTrue(any("章节「Cli only」 写在 public.md，却登记为 private" in f for f in self.fails()), self.fails())
        self.compose_setup(["pub", "per"], files={"public.md": PREAMBLE, "personal.md": SHARED + CLI})
        self.assertTrue(any("章节「Shared」 登记为 public，却写在 personal.md" in f for f in self.fails()), self.fails())

    def test_join_errors_and_the_size_limit_fail(self):
        self.compose_setup(["pub", "per"], files={"public.md": PREAMBLE + SHARED, "personal.md": CLI + SHARED})
        self.assertTrue(any("出现了两次" in f for f in self.fails()), self.fails())
        self.compose_setup(["pub", "per"], rule_limit_bytes=10)
        self.assertTrue(any("合成后的规则" in f and "超过上限 10" in f for f in self.fails()), self.fails())

    def test_a_rules_src_file_cannot_be_a_pushed_plugins_rules_file(self):
        bad = dict(PERSONAL, rules_file="agents-md/personal.md")
        self.compose_setup(["pub", "bad"], specs={"pub": PUB, "bad": bad, "per": PERSONAL, "loc": LOC})
        self.assertTrue(any("就是规则源本身" in f for f in self.fails()), self.fails())


class IssueTests(unittest.TestCase):
    def found(self, env, tid, fix, manifest=ISSUE_MANIFEST):
        c = dict(cell("rules", "AGENTS.md 合成", "drift", fix), expect_kind="composed")
        status = env_status(env, [target(tid, "claude-code", env, [c])])
        return issues.build([status], [], manifest, NOW)[0]

    def test_mac_writes_it_again_from_the_source(self):
        found = self.found("mac", "mac/claude-code", "手改的内容先搬进规则源 ~/.claude/agents-md，"
                                                     "再运行 bin/agent-env render --target mac/claude-code --write")
        self.assertEqual((found["actor"], found["ai_task"]), ("ai", "fix"))
        self.assertEqual([s["command"] for s in found["steps"]],
                         ["bin/agent-env render --target mac/claude-code --check",
                          "bin/agent-env render --target mac/claude-code --write", "bin/agent-env doctor"])
        self.assertIn("和插件合成的不一致", found["title_zh"])

    def test_an_environment_with_its_own_sync_syncs_again(self):
        envs = dict(ISSUE_MANIFEST["environments"], wsl={"platform": "linux", "sync_command": "env-sync run"})
        found = self.found("wsl", "wsl/claude-code", "手改的内容先搬进规则源 ~/.claude/agents-md，再运行 env-sync run",
                           dict(ISSUE_MANIFEST, environments=envs))
        self.assertEqual([s["command"] for s in found["steps"]][0], "env-sync run")
        self.assertEqual(self.found("wsl", "wsl/claude-code", "x")["steps"][0]["command"],
                         "bin/agent-env render --target wsl/claude-code --check", "no sync_command: render again")


class PluginsViewTests(ComposeBase):
    def test_own_machine_needs_no_credential_and_shows_a_stale_file(self):
        self.compose_setup(["pub", "per", "loc"])
        [row, _] = pv.environments(self.m, [], plugins.table(self.m))
        self.assertEqual(row["credential"]["state"], "none")
        self.assertIn("本机从规则源直接合成", row["credential"]["detail"])
        self.assertEqual(row["notes"], [])
        status = self.doctor()
        [row, _] = pv.environments(self.m, [status], plugins.table(self.m))
        self.assertEqual([n["code"] for n in row["notes"]], ["compose_missing"])
        self.assertEqual(row["notes"][0]["fix"], "bin/agent-env render --target test/host --write")


class DemoManifestTests(unittest.TestCase):
    """The example this repository ships: its Mac and WSL compose ~/.claude/AGENTS.md from the public, personal
    and their own local plugin, and the whole manifest passes `check`."""

    DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo" / "manifest" / "habits.toml"

    def test_demo_passes_check(self):
        rep = habits_check.run_checks(self.DEMO)
        self.assertEqual([r["message"] for r in rep.results if r["level"] == "FAIL"], [])

    def test_mac_and_wsl_compose_the_global_rules(self):
        m = habits.load_manifest(self.DEMO)
        table = plugins.table(m)
        composed = {t["id"]: t for t in m["targets"] if compose.is_composed(t)}
        self.assertEqual(sorted(composed), ["mac/claude-code", "wsl/claude-code"])
        for tid, t in composed.items():
            self.assertEqual(t["rules_output"], "~/.claude/AGENTS.md")
            names = t["plugins"]
            self.assertEqual([plugins.visibility(table[n]) for n in names], ["public", "personal", "local"], tid)
            self.assertTrue(any(e.get("kind") == "composed" for e in t["expect"]), tid)
        self.assertEqual(Path(m["sources"]["rules_src"]).resolve(), (self.DEMO.parent.parent / "sources" / "agents-md").resolve())


if __name__ == "__main__":
    unittest.main()
