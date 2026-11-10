import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import habits  # noqa: E402
import habits_check  # noqa: E402
import render  # noqa: E402
from test_habits import RULES, build_fixture  # noqa: E402

PROFILE = "# Bot profile\n\n## Bot only\n\nbot-specific line\n"


def bot_target(**extra):
    t = {"id": "bot", "roles": ["bot"], "rule_limit_bytes": 0, "verified": True,
         "render": "generated", "supplement": "profile.md", "output": "~/bot/AGENTS.md"}
    t.update(extra)
    return t


class RenderBase(unittest.TestCase):
    """Fixture in a tempdir; HOME and XDG_STATE_HOME point inside it."""

    def make(self, target=None, profile=PROFILE, rules=RULES):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        if profile is not None:
            (self.root / "profile.md").write_text(profile, encoding="utf-8")
        self.path = build_fixture(self.root, rules=rules, targets=[target or bot_target()])
        self.m = habits.load_manifest(self.path)
        self.out = self.home / "bot" / "AGENTS.md"

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = render.run([str(self.path), "--target", "bot", *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class RenderRulesTests(RenderBase):
    def test_profile_first_then_note_then_selected_sections(self):
        self.make()
        text = render.render_rules(self.m, RULES, "bot")
        self.assertTrue(text.startswith(PROFILE))
        note = text.index("<!-- 本文件由 agent-env 生成")
        self.assertLess(text.index("bot-specific line"), note)
        self.assertLess(note, text.index("Intro line."))
        # roles = ["bot"]: preamble, Alpha, Gamma; not Beta (cli only)
        self.assertIn("alpha body", text)
        self.assertIn("gamma body", text)
        self.assertNotIn("beta body", text)
        self.assertIn("--target bot --write", text)

    def test_output_is_stable(self):
        self.make()
        a = render.render_rules(self.m, RULES, "bot")
        b = render.render_rules(habits.load_manifest(self.path), RULES, "bot")
        self.assertEqual(a, b)
        self.assertNotRegex(a, r"\d{4}-\d{2}-\d{2}T|\d{8}T\d{6}")

    def test_note_uses_tilde_for_home_paths(self):
        self.make()
        (self.home / "m").mkdir()
        moved = self.home / "m" / "habits.toml"
        moved.write_text(self.path.read_text(encoding="utf-8").replace(
            '"profile.md"', f'"{self.root / "profile.md"}"'), encoding="utf-8")
        text = render.render_rules(habits.load_manifest(moved), RULES, "bot")
        self.assertIn("~/m/habits.toml", text)
        self.assertNotIn(str(self.home), text)

    def test_over_limit_fails_without_truncating(self):
        self.make(bot_target(rule_limit_bytes=50))
        with self.assertRaises(render.RenderError) as ctx:
            render.render_rules(self.m, RULES, "bot")
        self.assertIn("超过上限 50 B", str(ctx.exception))
        self.assertIn("修复：", str(ctx.exception))

    def test_unclosed_fence_refuses(self):
        self.make()
        with self.assertRaises(render.RenderError):
            render.render_rules(self.m, RULES + "\n## Late\n```\nopen\n", "bot")

    def test_target_without_supplement_is_note_plus_sections(self):
        t = bot_target()
        del t["supplement"]
        self.make(t, profile=None)
        text = render.render_rules(self.m, RULES, "bot")
        self.assertTrue(text.startswith("<!-- 本文件由 agent-env 生成"))


class CliTests(RenderBase):
    def test_stdout_default(self):
        self.make()
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(out, render.render_rules(self.m, RULES, "bot"))
        self.assertFalse(self.out.exists())

    def test_check_missing_then_write_then_check_passes(self):
        self.make()
        code, _, err = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("不存在", err)
        code, out, _ = self.cli("--write")
        self.assertEqual(code, 0)
        self.assertIn("无快照", out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), render.render_rules(self.m, RULES, "bot"))
        code, out, _ = self.cli("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_check_detects_drift_with_diff(self):
        self.make()
        self.cli("--write")
        self.out.write_text(self.out.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
        code, _, err = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("漂移", err)
        self.assertIn("-hand edit", err)
        self.assertIn("修复：", err)

    def test_write_snapshots_old_file_and_replaces_atomically(self):
        self.make()
        self.out.parent.mkdir(parents=True)
        self.out.write_text("old content\n", encoding="utf-8")
        os.chmod(self.out, 0o600)
        with mock.patch("render.os.replace", wraps=os.replace) as rep:
            code, out, _ = self.cli("--write")
        self.assertEqual(code, 0)
        src, dst = rep.call_args.args
        self.assertEqual(Path(dst), self.out)
        self.assertEqual(Path(src).parent, self.out.parent)  # temp file in the same dir
        snaps = list((self.root / "state" / "agent-env" / "snapshots").glob("*/bot/AGENTS.md"))
        self.assertEqual(len(snaps), 1)
        self.assertRegex(snaps[0].parts[-3], r"^\d{8}T\d{6}Z$")
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "old content\n")
        self.assertEqual(os.stat(self.out).st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ["AGENTS.md"])

    def test_failed_write_keeps_old_file_and_no_temp(self):
        self.make()
        self.out.parent.mkdir(parents=True)
        self.out.write_text("old content\n", encoding="utf-8")
        with mock.patch("render.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.cli("--write")
        self.assertEqual(self.out.read_text(encoding="utf-8"), "old content\n")
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ["AGENTS.md"])

    def test_second_write_is_noop(self):
        self.make()
        self.cli("--write")
        code, out, _ = self.cli("--write")
        self.assertEqual(code, 0)
        self.assertIn("未写入", out)
        self.assertFalse((self.root / "state" / "agent-env" / "snapshots").exists())

    def test_write_refused_without_render_generated(self):
        t = bot_target()
        del t["render"]
        self.make(t)
        code, _, err = self.cli("--write")
        self.assertEqual(code, 1)
        self.assertIn("render", err)
        self.assertFalse(self.out.exists())

    def test_over_limit_write_fails_and_keeps_file(self):
        self.make(bot_target(rule_limit_bytes=50))
        code, _, err = self.cli("--write")
        self.assertEqual(code, 1)
        self.assertIn("超过上限", err)
        self.assertFalse(self.out.exists())

    def test_unknown_target(self):
        self.make()
        out, err = io.StringIO(), io.StringIO()
        code = render.run([str(self.path), "--target", "nope"], out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn("修复：", err.getvalue())


class SkillsDisabledTests(RenderBase):
    def test_parse_variants(self):
        f = render.read_skills_disabled
        self.assertEqual(f("model: x\nskills:\n  external_dirs:\n    - ~/a\n  disabled:\n    - one\n    - 'two'\napprovals:\n  disabled:\n    - nope\n"),
                         ["one", "two"])
        self.assertEqual(f("skills:\n  disabled: []\n"), [])
        self.assertEqual(f("skills:\n  disabled: [a, \"b\"]\n"), ["a", "b"])
        self.assertEqual(f("skills:\n  disabled:\n  - a\n  other: 1\n"), ["a"])
        self.assertEqual(f("model: x\n"), [])
        self.assertEqual(f("skills:\n  external_dirs: []\n"), [])
        self.assertIsNone(f("skills: {disabled: [a]}\n"))
        self.assertIsNone(f("skills:\n  disabled: &x\n    - a\n"))
        self.assertIsNone(f("skills:\n  disabled:\n    - {a: 1}\n"))
        self.assertIsNone(f("skills:\n  disabled:\n    - a\n    b\n"))

    def _cfg(self, text, expected):
        self.make(bot_target(skills_config="~/config.yaml", skills_disabled=expected))
        (self.home / "config.yaml").write_text(text, encoding="utf-8")
        self.cli("--write")
        return self.cli("--check")

    def test_check_skills_match(self):
        code, out, _ = self._cfg("skills:\n  disabled:\n    - a\n", ["a"])
        self.assertEqual(code, 0)
        self.assertIn("skills.disabled 与清单一致", out)

    def test_check_skills_mismatch_fails(self):
        code, _, err = self._cfg("skills:\n  disabled:\n    - a\n    - b\n", ["a"])
        self.assertEqual(code, 1)
        self.assertIn("配置多出 ['b']", err)

    def test_check_skills_unparseable_is_unverified(self):
        code, _, err = self._cfg("skills: {disabled: [a]}\n", ["a"])
        self.assertEqual(code, 0)
        self.assertIn("UNVERIFIED", err)


class LimitUnitTests(RenderBase):
    CJK_PROFILE = "# 档案\n\n" + "中" * 100 + "\n"

    def test_measure(self):
        self.assertEqual(habits.measure("中a", "bytes"), 4)
        self.assertEqual(habits.measure("中a", "chars"), 2)
        self.assertEqual(habits.measure("😀", "chars"), 2)  # UTF-16 units, never fewer than code points

    def _sizes(self):
        self.make(profile=self.CJK_PROFILE)
        text = render.compose(self.m, RULES, "bot")
        return habits.measure(text, "chars"), habits.measure(text, "bytes")

    def test_chars_limit_passes_where_bytes_would_fail(self):
        chars, size = self._sizes()
        self.assertLess(chars, size)
        limit = (chars + size) // 2
        self.make(bot_target(rule_limit_bytes=limit, rule_limit_unit="chars"), profile=self.CJK_PROFILE)
        render.render_rules(self.m, RULES, "bot")  # no raise
        rep = habits_check.run_checks(self.path)
        self.assertTrue(rep.ok, rep.results)
        self.assertTrue(any("字符 / 上限" in r["message"] for r in rep.results))
        self.make(bot_target(rule_limit_bytes=limit), profile=self.CJK_PROFILE)  # default bytes
        with self.assertRaises(render.RenderError):
            render.render_rules(self.m, RULES, "bot")
        self.assertFalse(habits_check.run_checks(self.path).ok)

    def test_chars_limit_still_fails_when_over(self):
        chars, _ = self._sizes()
        self.make(bot_target(rule_limit_bytes=chars - 1, rule_limit_unit="chars"), profile=self.CJK_PROFILE)
        with self.assertRaises(render.RenderError) as ctx:
            render.render_rules(self.m, RULES, "bot")
        self.assertIn("字符", str(ctx.exception))

    def test_bad_unit_fails_check(self):
        self.make(bot_target(rule_limit_unit="words"))
        fails = [r for r in habits_check.run_checks(self.path).results if r["level"] == "FAIL"]
        self.assertTrue(any("rule_limit_unit" in r["message"] and r["fix"] for r in fails), fails)


class AlsoOutputTests(RenderBase):
    def setUp(self):
        self.make(bot_target(also_output=["~/bot2/AGENTS.md"]))
        self.extra = self.home / "bot2" / "AGENTS.md"

    def test_check_passes_when_both_match(self):
        code, out, err = self.cli("--write")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.extra.read_text(encoding="utf-8"), self.out.read_text(encoding="utf-8"))
        code, out, err = self.cli("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("PASS ~/bot/AGENTS.md", out)
        self.assertIn("PASS ~/bot2/AGENTS.md", out)

    def test_check_reports_the_missing_path(self):
        self.cli("--write")
        self.extra.unlink()
        code, out, err = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("PASS ~/bot/AGENTS.md", out)
        self.assertIn("FAIL ~/bot2/AGENTS.md 不存在", err)

    def test_write_snapshots_both_into_one_folder(self):
        for p in (self.out, self.extra):
            p.parent.mkdir(parents=True)
            p.write_text("old\n", encoding="utf-8")
        code, out, err = self.cli("--write")
        self.assertEqual(code, 0, err)
        snaps = sorted((self.root / "state" / "agent-env" / "snapshots").glob("*/bot/AGENTS.md*"))
        self.assertEqual([s.name for s in snaps], ["AGENTS.md", "AGENTS.md.1"])  # same basename kept apart
        self.assertEqual(snaps[0].parent, snaps[1].parent)


class ManifestFieldTests(RenderBase):
    def fails(self, **extra):
        self.make(bot_target(**extra))
        rep = habits_check.run_checks(self.path)
        return [r for r in rep.results if r["level"] == "FAIL"]

    def test_valid_fields_pass(self):
        self.assertEqual(self.fails(skills_config="~/c.yaml", skills_disabled=[]), [])
        self.assertEqual(self.fails(also_output=["~/other/AGENTS.md"]), [])

    def test_missing_supplement_file_fails(self):
        fails = self.fails(supplement="nope.md")
        self.assertTrue(any("档案不存在" in r["message"] for r in fails))
        self.assertTrue(all(r["fix"] for r in fails))

    def test_bad_types_fail(self):
        for extra, needle in [({"output": "/abs/x"}, "output"),
                              ({"output": 3}, "output"),
                              ({"render": "manual"}, "render"),
                              ({"supplement": 1}, "supplement"),
                              ({"skills_disabled": "a", "skills_config": "~/c"}, "skills_disabled"),
                              ({"skills_disabled": ["a"]}, "skills_config"),
                              ({"skills_config": "~/c"}, "skills_config"),
                              ({"also_output": "~/x"}, "also_output"),
                              ({"also_output": ["/abs/x"]}, "also_output"),
                              ({"also_output": []}, "also_output"),
                              ({"also_output": ["~/bot/AGENTS.md"]}, "重复"),
                              ({"also_output": ["~/x", "~/x"]}, "重复")]:
            with self.subTest(extra=extra):
                fails = self.fails(**extra)
                self.assertTrue(any(needle in r["message"] for r in fails), fails)

    def test_also_output_without_render_fails(self):
        t = bot_target(also_output=["~/x"])
        del t["render"]
        self.make(t)
        fails = [r for r in habits_check.run_checks(self.path).results if r["level"] == "FAIL"]
        self.assertTrue(any("also_output" in r["message"] for r in fails), fails)

    def test_render_without_output_fails(self):
        t = bot_target()
        del t["output"]
        self.make(t)
        fails = [r for r in habits_check.run_checks(self.path).results if r["level"] == "FAIL"]
        self.assertTrue(any("没有 output" in r["message"] for r in fails))

    def test_size_check_counts_profile(self):
        self.make(bot_target(rule_limit_bytes=len(PROFILE.encode()) + 60))
        fails = [r for r in habits_check.run_checks(self.path).results if r["level"] == "FAIL"]
        self.assertTrue(any("超限" in r["message"] for r in fails), fails)


if __name__ == "__main__":
    unittest.main()
