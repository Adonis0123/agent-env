"""Plugins (engine/plugins.py): visibility rules, stable builds, temp-dir swaps, the publish ledger, the
privacy guard, Agent Skills limits, the pointer rule, token expiry and 已覆盖 reporting."""
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import habits  # noqa: E402
import habits_check  # noqa: E402
import plugins  # noqa: E402
import vendor  # noqa: E402
from test_agent_env import Base, toml_value  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEMO_MANIFEST = REPO / "examples" / "demo" / "manifest" / "habits.toml"  # the README's example personal layer
CLOUD_ENVS = {"test": {"platform": "nomatch"},
              "cloud": {"transport": "cloud-hosted", "driven_from": "test", "deny_visibility": ["company"]}}
PUBLIC = {"visibility": "public", "description": "public rules", "sections": ["__preamble__", "Shared"],
          "skills": ["own-skill"], "skill_sources": ["third"]}
PERSONAL = {"visibility": "personal", "sections": ["Cli only"]}
BOT_EXPECT = [{"kind": "delivered", "habit": "rules", "name": "指路规则"},
              {"kind": "plugins", "habit": "rules"}, {"kind": "token", "habit": "rules"}]


class PluginBase(Base):
    def setUp(self):
        super().setUp()
        # gitleaks is a subprocess and may not be installed where the tests run: by default it is
        # "installed and finds nothing"; the tests of the gitleaks layer put the real one back
        self.gitleaks = mock.patch.object(plugins, "gitleaks_hits", lambda root: [])
        self.gitleaks.start()
        self.addCleanup(self.gitleaks.stop)
        self.skills = self.root / "skills"
        self.skill("own-skill", "Does its own thing.")
        self.skill("third", "Comes from upstream.")
        self.lock = self.root / "skill-lock.json"
        self.lock.write_text(json.dumps({"version": 3, "skills": {"third": {
            "source": "someone/skills", "sourceType": "github", "sourceUrl": "https://github.com/someone/skills.git",
            "skillPath": "skills/third/SKILL.md", "skillFolderHash": "abc", "installedAt": "x"}}}), encoding="utf-8")

    def skill(self, name, desc, body="body\n", front_name=None):
        d = self.skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {front_name or name}\ndescription: {desc}\n---\n{body}", encoding="utf-8")
        return d

    def setup(self, plugin_specs, targets=None, environments=None):
        targets = targets if targets is not None else [self.bot(["pub", "per"])]
        m = self.write_manifest(targets, environments if environments is not None else CLOUD_ENVS)
        text = 'plugin_owner = "tester"\n' + m.read_text()
        text = text.replace("[sources]\n", f'[sources]\nskills = "{self.skills}"\nskills_lock = "{self.lock}"\n', 1)
        for name, spec in plugin_specs.items():
            text += f"[plugins.{name}]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in spec.items())
        m.write_text(text, encoding="utf-8")
        return habits.load_manifest(m)

    def bot(self, names, **extra):
        return self.target(BOT_EXPECT, id="cloud/bot", host="grok-bot", env="cloud", roles=["remote"],
                           plugins=names, **extra)

    def fails(self):
        return [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]

    def rules(self):
        return (self.root / "AGENTS.md").read_text(encoding="utf-8")


class VisibilityTests(PluginBase):
    def test_valid_setup_passes_check(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL})
        self.assertEqual(self.fails(), [])

    def test_unmarked_plugin_is_local(self):
        self.assertEqual(plugins.visibility({}), "local")
        self.setup({"pub": PUBLIC, "per": {"sections": ["Cli only"]}})  # no visibility
        fails = self.fails()
        self.assertTrue(any("不能用本机插件「per」" in f and "没标可见性" in f for f in fails), fails)

    def test_company_plugin_rejected_for_cloud_env_that_denies_it(self):
        self.setup({"pub": PUBLIC, "co": {"visibility": "company", "sections": ["Cli only"]}},
                   [self.bot(["pub", "co"])])
        fails = self.fails()
        self.assertTrue(any("不允许公司内容" in f and "「co」" in f for f in fails), fails)

    def test_demo_manifest_rejects_company_plugin_for_grok_bot(self):
        with open(DEMO_MANIFEST, "rb") as fh:
            import tomllib
            m = tomllib.load(fh)
        m.setdefault("plugins", {})["co"] = {"visibility": "company"}
        bot = next(t for t in m["targets"] if t["id"] == "grok-bot/bot")
        self.assertEqual(plugins.target_errors(m, bot), [])
        bot = dict(bot, plugins=bot["plugins"] + ["co"])
        errs = plugins.target_errors(m, bot)
        self.assertTrue(any("不允许公司内容" in msg for msg, _ in errs), errs)
        self.assertTrue(all("修复" not in fix and fix for _, fix in errs))

    def test_unknown_plugin_and_local_not_last(self):
        self.setup({"pub": PUBLIC, "patch": {"visibility": "local"}},
                   [self.target([{"kind": "plugins", "habit": "rules"}], plugins=["patch", "pub", "ghost"])],
                   environments={"test": {"platform": "nomatch"}})
        fails = self.fails()
        self.assertTrue(any("未定义的插件「ghost」" in f for f in fails), fails)
        self.assertTrue(any("补丁层「patch」没有排在最后" in f for f in fails), fails)

    def test_public_plugin_cannot_carry_private_section(self):
        self.setup({"pub": dict(PUBLIC, sections=["Cli only"]), "per": PERSONAL})
        self.assertTrue(any("章节「Cli only」没有登记为 public" in f for f in self.fails()))

    def test_own_skill_that_has_an_upstream_must_be_recorded(self):
        self.setup({"pub": dict(PUBLIC, skills=["third"], skill_sources=[]), "per": PERSONAL})
        self.assertTrue(any("移到 skill_sources" in r["fix"] for r in habits_check.run_checks(self.manifest).results))


class SkillLimitTests(PluginBase):
    def test_limits(self):
        long_name = "a" * 65
        self.assertTrue(any("超过 64" in m for m, _ in plugins.skill_limit_errors(self.skill(long_name, "x"))))
        self.assertTrue(any("超过 1024" in m for m, _ in plugins.skill_limit_errors(self.skill("big", "d" * 1025))))
        self.assertTrue(any("和目录名不一致" in m for m, _ in plugins.skill_limit_errors(self.skill("dir", "x", front_name="other"))))
        self.assertEqual(plugins.skill_limit_errors(self.skill("fine", "d" * 1024)), [])

    def test_build_refuses_a_skill_over_the_limit(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        self.skill("own-skill", "d" * 1025)
        with self.assertRaisesRegex(plugins.PluginError, "超过 1024.*修复"):
            plugins.render_plugin(m, "pub", self.rules())


class BuildTests(PluginBase):
    def test_layout_and_stability(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        files = plugins.render_plugin(m, "pub", self.rules())
        self.assertEqual(sorted(files), [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json",
                                         ".cursor-plugin/plugin.json", "AGENTS.md", "agent-env-plugin.json",
                                         "package.json", "skills-lock.json", "skills/own-skill/SKILL.md"])
        agents = files["AGENTS.md"][0].decode()
        self.assertIn("shared body", agents)
        self.assertNotIn("cli body", agents)
        lock = json.loads(files["skills-lock.json"][0])
        self.assertEqual(lock["version"], 1)
        self.assertEqual(list(lock["skills"]["third"]), ["source", "sourceUrl", "sourceType", "skillPath", "computedHash"])
        self.assertEqual(lock["skills"]["third"]["computedHash"], plugins.skill_folder_hash(self.skills / "third"))
        market = json.loads(files[".claude-plugin/marketplace.json"][0])
        self.assertEqual(market["plugins"], [{"name": "pub", "source": "./", "description": "public rules", "version": "0.1.0"}])
        self.assertEqual(json.loads(files[".claude-plugin/plugin.json"][0])["author"], {"name": "tester"})
        self.assertEqual(json.loads(files["package.json"][0])["pi"], {"skills": ["./skills"]})
        manifest = json.loads(files["agent-env-plugin.json"][0])
        self.assertEqual((manifest["visibility"], manifest["visibility_zh"]), ("public", "公开"))
        self.assertFalse(any(rel.startswith((".codex-plugin", ".grok-plugin")) for rel in files))

        out = io.StringIO()
        self.assertEqual(agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--all"], out=out), 0)
        first = plugins.tree_files(plugins.output_dir(m, "pub"))
        out = io.StringIO()
        agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--all"], out=out)
        self.assertEqual(out.getvalue().count("已是最新，未写入"), 2, out.getvalue())
        self.assertEqual(plugins.tree_files(plugins.output_dir(m, "pub")), first)
        for d in (plugins.output_dir(m, "pub"), plugins.output_dir(m, "per")):
            for f in d.rglob("*"):
                if f.is_file():
                    text = f.read_text(encoding="utf-8")
                    self.assertNotIn(str(self.root), text)
                    self.assertNotIn("/Users/", text)

    def test_runtime_dirs_and_symlinks(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        (self.skills / "own-skill" / "node_modules").mkdir()
        (self.skills / "own-skill" / "node_modules" / "x.js").write_text("x")
        self.assertNotIn("skills/own-skill/node_modules/x.js", plugins.render_plugin(m, "pub", self.rules()))
        (self.skills / "own-skill" / "link.md").symlink_to(self.root / "AGENTS.md")
        with self.assertRaisesRegex(plugins.PluginError, "符号链接.*修复"):
            plugins.render_plugin(m, "pub", self.rules())

    def test_failure_mid_build_leaves_the_old_output(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        changed, dest, _, _ = plugins.build(m, "pub", self.rules())
        self.assertTrue(changed)
        before = plugins.tree_files(dest)
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "shared body v2"), encoding="utf-8")
        calls = {"n": 0}
        real = plugins._write_file

        def flaky(path, data, exe):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full")
            real(path, data, exe)
        with mock.patch.object(plugins, "_write_file", flaky), self.assertRaises(OSError):
            plugins.build(m, "pub", self.rules())
        self.assertEqual(plugins.tree_files(dest), before, "old output intact")
        self.assertEqual([p.name for p in dest.parent.iterdir() if p.name.startswith(".")], [".pub.lock"],
                         "no temp dir left (the lock file stays)")
        self.assertTrue(plugins.build(m, "pub", self.rules())[0])
        self.assertIn(b"shared body v2", plugins.tree_files(dest)["AGENTS.md"][0])

    def test_crash_between_renames_is_recovered(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        _, dest, _, _ = plugins.build(m, "pub", self.rules())
        (dest / ".git").mkdir()
        (dest / ".git" / "HEAD").write_text("ref\n")
        os.rename(dest, dest.parent / f".{dest.name}.old-abc")  # the new tree never landed
        changed, _, _, notes = plugins.build(m, "pub", self.rules())
        self.assertFalse(changed, "the old output was put back and is current")
        self.assertTrue(any("放回" in n for n in notes), notes)
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "v3"), encoding="utf-8")
        self.assertTrue(plugins.build(m, "pub", self.rules())[0])
        self.assertEqual((dest / ".git" / "HEAD").read_text(), "ref\n", "a repository's .git survives a rebuild")


class PrivacyTests(PluginBase):
    def hits_for(self, vis, body):
        self.skill("own-skill", "Does its own thing.", body=body)
        m = self.setup({"pub": dict(PUBLIC, visibility=vis), "per": PERSONAL},
                       targets=[self.target([], plugins=["pub"])], environments={"test": {"platform": "nomatch"}})
        try:
            plugins.render_plugin(m, "pub", self.rules())
        except plugins.PluginError as exc:
            self.assertIn("修复：", str(exc))
            return str(exc)
        return ""

    def test_guard_hits(self):
        self.assertIn("绝对家目录路径", self.hits_for("public", "see /Users/you/notes\n"))
        self.assertIn("绝对家目录路径", self.hits_for("personal", "cd /home/alice/x\n"))
        self.assertIn("邮箱", self.hits_for("public", "mail bob@mailhost.io\n"))
        self.assertIn("疑似密钥", self.hits_for("personal", "token ghp_" + "a" * 36 + "\n"))

    def test_reserved_example_domains_are_not_emails(self):
        for ok in ("bob@example.com", "a.b+c@EXAMPLE.org", "x@example.net", "x@corp.example", "x@ci.test",
                   "x@nobody.invalid", "x@dev.localhost"):
            self.assertFalse(plugins.real_email(f"mail {ok} now"), ok)
            self.assertNotIn("邮箱", self.hits_for("public", f"mail {ok}\n"), ok)
        for bad in ("foo@examples.com", "foo@example.com.cn", "foo@mail.example.com", "foo@example.co",
                    "foo@test.com", "foo@invalid.org", "foo@localhost.dev", "foo@example.io"):
            self.assertTrue(plugins.real_email(f"mail {bad} now"), bad)
            self.assertIn("邮箱", self.hits_for("public", f"mail {bad}\n"), bad)
        self.assertTrue(plugins.real_email("x@example.com and bob@mailhost.io"))  # one real address is enough

    def test_company_scans_secrets_only_and_local_scans_nothing(self):
        self.assertEqual(self.hits_for("company", "mail bob@example.com /Users/you\n"), "")
        self.assertIn("疑似密钥", self.hits_for("company", "AKIA" + "A" * 16 + "\n"))
        self.assertEqual(self.hits_for("local", "AKIA" + "A" * 16 + " /Users/you\n"), "")

    def test_safe_text_passes(self):
        self.assertEqual(self.hits_for("public", "clone git@github.com:o/r.git into ~/coding, see $HOME/x\n"), "")


POINTING = ("# Title\n\nIntro line. Details live in `docs/`.\n\n## Shared\n\n"
            "- shared body，细节见 `docs/x.md`。\n- 用 `pbcopy` 复制。\n\n只在本机成立的一段。\n\nlast para\n\n"
            "## Cli only\n\ncli body，脚本在 `scripts/run.zsh`。\n")
OMIT = [" Details live in `docs/`.", "，细节见 `docs/x.md`", "- 用 `pbcopy` 复制。\n", "只在本机成立的一段。"]


class RulesTextBase(PluginBase):
    def with_rules(self, text, plugin_specs=None):
        (self.root / "AGENTS.md").write_text(text, encoding="utf-8")
        return self.setup(plugin_specs or {"pub": PUBLIC, "per": PERSONAL})

    def shared(self, body):
        return f"# Title\n\nIntro line.\n\n## Shared\n\n{body}\n\n## Cli only\n\ncli body\n"


class OutsidePathTests(RulesTextBase):
    """A public plugin may not point at a file it does not carry; a personal one is only warned."""

    def test_public_plugin_is_refused_with_plugin_line_path_and_fix(self):
        m = self.with_rules(POINTING)
        with self.assertRaises(plugins.PluginError) as ctx:
            plugins.render_plugin(m, "pub", self.rules())
        msg = str(ctx.exception)
        for want in ("公开插件「pub」", "AGENTS.md:5 `docs/`", "AGENTS.md:9 `docs/x.md`", "修复：", "[plugins.pub] 的 omit",
                     "放进插件"):
            self.assertIn(want, msg)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"],
                                        out=out, err=err), 1)
        self.assertIsNone(plugins.tree_files(plugins.output_dir(m, "pub")), "nothing is written")

    def test_personal_plugin_is_only_warned(self):
        m = self.with_rules(POINTING)
        self.assertIn("AGENTS.md", plugins.render_plugin(m, "per", self.rules()))
        changed, _, _, notes = plugins.build(m, "per", self.rules())
        self.assertTrue(changed)
        warned = [n for n in notes if n.startswith("提醒：个人插件「per」有 1 处指向插件外的文件")]
        self.assertEqual(len(warned), 1, notes)
        self.assertIn("AGENTS.md:5 `scripts/run.zsh`", warned[0])
        self.assertIn("修复：", warned[0])
        company = self.with_rules(POINTING, {"pub": PUBLIC, "per": dict(PERSONAL, visibility="company")})
        self.assertEqual(len(plugins.outside_notes("per", "company", plugins.render_plugin(company, "per", self.rules()))), 1)
        local = {"AGENTS.md": (b"see `docs/x.md`\n", False)}
        self.assertEqual(plugins.outside_notes("x", "local", local), [])

    def test_what_counts_as_a_path(self):
        def paths(line):
            return [p for _, _, p in plugins.outside_paths({"AGENTS.md": (line.encode(), False)})]
        for line, want in (
                ("see `~/agent-rules/docs/x.md` and `~/`", ["~/agent-rules/docs/x.md", "~/"]),
                ("run `zsh scripts/run.zsh --flag` then `bin/tool`", ["scripts/run.zsh", "bin/tool"]),
                ("put it in `docs/` or `./docs/a.md:12`", ["docs/", "./docs/a.md"]),
                ("see `profiles/mac/README.md` and `../up`", ["profiles/mac/README.md", "../up"]),
                ("[a](../x.md), [b](notes.md#part) and ![c](img/c.png)", ["../x.md", "notes.md#part", "img/c.png"]),
                ("[ref]: docs/ref.md", ["docs/ref.md"]),
                ("`--config=docs/x.toml`", ["docs/x.toml"]),
        ):
            self.assertEqual(paths(line), want, line)
        for line in (
                "bare names `.env`, `a.png`, `AGENTS.md`, `settings.json`, `git status`",
                "urls `https://github.com/owner/repo/docs/x.md`, `git@github.com:owner/docs.md`, [x](https://e.test/docs/x.md)",
                "absolute `/tmp/docs/x.md`, `/usr/bin/trash`, `/permissions`; anchors [x](#docs) and [y](/docs/x.md)",
                "placeholders `skills/<name>/SKILL.md`, `$REPO_ROOT/docs/x.md`, `docs/*.md`",
                "other slashes `owner/repo`, `feat/<slug>`, `and/or`, `.claude/settings.json`, `src/index.ts`",
                "not in a code span: docs/x.md, ~/notes, scripts/run.zsh",
                "```\nsee `docs/x.md` and [a](docs/x.md)\n```",
        ):
            self.assertEqual(paths(line), [], line)
        script = {"skills/s/run.sh": (b"x=`cat docs/x.md`\n", True)}
        self.assertEqual(plugins.outside_paths(script), [], "in a script a backtick is code")

    def test_paths_inside_the_plugin_pass(self):
        d = self.skill("own-skill", "Does its own thing.",
                       body="See `references/a.md`, [a](references/a.md), `./` and `skills/own-skill/SKILL.md`.\n")
        (d / "references").mkdir()
        (d / "references" / "a.md").write_text("Back to [the skill](../SKILL.md) and `../../../skills/`.\n", encoding="utf-8")
        m = self.with_rules(self.shared("Skills live in `skills/`, start at `skills/own-skill/SKILL.md`."))
        files = plugins.render_plugin(m, "pub", self.rules())
        self.assertIn("skills/own-skill/references/a.md", files)
        self.skill("own-skill", "Does its own thing.", body="See `references/missing.md`.\n")
        with self.assertRaisesRegex(plugins.PluginError, r"skills/own-skill/SKILL\.md:5 `references/missing\.md`"):
            plugins.render_plugin(m, "pub", self.rules())


class OmitTests(RulesTextBase):
    """`omit` cuts exact pieces out of the sections a plugin takes; a piece that is not there fails."""

    def pub(self, omit, rules=POINTING):
        return self.with_rules(rules, {"pub": dict(PUBLIC, omit=omit), "per": PERSONAL})

    def agents(self, m):
        text = plugins.render_plugin(m, "pub", self.rules())["AGENTS.md"][0].decode()
        return text.split("-->\n\n", 1)[1]

    def test_pieces_are_cut_and_the_lines_left_clean(self):
        m = self.pub(OMIT)
        self.assertEqual(self.fails(), [])
        self.assertEqual(self.agents(m), "# Title\n\nIntro line.\n\n## Shared\n\n- shared body。\n\nlast para\n\n")
        self.assertIn("cli body", plugins.render_plugin(m, "per", self.rules())["AGENTS.md"][0].decode(),
                      "another plugin's sections are not touched")

    def test_pieces_reach_no_generated_file(self):
        files = plugins.render_plugin(self.pub(OMIT), "pub", self.rules())
        for rel, (data, _) in files.items():
            text = data.decode()
            for piece in OMIT + ["omit", "pbcopy", "只在本机成立"]:
                self.assertNotIn(piece.strip(), text, rel)
            self.assertNotIn(json.dumps(OMIT[1])[1:-1], text, rel)
        self.assertNotIn("omit", json.loads(files["agent-env-plugin.json"][0]))

    def test_build_is_byte_stable(self):
        m = self.pub(OMIT)
        self.assertEqual(plugins.render_plugin(m, "pub", self.rules()), plugins.render_plugin(m, "pub", self.rules()))
        args = ["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"]
        out = io.StringIO()
        self.assertEqual(agent_env.main(args, out=out), 0)
        self.assertIn("已生成", out.getvalue())
        first = plugins.tree_files(plugins.output_dir(m, "pub"))
        out = io.StringIO()
        self.assertEqual(agent_env.main(args, out=out), 0)
        self.assertIn("已是最新，未写入", out.getvalue())
        self.assertEqual(plugins.tree_files(plugins.output_dir(m, "pub")), first)

    def test_a_piece_that_is_not_in_the_source_fails(self):
        m = self.pub(OMIT + ["，这句源里没有"])
        with self.assertRaisesRegex(plugins.PluginError, r"\[plugins\.pub\] 的 omit 第 5 项.*找不到.*修复：") as ctx:
            plugins.render_plugin(m, "pub", self.rules())
        self.assertNotIn("这句源里没有", str(ctx.exception), "an error names a piece by position, not by text")
        m = self.pub(OMIT)  # the source changes under a piece that used to match
        (self.root / "AGENTS.md").write_text(POINTING.replace("细节见", "详见"), encoding="utf-8")
        with self.assertRaisesRegex(plugins.PluginError, "omit 第 2 项.*找不到"):
            plugins.render_plugin(m, "pub", self.rules())
        self.assertIsNone(plugins.tree_files(plugins.output_dir(m, "pub")))
        m = self.pub(["cli body"])  # text of a section this plugin does not take
        with self.assertRaisesRegex(plugins.PluginError, "omit 第 1 项.*找不到"):
            plugins.render_plugin(m, "pub", self.rules())

    def test_each_entry_cuts_one_occurrence(self):
        rules = self.shared("- one，见上。\n- two，见上。")
        with self.assertRaisesRegex(plugins.PluginError, "omit 第 1 项.*出现 2 处，清单里写了 1 次.*修复："):
            plugins.render_plugin(self.pub(["，见上"], rules), "pub", self.rules())
        self.assertIn("- one。\n- two。\n", self.agents(self.pub(["，见上", "，见上"], rules)))
        only_one = self.agents(self.pub(["- two，见上。"], rules))  # written longer: it matches one place
        self.assertEqual(only_one, "# Title\n\nIntro line.\n\n## Shared\n\n- one，见上。\n\n")

    def test_dangling_punctuation_is_refused_not_guessed(self):
        for piece, what in (("细节见 `docs/x.md`", "连在一起的标点 ，。"), ("shared body，细节见 `docs/x.md`", "行首的标点 。"),
                            ("shared body，细节见 `docs/x.md`。", "只剩标记的空列表项")):
            with self.assertRaisesRegex(plugins.PluginError, f"omit 第 1 项去掉后留下了{what}.*修复：") as ctx:
                plugins.render_plugin(self.pub([piece]), "pub", self.rules())
        rules = self.shared("keep（见 `docs/x.md`） this,  not  that")
        with self.assertRaisesRegex(plugins.PluginError, "空括号 （）"):
            plugins.render_plugin(self.pub(["见 `docs/x.md`"], rules), "pub", self.rules())
        self.assertIn("keep this,  not  that\n", self.agents(self.pub(["（见 `docs/x.md`）"], rules)),
                      "spaces the cut did not touch stay as they are")

    def test_headings_and_fences_cannot_be_cut(self):
        with self.assertRaisesRegex(plugins.PluginError, "标题行或代码块围栏.*修复："):
            plugins.render_plugin(self.pub(["## Shared\n"]), "pub", self.rules())
        rules = self.shared("```\ncode\n```")
        with self.assertRaisesRegex(plugins.PluginError, "标题行或代码块围栏"):
            plugins.render_plugin(self.pub(["```\ncode"], rules), "pub", self.rules())

    def test_field_is_validated_by_check(self):
        self.pub("，细节见")
        self.assertTrue(any("omit 不是字符串列表" in f for f in self.fails()), self.fails())
        self.pub(["  "])
        self.assertTrue(any("omit 第 1 项只有空白" in f for f in self.fails()), self.fails())
        self.with_rules(POINTING, {"pub": {"visibility": "public", "omit": ["x"]}, "per": PERSONAL})
        self.assertTrue(any("写了 omit 却没有 sections" in f for f in self.fails()), self.fails())


class LedgerTests(PluginBase):
    def publish(self, m):
        out = io.StringIO()
        return plugins.publish(m, ["pub"], self.rules(), out), out.getvalue()

    def test_stops_at_commit_then_resumes(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        rc, text = self.publish(m)
        self.assertEqual(rc, 1)
        self.assertIn("build 完成", text)
        self.assertIn("scan 完成", text)
        self.assertIn("未完成，停在 commit", text)
        ledger = json.loads(plugins.ledger_path().read_text())
        self.assertEqual(sorted(ledger["plugins"]["pub"]["steps"]), ["build", "scan"])
        rc, text = self.publish(m)
        self.assertIn("build 已完成", text)
        self.assertIn("scan 已完成", text)
        self.assertEqual(rc, 1)

    def test_interrupted_after_build_resumes_at_scan(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        with mock.patch.dict(plugins.STEPS, {"scan": mock.Mock(side_effect=KeyboardInterrupt)}):
            with self.assertRaises(KeyboardInterrupt):
                self.publish(m)
        self.assertEqual(list(json.loads(plugins.ledger_path().read_text())["plugins"]["pub"]["steps"]), ["build"])
        with mock.patch.object(plugins, "install", side_effect=AssertionError("build must not rerun")):
            rc, text = self.publish(m)
        self.assertIn("build 已完成", text)
        self.assertIn("scan 完成", text)

    def test_source_change_restarts(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        self.publish(m)
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "v2"), encoding="utf-8")
        rc, text = self.publish(m)
        self.assertIn("源变了，从生成重新开始", text)
        self.assertIn("build 完成", text)

    def test_local_plugin_never_published(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL, "patch": {"visibility": "local"}})
        out = io.StringIO()
        self.assertEqual(plugins.publish(m, ["patch"], self.rules(), out), 0)
        self.assertIn("不发布", out.getvalue())


class PointerTests(PluginBase):
    def test_placeholders_length_and_record(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL})
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(agent_env.main(["plugin", "pointer", "--manifest", str(self.manifest), "--target", "cloud/bot"],
                                        out=out, err=err), 0)
        text = out.getvalue()
        self.assertIn("<公开插件仓地址>", text)
        self.assertIn("/workspace", text)
        self.assertIn("从没拉成功过就停下问我", text)
        self.assertLessEqual(len(text.rstrip("\n")), 400)
        self.assertIn("占位符", err.getvalue())
        err = io.StringIO()
        rc = agent_env.main(["plugin", "pointer", "--manifest", str(self.manifest), "--target", "cloud/bot", "--record"],
                            out=io.StringIO(), err=err)
        self.assertEqual(rc, 1)
        self.assertIn("占位符", err.getvalue())

    def test_real_urls_record_and_doctor(self):
        self.setup({"pub": dict(PUBLIC, repo="https://github.com/someone/agent-public"),
                    "per": dict(PERSONAL, repo="https://github.com/someone/agent-personal")},
                   [self.bot(["pub", "per"], token={"name": "BOT_PLUGIN_TOKEN", "expires": "2099-01-01"})])
        m = habits.load_manifest(self.manifest)
        text = plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))
        self.assertLessEqual(len(text.rstrip("\n")), 400)
        self.assertLess(text.index("agent-public"), text.index("agent-personal"))
        self.assertIn("secret BOT_PLUGIN_TOKEN", text)
        state = lambda: {c["name"]: c["state"] for c in self.cells(self.doctor("cloud"), "cloud/bot")}["指路规则"]
        self.assertEqual(state(), "missing")
        agent_env.main(["plugin", "pointer", "--manifest", str(self.manifest), "--target", "cloud/bot"],
                       out=io.StringIO(), err=io.StringIO())
        self.assertEqual(agent_env.main(["plugin", "pointer", "--manifest", str(self.manifest), "--target", "cloud/bot",
                                         "--record"], out=io.StringIO(), err=io.StringIO()), 0)
        self.assertEqual(state(), "synced")

    def test_lock_step_only_when_a_plugin_has_third_party_skills(self):
        url = "https://github.com/someone/agent-public"
        m = self.setup({"pub": dict(PUBLIC, repo=url), "per": PERSONAL}, [self.bot(["pub"])])
        text = plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))
        self.assertIn("skills/ 直接读", text)
        for part in ("skills-lock.json", "npx -y skills experimental_install", ".agents/skills/",
                     "sourceUrl+skillPath", "说明用了哪种"):
            self.assertIn(part, text)
        m = self.setup({"pub": dict(PUBLIC, repo=url, skill_sources=[]), "per": PERSONAL}, [self.bot(["pub"])])
        text = plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))
        self.assertIn("skills/ 直接读", text)
        self.assertNotIn("skills-lock.json", text)

    def test_too_long(self):
        m = self.setup({"pub": dict(PUBLIC, repo="https://github.com/" + "x" * 400), "per": PERSONAL})
        with self.assertRaisesRegex(plugins.PluginError, "超过上限 400.*修复"):
            plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))


class TokenTests(PluginBase):
    def cell(self, token=None, names=("pub", "per")):
        extra = {"token": token} if token else {}
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(list(names), **extra)])
        return {c["name"]: c for c in self.cells(self.doctor("cloud"), "cloud/bot")}["插件 token"]

    def test_missing_when_private_plugin_has_no_token(self):
        c = self.cell()
        self.assertEqual(c["state"], "missing")
        self.assertIn("fine-grained", c["fix"])

    def test_public_only_needs_none(self):
        self.assertEqual(self.cell(names=("pub",))["state"], "synced")

    def test_warn_within_seven_days_and_fail_when_expired(self):
        soon = (date.today() + timedelta(days=5)).isoformat()
        c = self.cell({"name": "T", "expires": soon})
        self.assertEqual((c["state"], c.get("warn")), ("synced", True))
        self.assertIn("还有 5 天", c["detail"])
        self.assertIn("提醒：", agent_env.format_doctor(self.doctor("cloud")))
        c = self.cell({"name": "T", "expires": date.today().isoformat()})
        self.assertEqual(c["state"], "drift")
        self.assertIn("已于", c["detail"])
        far = self.cell({"name": "T", "expires": (date.today() + timedelta(days=60)).isoformat()})
        self.assertEqual((far["state"], far.get("warn")), ("synced", None))

    def test_token_shape_validated(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], token={"name": "T", "value": "x"})])
        fails = self.fails()
        self.assertTrue(any("token 多了键 ['value']" in f for f in fails), fails)
        self.assertTrue(any("token.expires 不是日期" in f for f in fails), fails)


class OverrideAndStatusTests(PluginBase):
    def test_patch_layer_overrides_are_reported(self):
        patch = self.root / "patch.md"
        patch.write_text("## Shared\n\nlocal shared body\n", encoding="utf-8")
        self.setup({"pub": PUBLIC, "per": PERSONAL, "patch": {"visibility": "local", "rules_file": str(patch)}},
                   [self.target([{"kind": "plugins", "habit": "rules"}], plugins=["pub", "patch"])],
                   environments={"test": {"platform": "nomatch"}})
        self.assertEqual(self.fails(), [])
        cells = self.cells(self.doctor())
        over = [c for c in cells if c["name"].startswith("已覆盖")]
        self.assertEqual(len(over), 1, cells)
        self.assertEqual(over[0]["state"], "customized")
        self.assertIn("原来自插件 pub，由补丁层 patch 覆盖", over[0]["detail"])
        self.assertEqual({c["name"]: c["state"] for c in cells}["插件 pub（公开）"], "missing")
        self.assertIn(b"local shared body", plugins.render_plugin(habits.load_manifest(self.manifest), "patch",
                                                                  self.rules())["AGENTS.md"][0])

    def test_stale_build_is_drift(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        plugins.build(m, "pub", self.rules())
        name = "插件 pub（公开）"
        self.assertEqual({c["name"]: c["state"] for c in self.cells(self.doctor("cloud"), "cloud/bot")}[name], "synced")
        self.skill("third", "Comes from upstream, updated.")  # a third-party skill changed: its computedHash too
        c = {c["name"]: c for c in self.cells(self.doctor("cloud"), "cloud/bot")}[name]
        self.assertEqual(c["state"], "drift")
        self.assertEqual(c["fix"], "bin/agent-env plugin build --plugin pub")

    def test_cloud_env_is_its_own_environment_in_all_status(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL})
        live = self.doctor("test")
        self.assertEqual([t["id"] for t in live["targets"]], [], "the bot is not inside the machine that drives it")
        all_ = agent_env.all_status(self.manifest, "test", self.repo, live=live)
        envs = {e["env"]: e for e in all_["environments"]}
        self.assertEqual(envs["cloud"]["checked_from"], "test")
        self.assertEqual(envs["cloud"]["source"], "live")
        self.assertEqual([t["id"] for t in envs["cloud"]["targets"]], ["cloud/bot"])
        self.assertFalse(any(e["env"] == "cloud" for e in all_["errors"]))


class ComputedHashTests(PluginBase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_matches_the_npx_skills_algorithm(self):
        d = self.skills / "mixed"
        for rel in ("SKILL.md", "a-b.md", "a_b.md", "a.md", "B.md", "b/c.md", "b-c/d.md", "Z9.txt", "10.md", "node_modules/x"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(rel, encoding="utf-8")
        js = """
import { createHash } from "node:crypto"; import { readdir, readFile } from "node:fs/promises"; import { join, relative } from "node:path";
async function collect(base, cur, out) { const es = await readdir(cur, { withFileTypes: true }); await Promise.all(es.map(async (e) => {
  const f = join(cur, e.name); if (e.isDirectory()) { if (e.name === ".git" || e.name === "node_modules") return; await collect(base, f, out); }
  else if (e.isFile()) out.push({ r: relative(base, f).split("\\\\").join("/"), c: await readFile(f) }); })); }
const fs = []; await collect(process.argv[1], process.argv[1], fs); fs.sort((a, b) => a.r.localeCompare(b.r));
const h = createHash("sha256"); for (const f of fs) { h.update(f.r); h.update(f.c); } console.log(h.digest("hex"));
"""
        node = subprocess.run(["node", "--input-type=module", "-e", js, str(d)], capture_output=True, text=True, check=True)
        self.assertEqual(plugins.skill_folder_hash(d), node.stdout.strip())


MIT = "MIT License\n\nCopyright (c) 2025 Someone\n\nPermission is hereby granted, free of charge...\n"


class VendorTests(PluginBase):
    """Vendored third-party skills (plugin `vendor`, engine/vendor.py): copied from the local copy with
    their provenance and upstream license, left out of skills-lock.json and the npx pointer step."""

    def setUp(self):
        super().setUp()
        (self.skills / "third" / "scripts").mkdir()
        (self.skills / "third" / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
        os.chmod(self.skills / "third" / "scripts" / "run.sh", 0o755)
        self.skill("other", "Another upstream skill.")
        self.upstream_tree = self.git_tree(self.skills / "third")
        self.write_lock()
        self.licenses = plugins.state_dir() / "licenses"
        self.licenses.mkdir(parents=True)
        (self.licenses / "someone-skills.LICENSE").write_text(MIT, encoding="utf-8")

    def write_lock(self, third_url="https://github.com/someone/skills.git"):
        entry = lambda n, url, h: {"source": "someone/skills", "sourceType": "github", "sourceUrl": url,
                                   "skillPath": f"skills/{n}/SKILL.md", "skillFolderHash": h, "installedAt": "x"}
        self.lock.write_text(json.dumps({"version": 3, "skills": {
            "third": entry("third", third_url, self.upstream_tree),
            "other": entry("other", "https://github.com/someone/skills.git", "0" * 40)}}), encoding="utf-8")

    @staticmethod
    def git_tree(d: Path) -> str:
        """`git write-tree` of a folder: the reference for vendor.tree_sha."""
        work = Path(d).parent / f".{Path(d).name}-git"
        shutil.copytree(d, work / "x")
        g = lambda *a: subprocess.run(["git", *a], cwd=work / "x", capture_output=True, text=True, check=True).stdout
        g("init", "-q")
        g("add", "-A")
        tree = g("write-tree").strip()
        shutil.rmtree(work)
        return tree

    def spec(self, **extra):
        return {**PUBLIC, "skill_sources": ["third", "other"], "vendor": ["third"], **extra}

    def test_tree_sha_equals_git_write_tree(self):
        d = self.skills / "third"
        files = {rel: ((d / rel).read_bytes(), bool((d / rel).stat().st_mode & 0o111)) for rel in plugins.skill_copy(d)[0]}
        self.assertEqual(vendor.tree_sha(files), self.git_tree(d))
        files["a-b/c.md"] = (b"x", False)
        files["a.md"] = (b"y", False)
        files["a/b.md"] = (b"z", True)
        nested = self.skills / "nested"
        for rel, (data, exe) in files.items():
            (nested / rel).parent.mkdir(parents=True, exist_ok=True)
            (nested / rel).write_bytes(data)
            os.chmod(nested / rel, 0o755 if exe else 0o644)
        self.assertEqual(vendor.tree_sha(files), self.git_tree(nested))

    def test_copy_license_provenance_and_lock(self):
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        self.assertEqual(self.fails(), [])
        files = plugins.render_plugin(m, "pub", self.rules())
        for rel in ("SKILL.md", "scripts/run.sh"):
            self.assertEqual(files[f"skills/third/{rel}"][0], (self.skills / "third" / rel).read_bytes())
        self.assertTrue(files["skills/third/scripts/run.sh"][1])
        self.assertEqual(files["skills/third/UPSTREAM-LICENSE"][0].decode(), MIT)
        index = json.loads(files["vendor.json"][0])
        self.assertEqual(index, {"version": 1, "skills": {"third": {
            "source": "someone/skills", "sourceUrl": "https://github.com/someone/skills.git",
            "skillPath": "skills/third/SKILL.md", "upstreamTree": self.upstream_tree, "localTree": self.upstream_tree,
            "modified": False, "license": "skills/third/UPSTREAM-LICENSE"}}})
        md = files["skills/third/UPSTREAM.md"][0].decode()
        for part in ("someone/skills", "Path: skills/third", self.upstream_tree, "Modified locally: no", "UPSTREAM-LICENSE"):
            self.assertIn(part, md)
        lock = json.loads(files["skills-lock.json"][0])
        self.assertEqual(list(lock["skills"]), ["other"])  # the vendored one is not installed through npx
        meta = json.loads(files["agent-env-plugin.json"][0])
        self.assertEqual((meta["skill_sources"], meta["vendor"]), (["third", "other"], ["third"]))
        self.assertNotIn("skills/other/SKILL.md", files)

    def test_local_edit_is_marked_modified(self):
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        with open(self.skills / "third" / "SKILL.md", "a", encoding="utf-8") as fh:
            fh.write("my own line\n")
        files = plugins.render_plugin(m, "pub", self.rules())
        rec = json.loads(files["vendor.json"][0])["skills"]["third"]
        self.assertTrue(rec["modified"])
        self.assertEqual(rec["localTree"], self.git_tree(self.skills / "third"))
        self.assertIn("Modified locally: yes", files["skills/third/UPSTREAM.md"][0].decode())

    def test_all_vendored_means_no_lock_and_no_npx_step(self):
        url = "https://github.com/someone/agent-public"
        m = self.setup({"pub": self.spec(repo=url, skill_sources=["third"]), "per": PERSONAL}, [self.bot(["pub"])])
        files = plugins.render_plugin(m, "pub", self.rules())
        self.assertNotIn("skills-lock.json", files)
        text = plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))
        self.assertIn("skills/ 直接读，要装就整个目录复制（含 UPSTREAM 出处和许可）", text)
        self.assertNotIn("npx", text)
        self.assertNotIn("skills-lock.json", text)
        self.assertLessEqual(len(text.rstrip("\n")), 400)
        m = self.setup({"pub": self.spec(repo=url), "per": PERSONAL}, [self.bot(["pub"])])
        text = plugins.pointer_text(m, habits.find_target(m, "cloud/bot"))
        self.assertIn("整个目录复制", text)
        self.assertIn("npx -y skills experimental_install", text)  # "other" is still in the lock
        self.assertLessEqual(len(text.rstrip("\n")), 400)

    def test_byte_stable(self):
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        self.assertEqual(plugins.render_plugin(m, "pub", self.rules()), plugins.render_plugin(m, "pub", self.rules()))
        self.assertEqual(agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"],
                                        out=io.StringIO()), 0)
        first = plugins.tree_files(plugins.output_dir(m, "pub"))
        out = io.StringIO()
        agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"], out=out)
        self.assertIn("已是最新，未写入", out.getvalue())
        self.assertEqual(plugins.tree_files(plugins.output_dir(m, "pub")), first)

    def test_missing_license_fails_with_a_fix(self):
        (self.licenses / "someone-skills.LICENSE").unlink()
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        results = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("缺上游许可" in r["message"] and "someone-skills.LICENSE" in r["message"]
                            and "vendor-diff --plugin pub --skill third --write-license" in r["fix"] for r in results), results)
        with self.assertRaisesRegex(plugins.PluginError, "缺上游许可.*修复"):
            plugins.render_plugin(m, "pub", self.rules())
        err = io.StringIO()
        self.assertNotEqual(agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"],
                                           out=io.StringIO(), err=err), 0)

    def test_licenses_dir_from_sources(self):
        other = self.root / "my-licenses"
        other.mkdir()
        (other / "someone-skills.LICENSE").write_text("other text\n", encoding="utf-8")
        self.setup({"pub": self.spec(), "per": PERSONAL})
        self.manifest.write_text(self.manifest.read_text().replace("[sources]\n", f'[sources]\nlicenses = "{other}"\n', 1))
        m = habits.load_manifest(self.manifest)
        self.assertEqual(plugins.render_plugin(m, "pub", self.rules())["skills/third/UPSTREAM-LICENSE"][0], b"other text\n")

    def test_vendor_must_be_a_skill_source_and_not_clash(self):
        self.setup({"pub": self.spec(vendor=["third", "ghost"]), "per": PERSONAL})
        self.assertTrue(any("不在 skill_sources" in f for f in self.fails()))
        (self.skills / "third" / "UPSTREAM.md").write_text("mine\n", encoding="utf-8")
        self.setup({"pub": self.spec(), "per": PERSONAL})
        self.assertTrue(any("和生成的出处文件重名" in f for f in self.fails()))

    def test_outside_path_in_a_vendored_skill_is_a_note_not_a_refusal(self):
        (self.skills / "third" / "SKILL.md").write_text("---\nname: third\ndescription: x\n---\nsee `docs/design.md`\n",
                                                       encoding="utf-8")
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        files = plugins.render_plugin(m, "pub", self.rules())  # upstream text: not refused
        notes = plugins.outside_notes("pub", "public", files)
        self.assertTrue(any("第三方 skill" in n and "docs/design.md" in n for n in notes), notes)
        self.skill("own-skill", "Does its own thing.", body="see `docs/design.md`\n")
        with self.assertRaisesRegex(plugins.PluginError, "指向插件外的文件"):  # an own skill still is
            plugins.render_plugin(m, "pub", self.rules())

    def test_local_change_shows_drift(self):
        from expect import plugins as expect_plugins
        m = self.setup({"pub": self.spec(), "per": PERSONAL})
        agent_env.main(["plugin", "build", "--manifest", str(self.manifest), "--plugin", "pub"], out=io.StringIO())
        self.assertEqual(expect_plugins.plugin_cell(m, "pub", self.rules())["state"], "synced")
        (self.skills / "third" / "SKILL.md").write_text("---\nname: third\ndescription: changed\n---\nnew\n", encoding="utf-8")
        c = expect_plugins.plugin_cell(m, "pub", self.rules())
        self.assertEqual(c["state"], "drift")
        self.assertIn("更新 3 个文件", c["detail"] if "detail" in c else json.dumps(c, ensure_ascii=False))

    def test_vendor_diff_against_upstream_head(self):
        up = self.root / "upstream"
        (up / "skills" / "third" / "scripts").mkdir(parents=True)
        for rel in ("SKILL.md", "scripts/run.sh"):
            shutil.copy2(self.skills / "third" / rel, up / "skills" / "third" / rel)
        (up / "LICENSE").write_text("Upstream MIT\n", encoding="utf-8")
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                                      cwd=up, capture_output=True, text=True, check=True)
        g("init", "-q")
        g("add", "-A")
        g("commit", "-qm", "init")
        self.write_lock(third_url=str(up))
        (self.licenses / "someone-skills.LICENSE").unlink()
        self.setup({"pub": self.spec(), "per": PERSONAL})
        args = ["plugin", "vendor-diff", "--manifest", str(self.manifest), "--plugin", "pub", "--skill", "third"]
        out = io.StringIO()
        self.assertEqual(agent_env.main(args + ["--write-license"], out=out), 0, out.getvalue())
        self.assertIn("无需更新", out.getvalue())
        self.assertEqual((self.licenses / "someone-skills.LICENSE").read_text(), "Upstream MIT\n")
        with open(self.skills / "third" / "SKILL.md", "a", encoding="utf-8") as fh:
            fh.write("my own line\n")
        out = io.StringIO()
        self.assertEqual(agent_env.main(args, out=out), 1)
        self.assertIn("+my own line", out.getvalue())
        self.assertIn("本机副本改过", out.getvalue())
        self.assertNotIn("agent-env-vendor-", out.getvalue())  # the temporary clone's path is not shown


if __name__ == "__main__":
    unittest.main()
