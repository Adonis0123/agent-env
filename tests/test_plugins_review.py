"""Regression tests for the two review rounds of the plugin design: one class per finding (F<n> from
round 1, R2<letter> from round 2), written from the reviewer's inputs. The class of each leak is covered,
not only the quoted input."""
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import collation  # noqa: E402
import evals  # noqa: E402
import habits  # noqa: E402
import habits_check  # noqa: E402
import issues  # noqa: E402
import plugins  # noqa: E402
import remote  # noqa: E402
import render  # noqa: E402
from test_plugins import DEMO_MANIFEST, PERSONAL, PUBLIC, REPO, PluginBase  # noqa: E402

FAKE_TOKEN = "ghp_" + "R" * 36  # the shape of a GitHub token; not a real one
VALUE = "abcdefghijklmnopqrstuvwxyz1234"  # 30 characters a credential could be made of
REAL_GITLEAKS_HITS = plugins.gitleaks_hits  # PluginBase replaces it with "installed, finds nothing"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xd8"
LOCAL_ENV = {"test": {"platform": "nomatch"}}


class ReviewBase(PluginBase):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = agent_env.main([argv[0], *argv[1:], "--manifest", str(self.manifest)], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def one_plugin(self, vis, **extra):
        """A manifest whose only plugin `pub` (with the own skill) has visibility `vis`."""
        return self.setup({"pub": dict(PUBLIC, visibility=vis, **extra), "per": PERSONAL},
                          targets=[self.target([], plugins=["pub"])], environments=LOCAL_ENV)

    def refusal(self, vis, **extra):
        """The build refusal for visibility `vis` ("" when the plugin builds)."""
        m = self.one_plugin(vis, **extra)
        try:
            plugins.render_plugin(m, "pub", self.rules())
        except plugins.PluginError as exc:
            self.assertIn("修复：", str(exc))
            return str(exc)
        return ""

    def body(self, vis, text):
        self.skill("own-skill", "Does its own thing.", body=text)
        return self.refusal(vis)

    def leftovers(self, dest):
        return sorted(p.name for p in dest.parent.iterdir() if p.name.startswith(f".{dest.name}.")
                      and not p.name.endswith(".lock"))


class F1SwapTests(ReviewBase):
    """P1-1: a failed rename lost the output dir, or restored it without `.git`."""

    def built_repo(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        _, dest, _, _ = plugins.build(m, "pub", self.rules())
        (dest / ".git").mkdir()
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        before = plugins.tree_files(dest)
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "shared body v2"), encoding="utf-8")
        return m, dest, before

    def test_f1_a_failed_rename_at_any_step_keeps_the_output_and_its_git(self):
        m, dest, before = self.built_repo()
        real = os.rename
        for fail_at in (1, 2, 3):  # set the old output aside / move the new tree in / move .git over
            calls = {"n": 0}

            def flaky(src, dst, fail_at=fail_at, calls=calls):
                calls["n"] += 1
                if calls["n"] == fail_at:
                    raise OSError(f"rename {fail_at} failed")
                return real(src, dst)
            with self.subTest(fail_at=fail_at):
                with mock.patch.object(plugins.os, "rename", flaky), self.assertRaises(OSError):
                    plugins.build(m, "pub", self.rules())
                self.assertEqual(calls["n"] >= fail_at, True, "the failing rename was reached")
                self.assertTrue(dest.is_dir(), "the output dir is still there")
                self.assertEqual(plugins.tree_files(dest), before, "the old output is intact")
                self.assertEqual((dest / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n", ".git is intact")
                self.assertEqual(self.leftovers(dest), [], "no temp or set-aside dir left")
        self.assertTrue(plugins.build(m, "pub", self.rules())[0])
        self.assertIn(b"shared body v2", plugins.tree_files(dest)["AGENTS.md"][0])
        self.assertEqual((dest / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual(self.leftovers(dest), [])

    def test_f1_killed_after_the_old_output_was_set_aside(self):
        """The real order: the old output (with .git) is aside, the new tree still in its temp dir."""
        m, dest, before = self.built_repo()
        new = plugins.render_plugin(m, "pub", self.rules())
        tmp = dest.parent / f".{dest.name}.tmp-dead1"
        for rel, (data, exe) in new.items():
            plugins._write_file(tmp / rel, data, exe)
        os.rename(dest, dest.parent / f".{dest.name}.old-dead1")
        with plugins.output_lock(dest):
            notes = plugins.recover(dest)
        self.assertTrue(any("放回" in n for n in notes), notes)
        self.assertEqual(plugins.tree_files(dest), before)
        self.assertEqual((dest / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual(self.leftovers(dest), [])

    def test_f1_killed_after_the_new_tree_landed_but_before_git_moved(self):
        m, dest, _ = self.built_repo()
        new = plugins.render_plugin(m, "pub", self.rules())
        old = dest.parent / f".{dest.name}.old-dead2"
        os.rename(dest, old)  # .git is still inside the set-aside old output
        for rel, (data, exe) in new.items():
            plugins._write_file(dest / rel, data, exe)
        changed, _, _, notes = plugins.build(m, "pub", self.rules())
        self.assertFalse(changed, "the new tree had landed and is current")
        self.assertTrue(any(".git" in n for n in notes), notes)
        self.assertEqual((dest / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual(self.leftovers(dest), [])


class F2LockTests(ReviewBase):
    """P1-2: a second build's recover() deleted the first build's live temp dir."""

    def test_f2_a_second_build_fails_fast_and_touches_nothing(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        dest = plugins.output_dir(m, "pub")
        dest.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(  # another process in the middle of a build of the same output
            [sys.executable, "-c", "import fcntl, os, sys\n"
                                   "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
                                   "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()\n",
             str(plugins.lock_path(dest))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            live = dest.parent / f".{dest.name}.tmp-live"
            (live / "skills").mkdir(parents=True)
            (live / "AGENTS.md").write_text("half written\n")
            with self.assertRaisesRegex(plugins.PluginError, "另一个 agent-env 进程正在生成.*修复：等"):
                plugins.build(m, "pub", self.rules())
            with self.assertRaisesRegex(plugins.PluginError, "另一个 agent-env 进程正在生成.*修复"):
                plugins.publish(m, ["pub"], self.rules(), io.StringIO())
            rc, _, err = self.cli("plugin", "build", "--all")
            self.assertEqual(rc, 1)
            self.assertIn("修复", err)
            self.assertEqual((live / "AGENTS.md").read_text(), "half written\n", "the other build's temp dir is untouched")
            self.assertFalse(dest.exists())
        finally:
            holder.stdin.close()
            holder.stdout.close()
            holder.wait(timeout=10)
        changed, _, _, notes = plugins.build(m, "pub", self.rules())  # the lock is free: leftovers are ours now
        self.assertTrue(changed)
        self.assertFalse(live.exists())
        self.assertTrue(any("清理" in n for n in notes), notes)

    def test_f2_doctor_never_cleans_up(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        dest = plugins.output_dir(m, "pub")
        live = dest.parent / f".{dest.name}.tmp-live"
        live.mkdir(parents=True)
        self.doctor("cloud")
        self.assertTrue(live.is_dir())

    def test_f2_each_build_gets_its_own_temp_name(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        seen = []
        real = plugins._swap
        with mock.patch.object(plugins, "_swap", lambda dest, tmp: (seen.append(tmp.name), real(dest, tmp))[1]):
            plugins.build(m, "pub", self.rules())
            (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "v2"), encoding="utf-8")
            plugins.build(m, "pub", self.rules())
        self.assertEqual(len(set(seen)), 2, seen)


class F3GateTests(ReviewBase):
    """P1-3: `plugin build` skipped manifest and heading validation."""

    def test_f3_cli_refuses_a_public_plugin_that_selects_a_private_section(self):
        m = self.setup({"pub": dict(PUBLIC, sections=["Cli only"]), "per": PERSONAL})
        for argv in (("plugin", "build", "--all"), ("plugin", "build", "--plugin", "pub"),
                     ("plugin", "publish", "--plugin", "pub"), ("plugin", "pointer", "--target", "cloud/bot"),
                     ("plugin", "build", "--plugin", "per")):  # nothing is generated from a failing manifest
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(out, "")
                self.assertIn("没有登记为 public", err)
                self.assertIn("修复：", err)
        self.assertFalse(plugins.output_dir(m, "pub").exists())
        self.assertFalse(plugins.output_dir(m, "per").exists())
        self.assertFalse(plugins.ledger_path().exists())
        self.assertFalse(agent_env.outbox_path("cloud/bot").exists())
        with self.assertRaisesRegex(plugins.PluginError, "没过校验.*修复"):  # the library refuses too
            plugins.build(m, "pub", self.rules())
        with self.assertRaisesRegex(plugins.PluginError, "没过校验.*修复"):
            plugins.publish(m, ["pub"], self.rules(), io.StringIO())
        self.assertEqual({c["name"]: c["state"] for c in self.cells(self.doctor("cloud"), "cloud/bot")}["插件 pub（公开）"],
                         "drift")

    def test_f3_a_tab_heading_is_its_own_section_and_blocks_the_build(self):
        rules = "# Title\n\nIntro line.\n\n## Shared\n\nshared body\n\n##\tLocal\n\nlocal only body\n\n## Cli only\n\ncli body\n"
        (self.root / "AGENTS.md").write_text(rules, encoding="utf-8")
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        self.assertEqual([h for h, _ in habits.parse_sections(rules)[1]], ["Shared", "Local", "Cli only"])
        self.assertNotIn("local only body", dict(habits.parse_sections(rules)[1])["Shared"])
        self.assertNotIn("local only body", plugins.agents_md(m, "pub", rules), "an unassigned section never rides along")
        with self.assertRaisesRegex(plugins.PluginError, "疑似未识别的标题.*修复"):
            plugins.render_plugin(m, "pub", rules)
        rc, out, err = self.cli("plugin", "build", "--all")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("修复：", err)
        self.assertFalse(plugins.output_dir(m, "pub").exists())

    def test_f3_every_low_heading_form_ends_the_section_before_it(self):
        for heading in ("##\tLocal", "   ## Local", "##\u00a0Local", "##\u3000Local", "##", "## Local ##", "# Local",
                        "#\tLocal", "  #  Local"):
            with self.subTest(heading=heading):
                rules = f"## Shared\n\nshared body\n\n{heading}\n\nlocal only body\n"
                sections = habits.parse_sections(rules)[1]
                self.assertEqual(len(sections), 2, sections)
                self.assertNotIn("local only body", sections[0][1])
        # a setext heading cannot be split by line, so it must stop the build
        rules = "# Title\n\nIntro line.\n\n## Shared\n\nshared body\n\nLocal\n-----\n\nlocal only body\n\n## Cli only\n\ncli body\n"
        (self.root / "AGENTS.md").write_text(rules, encoding="utf-8")
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        with self.assertRaisesRegex(plugins.PluginError, "疑似未识别的标题.*修复"):
            plugins.render_plugin(m, "pub", rules)
        # headings inside a code fence and deeper headings stay inside their section
        rules = "## Shared\n\n```md\n## Not a heading\n```\n\n### Detail\n\nshared body\n"
        self.assertEqual([h for h, _ in habits.parse_sections(rules)[1]], ["Shared"])


class F4RulesFileTests(ReviewBase):
    """P1-4: a public plugin pulled a section registered private through `rules_file`."""

    def test_f4_public_plugin_cannot_use_rules_file_at_all(self):
        private = self.root / "extra.md"
        private.write_text("## Cli only\n\ncli body\n", encoding="utf-8")
        for path in (self.root / "AGENTS.md", private):
            with self.subTest(path=path.name):
                m = self.setup({"pub": dict(PUBLIC, rules_file=str(path)), "per": PERSONAL})
                self.assertTrue(any("公开插件却写了 rules_file" in f for f in self.fails()), self.fails())
                with self.assertRaisesRegex(plugins.PluginError, "rules_file.*修复"):
                    plugins.render_plugin(m, "pub", self.rules())
                rc, out, _ = self.cli("plugin", "build", "--plugin", "pub")
                self.assertEqual((rc, out), (1, ""))
                self.assertFalse(plugins.output_dir(m, "pub").exists())

    def test_f4_pushed_plugins_cannot_carry_registered_sections_through_rules_file(self):
        extra = self.root / "extra.md"
        extra.write_text("## Shared\n\nmy own take\n", encoding="utf-8")
        for vis in ("personal", "company"):
            for path, why in ((self.root / "AGENTS.md", "规则源本身"), (extra, "已登记的共享章节「Shared」")):
                with self.subTest(vis=vis, path=path.name):
                    m = self.setup({"pub": PUBLIC, "per": {"visibility": vis, "rules_file": str(path)}},
                                   targets=[self.target([], plugins=["pub"])], environments=LOCAL_ENV)
                    self.assertTrue(any(why in f for f in self.fails()), self.fails())
                    with self.assertRaisesRegex(plugins.PluginError, "没过校验"):
                        plugins.render_plugin(m, "per", self.rules())

    def test_f4_a_plugins_own_section_and_the_patch_layer_still_work(self):
        own = self.root / "own.md"
        own.write_text("## Bot notes\n\nnotes for the bot\n", encoding="utf-8")
        m = self.setup({"pub": PUBLIC, "per": dict(PERSONAL, rules_file=str(own))})
        self.assertEqual(self.fails(), [])
        self.assertIn(b"notes for the bot", plugins.render_plugin(m, "per", self.rules())["AGENTS.md"][0])
        m = self.setup({"pub": PUBLIC, "per": PERSONAL, "patch": {"rules_file": str(self.root / "AGENTS.md")}},
                       targets=[self.target([], plugins=["pub", "patch"])], environments=LOCAL_ENV)
        self.assertEqual(self.fails(), [], "a local plugin never leaves this machine")

    def test_f4_company_skills_stay_out_of_public_and_personal_plugins(self):
        company = self.root / "company-skills"
        (company / "own-skill").mkdir(parents=True)
        for vis, refused in (("public", True), ("personal", True), ("company", False), ("local", False)):
            with self.subTest(vis=vis):
                m = self.one_plugin(vis)
                m["sources"]["company_skills"] = str(company)
                errs = [msg for msg, _ in plugins.plugin_errors(m, "pub", m["plugins"]["pub"])]
                self.assertEqual(any("是公司 skill" in e for e in errs), refused, errs)
                if refused:
                    with self.assertRaisesRegex(plugins.PluginError, "公司 skill.*修复"):
                        plugins.render_plugin(m, "pub", self.rules())


class F5CredentialTests(ReviewBase):
    """P1-5: an own skill's `.env` was copied; unquoted and multi-line secrets passed the scan."""

    STORES = (".env", ".env.local", ".ENV", "prod.env", "auth.json", "server.pem", "tls.key", "id_rsa", "id_rsa.pub",
              "id_ed25519", ".npmrc", ".netrc", ".pypirc", ".git-credentials", "credentials.json",
              "sub/.env", ".aws/credentials", ".env.d/values.txt")

    def test_f5_credential_store_files_are_never_copied(self):
        d = self.skills / "own-skill"
        for rel in self.STORES:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("HARMLESS=1\n")  # left out by name, whatever the content
        (d / "scripts").mkdir()
        (d / "scripts" / "run.sh").write_text("echo ok\n")
        for vis in ("public", "personal", "company", "local"):
            with self.subTest(vis=vis):
                m = self.one_plugin(vis)
                files = plugins.render_plugin(m, "pub", self.rules())
                copied = sorted(rel for rel in files if rel.startswith("skills/own-skill/"))
                self.assertEqual(copied, ["skills/own-skill/SKILL.md", "skills/own-skill/scripts/run.sh"])
        _, dest, _, notes = plugins.build(m, "pub", self.rules())
        self.assertTrue(any("没有复制凭证文件" in n and "own-skill/.env" in n for n in notes), notes)
        self.assertFalse((dest / "skills" / "own-skill" / ".env").exists())

    def test_f5_a_secret_in_env_never_reaches_a_plugin(self):
        (self.skills / "own-skill" / ".env").write_text(f"API_KEY={FAKE_TOKEN}\n")
        m = self.one_plugin("public")
        files = plugins.render_plugin(m, "pub", self.rules())
        self.assertFalse(any(FAKE_TOKEN.encode() in data for data, _ in files.values()))

    def test_f5_unquoted_and_multiline_secrets_are_caught(self):
        value = "abcdefghijklmnopqrstuvwxyz1234"  # 30 characters
        bodies = (f"API_KEY={value}\n", f"aws_secret_access_key = {value}\n", f"  password: {value}\n",
                  f"export GITHUB_TOKEN={value}\n", f"client-secret:{value}\n",
                  f"password: |\n  {value}\n", f"api_key: >-\n  {value[:15]}\n  {value[15:]}\n",
                  f"client_secret:\n  {value}\n", f'token: "{value[:15]}\n  {value[15:]}"\n',
                  f"export SECRET_KEY={value[:10]}\\\n{value[10:]}\n",
                  f"private_key: |+\n    {value[:15]}\n    {value[15:]}\n",
                  f"令牌{FAKE_TOKEN}\n", f"x_{FAKE_TOKEN}\n", "github_pat_" + "A1b2" * 10 + "\n")
        for text in bodies:
            for vis in ("public", "personal", "company"):
                with self.subTest(text=text, vis=vis):
                    self.assertIn("疑似密钥", self.body(vis, text))

    def test_f5_text_about_secrets_still_passes(self):
        for text in ("token: read it from the GITHUB_TOKEN environment variable\n", "api_key: $API_KEY\n",
                     "password:\n  min_length: 12\n  rotate: true\n", "API_KEY=<your key>\n",
                     "The token lives in the secret card; never paste its value.\n"):
            with self.subTest(text=text):
                self.assertEqual(self.body("public", text), "")


class F6BinaryTests(ReviewBase):
    """P1-6: a non-UTF-8 file skipped the scan and carried a fake GitHub token into a public plugin."""

    def put(self, name, data):
        (self.skills / "own-skill" / name).write_bytes(data)

    def test_f6_a_token_inside_a_binary_is_found(self):
        token = FAKE_TOKEN.encode()
        for data in (b"\xff\xfe\x00\x01" + token + b"\xff\xff", b"\xc3" + token + b"\xe9",  # glued to high bytes
                     ("\ufeff" + FAKE_TOKEN).encode("utf-16")):
            self.put("blob.bin", data)
            for vis in ("public", "personal", "company"):
                with self.subTest(vis=vis, data=data[:6]):
                    self.assertIn("疑似密钥", self.refusal(vis))

    def test_f6_unknown_binaries_are_refused_and_only_images_are_allowed(self):
        self.put("blob.bin", b"\xff\x00\x01\x02 no secret here")
        for vis, refused in (("public", True), ("personal", True), ("company", False), ("local", False)):
            with self.subTest(vis=vis):
                msg = self.refusal(vis)
                self.assertEqual("blob.bin 不是 UTF-8 文本" in msg, refused, msg)
        os.remove(self.skills / "own-skill" / "blob.bin")
        self.put("nul.txt", b"text with a NUL\x00 byte\n")
        self.assertIn("nul.txt 不是 UTF-8 文本", self.refusal("public"))
        os.remove(self.skills / "own-skill" / "nul.txt")
        self.put("logo.png", b"\x89PNG\r\n\x1a\n\xff\xd8\x00\x00")
        self.assertEqual(self.refusal("public"), "", "an image is on the allowlist")
        self.put("logo.png", b"\x89PNG\r\n\x1a\n\xff" + FAKE_TOKEN.encode() + b"\xff")
        self.assertIn("疑似密钥", self.refusal("public"), "an allowed image is still scanned")

    def test_f6_file_names_are_checked_like_content(self):
        self.put(f"{FAKE_TOKEN}.md", b"x\n")
        self.assertIn("文件名疑似密钥", self.refusal("public"))


class F7NoEchoTests(ReviewBase):
    """P1-7: a value filled into `token.value` was printed by the validation error."""

    def everything(self):
        """All text a user (or a log) can see for the current manifest."""
        results = habits_check.run_checks(self.manifest).results
        status = self.doctor("cloud")
        rc, out, err = self.cli("plugin", "pointer", "--target", "cloud/bot")
        rc2, out2, err2 = self.cli("plugin", "build", "--all")
        return results, json.dumps([results, status], ensure_ascii=False) + agent_env.format_doctor(status) \
            + out + err + out2 + err2

    def test_f7_a_token_value_is_never_echoed(self):
        for token in ({"name": "T", "value": FAKE_TOKEN, "expires": "2099-01-01"}, FAKE_TOKEN,
                      {"name": FAKE_TOKEN, "expires": "2099-01-01"}, {"name": "T", "expires": FAKE_TOKEN},
                      {"name": "T", "expires": "2099-01-01", FAKE_TOKEN: "x"}):
            with self.subTest(token=str(token)[:24]):
                self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], token=token)])
                results, seen = self.everything()
                self.assertNotIn(FAKE_TOKEN, seen)
                fails = [r for r in results if r["level"] == "FAIL"]
                self.assertTrue(any("token" in r["message"] for r in fails), fails)
                self.assertTrue(all(r["fix"] for r in fails))
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], token={"name": "T", "value": FAKE_TOKEN})])
        self.assertTrue(any("token 多了键 ['value']" in f for f in self.fails()), "the key is named, the value is not")

    def test_f7_plugin_fields_do_not_echo_values_either(self):
        for extra in ({"env": [f"GITHUB_TOKEN={FAKE_TOKEN}"]}, {"repo": f"https://user:{FAKE_TOKEN}@github.com/o/r"},
                      {"repo": f"https://github.com/o/r?token={FAKE_TOKEN}"}, {"token": FAKE_TOKEN},
                      {"description": f"uses {FAKE_TOKEN}"}, {"sections": ["Shared", FAKE_TOKEN]}):
            with self.subTest(extra=list(extra)):
                self.setup({"pub": dict(PUBLIC, **extra), "per": PERSONAL})
                results, seen = self.everything()
                self.assertNotIn(FAKE_TOKEN, seen)
                self.assertTrue(any(r["level"] == "FAIL" and "[plugins.pub]" in r["message"] for r in results), results)


class F8OldRendererTests(ReviewBase):
    """P1-8: `render --target grok-bot/bot` still printed the composed rules, local-only sections included."""

    def test_f8_no_old_text_path_for_a_plugin_target(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL})
        for argv in (("render", "--target", "cloud/bot"), ("render", "--target", "cloud/bot", "--check"),
                     ("deliver", "--target", "cloud/bot"), ("deliver", "--target", "cloud/bot", "--record")):
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")
                self.assertIn("plugin pointer --target cloud/bot", err)
                self.assertNotIn("cli body", out + err)
                self.assertNotIn("shared body", out + err)
        for fn in (render.compose, render.render_rules):
            with self.assertRaisesRegex(render.RenderError, "用插件.*修复"):
                fn(m, self.rules(), "cloud/bot")
        text = agent_env.delivery_text(m, "cloud/bot", self.rules())
        self.assertLessEqual(len(text.rstrip("\n")), 300)
        self.assertNotIn("body", text)

    def test_f8_a_plugin_target_cannot_keep_old_renderer_fields(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], render="generated", output="~/x.md")])
        self.assertTrue(any("按角色生成单份规则的字段 ['render', 'output']" in f for f in self.fails()), self.fails())

    def test_f8_the_demo_grok_bot_target_is_a_plugin_target(self):
        with open(DEMO_MANIFEST, "rb") as fh:
            m = tomllib.load(fh)
        with self.assertRaisesRegex(render.RenderError, "plugin pointer --target grok-bot/bot"):
            render.compose(m, "## On-demand references\n\nlocal only\n", "grok-bot/bot")


class F9HomePathTests(ReviewBase):
    """P2-9: `C:\\Users\\reviewer\\private` and `~reviewer/private` were not caught."""

    def test_f9_home_paths_in_every_spelling(self):
        for text in ("C:\\Users\\reviewer\\private", "~reviewer/private", "c:/users/reviewer/notes",
                     'path = "C:\\\\Users\\\\reviewer"', "/mnt/c/Users/you/x", "file:///Users/you/x",
                     "\\\\nas\\Users\\reviewer", "D:\\Documents and Settings\\reviewer", "/root/.ssh/config",
                     "/Users/you", "cd /home/reviewer/x", "~reviewer\\private"):
            for vis in ("public", "personal"):
                with self.subTest(text=text, vis=vis):
                    self.assertTrue(plugins.home_path(text))
                    self.assertIn("绝对家目录路径", self.body(vis, f"see {text}\n"))

    def test_f9_placeholders_and_urls_pass(self):
        for text in ("~/coding", "$HOME/x", "/home/<user>/x", "/Users/<name>/x", "%USERPROFILE%\\notes",
                     "https://example.com/home/page", "https://github.com/Users/you", "~~struck out~~", "~5 minutes",
                     "docs/root/index.md", "C:\\Program Files\\tool"):
            with self.subTest(text=text):
                self.assertFalse(plugins.home_path(text))
        self.assertEqual(self.body("public", "see ~/coding and $HOME/x, docs at https://example.com/home/page\n"), "")


class F10CollationTests(ReviewBase):
    """P2-10: file order differed from skills 1.7.6 (`a b.md` vs `az.md`, accented names)."""

    NAMES = ("SKILL.md", "a b.md", "az.md", "a-b.md", "a_b.md", "a.md", "ab.md", "B.md", "b/c.md", "b c/d.md", "bc.md",
             "é.md", "e.md", "f.md", "Ä.md", "ä.md", "Z9.txt", "10.md", "9.md", "résumé.md", "resume.md", "Resume.md",
             "ñandú.md", "nz.md", "œuvre.md", "of.md", "straße.md", "strasse.md", "x y/z w.md")

    def test_f10_the_reviewers_cases_without_node(self):
        order = lambda names: sorted(names, key=collation.sort_key)  # noqa: E731
        self.assertEqual(order(["az.md", "a b.md"]), ["a b.md", "az.md"], "a space sorts before any letter")
        self.assertEqual(order(["f.md", "é.md", "e.md"]), ["e.md", "é.md", "f.md"], "an accent is a secondary difference")
        self.assertEqual(order(["B.md", "a.md", "A.md", "b.md"]), ["a.md", "A.md", "b.md", "B.md"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_f10_order_and_hash_match_node(self):
        d = self.skills / "accents"
        for rel in self.NAMES:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(rel, encoding="utf-8")
        js = """
import { createHash } from "node:crypto"; import { readdir, readFile } from "node:fs/promises"; import { join, relative } from "node:path";
async function collect(base, cur, out) { const es = await readdir(cur, { withFileTypes: true }); await Promise.all(es.map(async (e) => {
  const f = join(cur, e.name); if (e.isDirectory()) { if (e.name === ".git" || e.name === "node_modules") return; await collect(base, f, out); }
  else if (e.isFile()) out.push({ r: relative(base, f).split("\\\\").join("/"), c: await readFile(f) }); })); }
const fs = []; await collect(process.argv[1], process.argv[1], fs); fs.sort((a, b) => a.r.localeCompare(b.r, "en-US"));
const h = createHash("sha256"); for (const f of fs) { h.update(f.r); h.update(f.c); }
console.log(JSON.stringify({ hash: h.digest("hex"), order: fs.map((f) => f.r) }));
"""
        node = json.loads(subprocess.run(["node", "--input-type=module", "-e", js, str(d)], capture_output=True, text=True,
                                         check=True).stdout)
        mine = sorted((p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()), key=collation.sort_key)
        self.assertEqual(mine, node["order"])
        self.assertEqual(plugins.skill_folder_hash(d), node["hash"])


class F11DescriptionTests(ReviewBase):
    """P2-11: a `>+` block scalar or a multi-line quoted description counted as 2 or 5 characters."""

    def write(self, name, front):
        d = self.skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n{front}---\nbody\n", encoding="utf-8")
        return d

    def over(self, d):
        return [m for m, _ in plugins.skill_limit_errors(d) if "超过 1024" in m]

    def test_f11_every_yaml_spelling_of_a_long_description_is_counted(self):
        long = "d" * 600
        fronts = {
            "block-keep": f"description: >+\n  {long}\n  {long}\n",
            "block-strip": f"description: |-\n  {long}\n  {long}\n",
            "block-indent": f"description: >2-\n  {long}\n  {long}\n",
            "double": f'description: "{long}\n  {long}"\n',
            "single": f"description: '{long}\n  {long}'\n",
            "plain": f"description: {long}\n  {long}\n",
        }
        for name, front in fronts.items():
            with self.subTest(name=name):
                errs = self.over(self.write(name, front))
                self.assertEqual(len(errs), 1, plugins.skill_limit_errors(self.skills / name))
                self.assertIn("1201", errs[0])  # 600 + a space or a line break + 600

    def test_f11_at_the_limit_passes_and_broken_front_matter_fails_closed(self):
        self.assertEqual(plugins.skill_limit_errors(self.write("fits", f"description: >-\n  {'d' * 1024}\n")), [])
        errs = plugins.skill_limit_errors(self.write("broken", 'description: "never closed\n'))
        self.assertTrue(any("front matter 这里不认" in m for m, _ in errs), errs)
        errs = plugins.skill_limit_errors(self.write("longname", "description: x\n").parent / "longname")
        self.assertEqual(errs, [])
        d = self.write("n" * 65, "description: x\n")
        self.assertTrue(any("超过 64" in m for m, _ in plugins.skill_limit_errors(d)))

    def test_f11_build_refuses_the_reviewers_skill(self):
        self.write("own-skill", f"description: >+\n  {'d' * 1100}\n")
        m = self.one_plugin("public")
        with self.assertRaisesRegex(plugins.PluginError, "超过 1024.*修复"):
            plugins.render_plugin(m, "pub", self.rules())
        self.assertTrue(any("超过 1024" in f for f in self.fails()))


class F12OnePointerPathTests(ReviewBase):
    """P2-12: `deliver --record` recorded a pointer that still had repository placeholders."""

    def test_f12_deliver_cannot_record_a_plugin_targets_pointer(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL})  # no repo yet: placeholders
        self.cli("plugin", "pointer", "--target", "cloud/bot")
        for argv in (("deliver", "--target", "cloud/bot", "--record"), ("plugin", "pointer", "--target", "cloud/bot", "--record")):
            rc, out, err = self.cli(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("修复", err)
        self.assertFalse(remote.delivery_path("cloud/bot").exists(), "nothing was recorded")
        self.setup({"pub": dict(PUBLIC, repo="https://github.com/someone/agent-public"),
                    "per": dict(PERSONAL, repo="https://github.com/someone/agent-personal")},
                   [self.bot(["pub", "per"], token={"name": "BOT_TOKEN", "expires": "2099-01-01"})])
        self.cli("plugin", "pointer", "--target", "cloud/bot")
        rc, _, err = self.cli("deliver", "--target", "cloud/bot", "--record")
        self.assertEqual(rc, 1, "with real URLs too, deliver is not a second way in")
        self.assertIn("plugin pointer --target cloud/bot --record", err)
        self.assertFalse(remote.delivery_path("cloud/bot").exists())
        self.assertEqual(self.cli("plugin", "pointer", "--target", "cloud/bot", "--record")[0], 0)
        self.assertTrue(remote.delivery_path("cloud/bot").exists())


class F13EvalEnvTests(ReviewBase):
    """P2-13: `eval --target grok-bot/bot` run on the Mac stored its result in eval/mac.json."""

    ENVS = {"test": {"platform": sys.platform}, "other": {"platform": "nomatch"},
            "cloud": {"transport": "cloud-hosted", "driven_from": "test", "deny_visibility": ["company"]}}

    def test_f13_results_are_stored_with_the_targets_environment(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL},
                   [self.bot(["pub", "per"], eval_skip="云端托管：没有本机无头入口"),
                    self.target([], id="other/host", env="other", host="hermes", eval_cases=["bot-chinese-reply"])],
                   environments=self.ENVS)
        rc, out, err = self.cli("eval", "--target", "cloud/bot")  # this machine is `test`
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(evals.eval_path("test").exists(), "not stored under the machine that ran the command")
        stored = json.loads(evals.eval_path("cloud").read_text())
        self.assertEqual([(r["target"], r["state"]) for r in stored["results"]], [("cloud/bot", "skip")])
        self.assertEqual(stored["env"], "cloud")
        self.assertEqual([r["target"] for r in self.doctor("cloud")["eval"]["results"]], ["cloud/bot"])
        rc, _, err = self.cli("eval", "--target", "cloud/bot", "--env", "test")
        self.assertEqual(rc, 1)
        self.assertIn("修复", err)
        rc, _, err = self.cli("eval", "--target", "other/host")  # another machine's target cannot be run here
        self.assertEqual(rc, 1)
        self.assertIn("到 other 上跑", err)
        self.assertFalse(evals.eval_path("other").exists())


def real_manifest() -> dict:
    """The demo manifest: the panel fixtures show its environments (examples/demo)."""
    with open(DEMO_MANIFEST, "rb") as fh:
        return tomllib.load(fh)


class F14DrivingMachineTests(unittest.TestCase):
    """P2-14: an unreported or stale Grok Bot told the user to run `status` on Grok Bot itself."""

    def check(self, issue):
        steps = issue["steps"]
        self.assertIn("Mac", issue["title_zh"])
        self.assertEqual(issue["actor"], "other_env")
        self.assertEqual([s["command"] for s in steps], ["bin/agent-env status --env grok-bot", None,
                                                         "bin/agent-env status --json --all --fetch"])
        self.assertIn("在 Mac 上", steps[0]["text_zh"])
        self.assertIn("在 Mac 上运行 bin/agent-env status --env grok-bot --commit --push", steps[1]["text_zh"])
        self.assertTrue(steps[1]["confirm"])
        self.assertFalse(any("在 Grok Bot 上" in s["text_zh"] for s in steps))

    def test_f14_unreported_cloud_env_names_the_machine_that_drives_it(self):
        m = real_manifest()
        found = issues.build([], [{"env": "grok-bot", "message": "读不到 status/grok-bot 分支", "fix": ""}], m)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title_zh"], "Mac 还没上报 Grok Bot 的检查结果")
        self.check(found[0])

    def test_f14_stale_cloud_env_names_the_machine_that_drives_it(self):
        m = real_manifest()
        stale = {"env": "grok-bot", "source": "branch", "checked_from": "mac", "generated_at": "2026-10-01T00:00:00Z",
                 "targets": []}
        found = issues.build([stale], [], m, now=datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.assertEqual(found[0]["title_zh"], "Mac 已经 40 天没上报 Grok Bot 的检查结果")
        self.check(found[0])

    def test_f14_a_real_machine_keeps_its_own_steps(self):
        found = issues.build([], [{"env": "wsl", "message": "读不到 status/wsl 分支", "fix": ""}], real_manifest())
        self.assertEqual(found[0]["steps"][0]["command"], "bin/agent-env status")
        self.assertIn("在 WSL 上", found[0]["steps"][0]["text_zh"])


class F16TokenDateTests(ReviewBase):
    """P2-16: `expires = 2026-11-10T23:59:59Z` lost its time and zone and was judged expired early."""

    def token_cell(self):
        return {c["name"]: c for c in self.cells(self.doctor("cloud"), "cloud/bot")}["插件 token"]

    def test_f16_a_datetime_is_rejected_with_a_fix(self):
        for raw in ("2026-11-10T23:59:59Z", "2026-11-10T23:59:59+08:00", "2026-11-10T23:59:59", '"2026-11-10T23:59:59Z"'):
            with self.subTest(raw=raw):
                self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], token={"name": "T", "expires": "@@"})])
                self.manifest.write_text(self.manifest.read_text().replace('"@@"', raw), encoding="utf-8")
                results = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
                self.assertTrue(any("token.expires" in r["message"] and "2027-02-08" in r["fix"] for r in results), results)
                cell = self.token_cell()
                self.assertEqual(cell["state"], "drift")
                self.assertNotIn("已于", cell["detail"], "it is not judged expired; the entry itself is wrong")
        self.assertIsNone(plugins.parse_date(datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc)))
        self.assertEqual(plugins.parse_date(date(2026, 11, 10)), date(2026, 11, 10))
        self.assertEqual(plugins.parse_date("2026-11-10"), date(2026, 11, 10))

    def test_f16_a_date_expires_at_the_start_of_its_day(self):
        m = self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.bot(["pub", "per"], token={"name": "T", "expires": "@@"})])
        self.manifest.write_text(self.manifest.read_text().replace('"@@"', "2026-11-10"), encoding="utf-8")
        m = habits.load_manifest(self.manifest)
        self.assertEqual(self.fails(), [])
        bot = habits.find_target(m, "cloud/bot")
        self.assertEqual(plugins.token_state(bot, m, date(2026, 11, 10))[0], "drift")
        state, detail, _, warn = plugins.token_state(bot, m, date(2026, 11, 9))
        self.assertEqual((state, warn), ("synced", True))
        self.assertIn("还有 1 天", detail)


class F17LeftoverEvalTests(ReviewBase):
    """Panel-17: a leftover eval record `mac/grok-bot` made an empty "Grok Bot" row in the Mac box."""

    def test_f17_records_of_targets_the_manifest_no_longer_has_are_ignored(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.target([]), self.bot(["pub", "per"])])
        row = lambda target, state: {"target": target, "case": "-", "state": state, "runs": 0, "passed": 0,  # noqa: E731
                                     "detail": "云端托管：没有本机无头入口"}
        path = evals.eval_path("test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "env": "test", "ran_at": "2026-11-01T00:00:00Z", "results": [
            row("test/grok-bot", "skip"), row("test/host", "pass"), row("test/gone", "fail")]}), encoding="utf-8")
        before = path.read_bytes()
        status = self.doctor("test")
        self.assertEqual([r["target"] for r in status["eval"]["results"]], ["test/host"])
        self.assertEqual(status["eval"]["ignored_targets"], ["test/gone", "test/grok-bot"])
        text = agent_env.format_doctor(status)
        self.assertIn("test/grok-bot", text)
        self.assertIn("已忽略", text)
        self.assertIn("记录文件没动", text)
        self.assertEqual(path.read_bytes(), before, "the stored file is not rewritten")
        all_ = agent_env.all_status(self.manifest, "test", self.repo)
        self.assertFalse(any("test/grok-bot" in json.dumps(i, ensure_ascii=False) or i.get("target") == "test/gone"
                             for i in all_["issues"]), all_["issues"])
        mac = next(e for e in all_["environments"] if e["env"] == "test")
        self.assertEqual([r["target"] for r in mac["eval"]["results"]], ["test/host"])

    def test_f17_a_report_without_leftovers_has_no_extra_field(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL}, [self.target([]), self.bot(["pub", "per"])])
        status = self.doctor("test")
        self.assertNotIn("ignored_targets", status["eval"])
        self.assertNotIn("已忽略", agent_env.format_doctor(status))


class F19F20TodoTests(unittest.TestCase):
    """Panel-19/20: the fixture todo did not match the engine, and its steps named `deliver` while the
    detail named `plugin pointer`."""

    def engine_issues(self):
        m = real_manifest()  # the demo repositories: every plugin of the pointer rule records one
        tid = "grok-bot/bot"
        fix = (f"bin/agent-env plugin pointer --target {tid} 输出指路规则，由你在该 app 里存进它的 memory 并确认后，"
               f"运行 bin/agent-env plugin pointer --target {tid} --record")
        status = {"env": "grok-bot", "source": "live", "targets": [{"id": tid, "host": "grok-bot", "env": "grok-bot", "habits": [
            {"kind": "rules", "name": "指路规则", "state": "missing", "expect_kind": "delivered", "fix": fix,
             "detail": "没有投递记录"},
            {"kind": "rules", "name": "插件 token", "state": "missing", "expect_kind": "token", "fix": "建 fine-grained token",
             "detail": "还没登记 token"},
            {"kind": "rules", "name": "插件 demo-public（公开）", "state": "drift", "expect_kind": "plugins",
             "fix": "bin/agent-env plugin build --plugin demo-public", "detail": "比源过期"}]}]}
        return {i["id"]: i for i in issues.build([status], [], m)}

    def test_f20_every_step_of_a_plugin_target_names_plugin_pointer(self):
        found = self.engine_issues()
        pointer = found["grok-bot/bot:rules:指路规则"]
        self.assertEqual(pointer["title_zh"], "Grok Bot 的指路规则还没投递")
        commands = [s["command"] for s in pointer["steps"] if s["command"]]
        self.assertEqual(commands, ["bin/agent-env plugin build --all",
                                    "bin/agent-env plugin pointer --target grok-bot/bot",
                                    "bin/agent-env plugin pointer --target grok-bot/bot --record",
                                    "bin/agent-env doctor --env grok-bot"])
        text = json.dumps(list(found.values()), ensure_ascii=False)
        self.assertNotIn("agent-env deliver", text)
        # the plugin records its repository: no precondition step, AI goes straight to the build
        self.assertEqual([s["who"] for s in pointer["steps"]], ["ai", "ai", "user", "ai", "ai"])
        self.assertNotIn("还没写 repo", json.dumps(pointer["steps"], ensure_ascii=False))
        self.assertEqual((pointer["actor"], pointer["ai_task"]), ("user", "fix"))
        token = found["grok-bot/bot:rules:插件 token"]
        self.assertEqual([(s["who"], s["command"]) for s in token["steps"]],
                         [("user", None), ("ai", "bin/agent-env doctor --env grok-bot")])
        stale = found["grok-bot/bot:rules:插件 demo-public（公开）"]
        self.assertEqual([s["command"] for s in stale["steps"]],
                         ["bin/agent-env plugin build --plugin demo-public", "bin/agent-env doctor --env grok-bot"])

    def test_f19_panel_fixtures_carry_what_the_engine_emits(self):
        want = self.engine_issues()["grok-bot/bot:rules:指路规则"]
        for name in ("v5-issues.json", "v5-real.json", "v7-relations.json"):
            with self.subTest(fixture=name):
                data = json.loads((REPO / "panel" / "fixtures" / name).read_text(encoding="utf-8"))
                todos = [i for i in data["issues"] if i.get("target") == "grok-bot/bot" and i["category"] == "habit"]
                mine = [i for i in todos if i["id"] == want["id"]]
                self.assertEqual(len(mine), 1, [i["id"] for i in todos])
                for key in ("title_zh", "impact_zh", "steps", "actor", "ai_task"):
                    self.assertEqual(mine[0][key], want[key], key)
                text = json.dumps(data, ensure_ascii=False)
                self.assertNotIn("deliver --target grok-bot/bot", text)
                self.assertNotIn("Grok Bot的", text)
                nested = [i for e in data["environments"] for i in e.get("issues", []) if i["id"] == want["id"]]
                self.assertEqual({i["title_zh"] for i in nested} - {want["title_zh"]}, set())



# ── round 2 ───────────────────────────────────────────────────────────────

class R2ASecretTests(ReviewBase):
    """A (finding 5): another spelling of a YAML secret passed. One structural rule, plus gitleaks."""

    def test_r2a_a_sensitive_key_followed_by_a_value_in_any_yaml_spelling(self):
        bodies = (
            f"api_key: &k |\n  {VALUE}\n",                              # the reviewer's anchor + block scalar
            "api_key:\n" + "\n" * 61 + f"  {VALUE}\n",                  # the reviewer's 61 blank lines
            f"password: !!str {VALUE}\n", f"token: !!binary |\n  {VALUE}\n", f"secret: &a !!str >-\n\n\n  {VALUE}\n",
            f"api_key:\n# a comment at column 0\n  {VALUE}\n", f"credentials:\n- {VALUE}\n",
            f"auth:\n  bearer: {VALUE}\n", f"Authorization: Bearer {VALUE}\n", f"private-key: |2\n    {VALUE}\n",
            "token:\n" + "".join(f"  k{i}: v\n" for i in range(90)) + f"  last: {VALUE}\n",
            f"- name: x\n  passwd: >\n\n      {VALUE[:15]}\n      {VALUE[15:]}\n",
            f'{{"name": "x", "client_secret": "{VALUE}"}}\n',
        )
        for text in bodies:
            for vis in ("public", "personal", "company"):
                with self.subTest(text=text[:40], vis=vis):
                    self.assertIn("疑似密钥", self.body(vis, text))
        self.assertEqual(self.body("local", bodies[0]), "", "a local plugin never leaves this machine")

    def test_r2a_text_about_secrets_still_passes(self):
        for text in ("author: someone-with-a-very-long-name-indeed\n", "authors:\n  - someone-with-a-very-long-name-indeed\n",
                     "token: create one at https://github.com/settings/personal-access-tokens/new\n",
                     "password:\n  min_length: 12\n  rotate: true\n", "api_key: $ANTHROPIC_API_KEY\n", "API_KEY=<your key>\n",
                     "token: read it from the GITHUB_TOKEN environment variable\n",
                     f"notes:\n  {VALUE}\n"):  # not a sensitive key
            with self.subTest(text=text):
                self.assertEqual(self.body("public", text), "")

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks not installed")
    def test_r2a_gitleaks_scans_the_staged_directory(self):
        stripe = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"  # a shape only gitleaks' rules know here
        m = self.one_plugin("public")
        dest = plugins.output_dir(m, "pub")
        for line in (f"charge with {stripe}\n", f"charge with {stripe}  # gitleaks:allow\n"):
            self.skill("own-skill", "Does its own thing.", body=line)
            self.assertEqual(plugins.privacy_hits(plugins.render_plugin(m, "pub", self.rules()), "public"), [],
                             "the built-in layer alone does not know this shape")
            with mock.patch.object(plugins, "gitleaks_hits", REAL_GITLEAKS_HITS):
                with self.assertRaisesRegex(plugins.PluginError, r"没过 gitleaks 扫描：skills/own-skill/SKILL\.md:\d+ gitleaks:.*修复") as cm:
                    plugins.build(m, "pub", self.rules())
                self.assertNotIn(stripe, str(cm.exception), "the value is never echoed")
                self.assertFalse(dest.exists(), "nothing replaced the output")
                self.assertEqual(self.leftovers(dest), [], "the staged dir is gone")
                with self.assertRaisesRegex(plugins.PluginError, "gitleaks"):
                    plugins.publish(m, ["pub"], self.rules(), io.StringIO())
                self.assertFalse((dest / ".git").exists())
        self.skill("own-skill", "Does its own thing.", body="clean\n")
        with mock.patch.object(plugins, "gitleaks_hits", REAL_GITLEAKS_HITS):
            self.assertTrue(plugins.build(m, "pub", self.rules())[0])
            self.assertEqual(plugins.build(m, "pub", self.rules())[0], False, "stable: the second run writes nothing")

    def test_r2a_without_gitleaks_public_fails_and_personal_warns(self):
        with mock.patch.object(plugins, "gitleaks_hits", REAL_GITLEAKS_HITS), \
                mock.patch.object(plugins, "gitleaks_exe", lambda: None):
            m = self.one_plugin("public")
            with self.assertRaisesRegex(plugins.PluginError, "本机没装 gitleaks.*修复：brew install gitleaks"):
                plugins.build(m, "pub", self.rules())
            self.assertFalse(plugins.output_dir(m, "pub").exists())
            self.assertEqual(self.leftovers(plugins.output_dir(m, "pub")), [])
            rc, out, err = self.cli("plugin", "build", "--all")
            self.assertEqual(rc, 1)
            self.assertIn("brew install gitleaks", err)
            for vis, warned in (("personal", True), ("company", False), ("local", False)):
                with self.subTest(vis=vis):
                    m = self.one_plugin(vis)
                    changed, _, _, notes = plugins.build(m, "pub", self.rules())
                    self.assertTrue(changed)
                    self.assertEqual(any("没装 gitleaks" in n and "brew install gitleaks" in n for n in notes), warned, notes)
                    shutil.rmtree(plugins.output_dir(m, "pub"))


class R2BBinaryTests(ReviewBase):
    """B (finding 6): a file that is not UTF-8 text is allowed only as a real, clean image."""

    def put(self, name, data):
        d = self.skills / "own-skill"
        for f in d.iterdir():
            if f.name != "SKILL.md":
                f.unlink()
        (d / name).write_bytes(data)

    def test_r2b_the_reviewers_inputs(self):
        cases = {
            "utf-16 token in notes.PNG": ("notes.PNG", FAKE_TOKEN.encode("utf-16-le"), "疑似密钥"),
            "png with a multi-line secret": ("shot.png", PNG + f"\napi_key: |\n  {VALUE}\n".encode() + b"\xff", "疑似密钥（多行的值）"),
            "png with a home path": ("shot.png", PNG + b"tEXtSource\x00/Users/you/private/a.png\xff", "绝对家目录路径"),
            "png with a utf-16-be home path": ("shot.png", PNG + "/home/reviewer/x".encode("utf-16-be") + b"\xff\xff", "绝对家目录路径"),
            "png with a utf-16-be token": ("shot.png", PNG + b"\xff\xff" + FAKE_TOKEN.encode("utf-16-be"), "疑似密钥"),
            "png with an encoded home path": ("shot.png", PNG + b"file://%2FUsers%2Fyou%2Fprivate\xff", "绝对家目录路径"),
        }
        for label, (name, data, want) in cases.items():
            self.put(name, data)
            for vis in ("public", "personal"):
                with self.subTest(case=label, vis=vis):
                    self.assertIn(want, self.refusal(vis))
        self.put("notes.PNG", FAKE_TOKEN.encode("utf-16-le"))
        msg = self.refusal("public")
        self.assertIn("notes.PNG 不是 UTF-8 文本，也不是允许的图片", msg, "the extension alone does not make it an image")
        self.assertIn("references", msg, "the fix says where such a file can go")
        self.assertIn("疑似密钥", self.refusal("company"), "a company plugin is scanned the same three ways")

    def test_r2b_only_a_real_image_passes(self):
        riff = b"RIFF\x24\x00\x00\x00"
        allowed = {"LOGO.PNG": PNG, "a.Jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF", "b.jpeg": b"\xff\xd8\xff\xdb", "c.gif": b"GIF89a\x01\x00\xff",
                   "d.webp": riff + b"WEBPVP8 \xff", "e.ico": b"\x00\x00\x01\x00\x01\x00\xff"}
        refused = {"fake.png": b"\xff\x00\x01\x02", "gif.png": b"GIF89a\x01\x00\xff", "wave.webp": riff + b"WAVEfmt \xff",
                   "image.txt": PNG, "image": PNG, "doc.pdf": b"%PDF-1.7\n\xff\xfe", "utf16.md": "plain words".encode("utf-16"),
                   "latin1.md": "caf\xe9".encode("latin-1"), "nul.md": b"text\x00more\n"}
        for name, data in allowed.items():
            self.put(name, data)
            with self.subTest(allowed=name):
                self.assertEqual(self.refusal("public"), "")
        for name, data in refused.items():
            self.put(name, data)
            with self.subTest(refused=name):
                self.assertIn(f"{name} 不是 UTF-8 文本，也不是允许的图片", self.refusal("public"))
                self.assertIn(f"{name} 不是 UTF-8 文本，也不是允许的图片", self.refusal("personal"))
                self.assertEqual(self.refusal("local"), "")


class R2CHomePathTests(ReviewBase):
    """C (finding 9): an encoded home path passed. The text is also scanned with its encodings taken off."""

    def test_r2c_encoded_home_paths(self):
        for text in ("[x](file://%2FUsers%2Fyou%2Fprivate)", "file://%252FUsers%252Fyou%252Fprivate",
                     "%2Fhome%2Freviewer%2Fnotes", "C%3A%5CUsers%5Creviewer", '"path": "\\/Users\\/you\\/x"',
                     "\\u002fUsers\\u002fyou", "\\x2fhome\\x2freviewer", "&#47;Users&#47;you", "&sol;home&sol;reviewer",
                     "&#x2F;Users&#x2F;you", "~reviewer%2Fprivate"):
            for vis in ("public", "personal"):
                with self.subTest(text=text, vis=vis):
                    self.assertTrue(plugins.home_path(text))
                    self.assertIn("绝对家目录路径", self.body(vis, f"see {text}\n"))

    def test_r2c_paths_that_name_no_person_pass(self):
        for text in ("$HOME/notes", "${HOME}/notes", "/var/folders/ab/cd123/T/tmp.x", "/private/var/folders/ab/cd123/T",
                     "/private/var/log/x.log", "/opt/homebrew/bin/gitleaks", "%2Fopt%2Fhomebrew%2Fbin", "100% done, 20%2 left",
                     "https://example.com/?next=%2FUsers%2Fyou", "~/coding", "/tmp/x", "/usr/local/bin"):
            with self.subTest(text=text):
                self.assertFalse(plugins.home_path(text))
        self.assertEqual(self.body("public", "tmp in /var/folders/ab/cd/T, tools in /opt/homebrew, home is $HOME\n"), "")

    def test_r2c_an_encoded_token_is_found_too(self):
        self.assertIn("疑似密钥", self.body("public", "see " + FAKE_TOKEN.replace("_", "%5F") + "\n"))


class R2DFrontMatterTests(ReviewBase):
    """D (finding 11): front matter the reader would have to guess at is refused, not read."""

    def write(self, name, front):
        d = self.skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\n{front}---\nbody\n", encoding="utf-8")
        return d

    def refused(self, front):
        errs = plugins.skill_limit_errors(self.write("s", "name: s\n" + front))
        return [(m, f) for m, f in errs if "front matter 这里不认" in m]

    def test_r2d_the_reviewers_inputs_are_refused_with_a_fix(self):
        long = "d" * 1100
        fronts = {
            "anchor and alias": f"other: &long {long}\ndescription: *long\n",
            "duplicate, long last": f"description: short\ndescription: {long}\n",
            "duplicate, long first": f"description: {long}\ndescription: short\n",
            "plain value with ': '": "description: about: value\n",
        }
        for label, front in fronts.items():
            with self.subTest(case=label):
                hit = self.refused(front)
                self.assertEqual(len(hit), 1, plugins.skill_limit_errors(self.skills / "s"))
                self.assertTrue("单行文字" in hit[0][1] or "只保留一个" in hit[0][0], hit)
                self.write("own-skill", "name: own-skill\n" + front)
                m = self.one_plugin("public")
                with self.assertRaisesRegex(plugins.PluginError, "front matter 这里不认.*修复"):
                    plugins.render_plugin(m, "pub", self.rules())
                self.assertTrue(any("front matter 这里不认" in f for f in self.fails()))

    def test_r2d_everything_that_needs_a_real_yaml_parser_is_refused(self):
        for front in ("description: &a text\n", "description: *a\n", "description: !!str text\n", "description: !custom text\n",
                      "base: &b\n  description: x\n<<: *b\n", "description: [a, b]\n", "description: {a: b}\n",
                      "description:\n  nested: map\n", "description:\n  - item\n", "description: x\nmeta:\n  a: &anchor 1\n",
                      "description: x\nmeta:\n  - *alias\n", "description: x\nmeta:\n  <<: *base\n", "description: x\nmeta: [*a]\n",
                      "description: x\nmeta: [\n  *a\n]\n", "description: x\nmeta: {<<: *b}\n", "description: ends with colon:\n",
                      "description: first line\n  second: line\n", "description: @mention\n", "description: `code`\n",
                      "description: |x\n  text\n", "description: % directive\n"):
            with self.subTest(front=front):
                self.assertEqual(len(self.refused(front)), 1, plugins.skill_limit_errors(self.skills / "s"))
        errs = plugins.skill_limit_errors(self.write("s", "description: x\nname: s\nname: s\n"))  # also for `name`
        self.assertTrue(any("键 name 出现了两次" in m for m, _ in errs), errs)

    def test_r2d_plain_front_matter_still_reads(self):
        for front in ("description: Does one thing well, when asked.\n", 'description: "about: value, with *stars* and &more"\n',
                      "description: 'it''s: fine'\n", "description: >-\n  folded: text\n  with *emphasis*\n",
                      "description: |\n  literal: text\n", "description: x\nallowed-tools: Bash(git add:*), Read\n",
                      "description: x\ntools: [Read, Grep]\n", 'description: x\nmetadata:\n  version: "1.0"\n  tags:\n    - a\n',
                      "description: http://example.com/a:b is fine\n", "description: a\n  second line\n"):
            with self.subTest(front=front):
                self.assertEqual(plugins.skill_limit_errors(self.write("s", "name: s\n" + front)), [])

    def test_r2d_the_lenient_reader_of_the_old_renderer_is_unchanged(self):
        import frontmatter
        self.assertEqual(frontmatter.scalars("---\ndescription: about: value\n---\n")["description"], "about: value")
        self.assertEqual(render.skill_description(self.write("s", "name: s\ndescription: about: value\n") / "SKILL.md"),
                         "about: value")
        with self.assertRaises(frontmatter.FrontMatterError):
            frontmatter.scalars("---\ndescription: about: value\n---\n", strict=True)


class R2EHashStabilityTests(ReviewBase):
    """E (finding 10): accepted limitation. Our computedHash is the en-US order on every machine."""

    def test_r2e_the_hash_does_not_change_with_the_locale(self):
        d = self.skills / "names"
        for rel in ("SKILL.md", "z.md", "å.md", "ä.md", "ö.md", "a b.md", "az.md", "中文.md", "日本.md", "é/x.md", "E.md"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(rel, encoding="utf-8")
        mine = plugins.skill_folder_hash(d)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import locale; locale.setlocale(locale.LC_ALL, '') if sys.argv[3] == '1' else None\n"
                "import plugins; from pathlib import Path; print(plugins.skill_folder_hash(Path(sys.argv[2])))")
        seen = {}
        for loc in ("C", "en_US.UTF-8", "sv_SE.UTF-8", "zh_CN.UTF-8", "tr_TR.UTF-8"):
            env = dict(os.environ, LC_ALL=loc, LANG=loc, PYTHONIOENCODING="utf-8")
            for adopt in ("0", "1"):  # also with the process having adopted that locale
                run = subprocess.run([sys.executable, "-c", code, str(REPO / "engine"), str(d), adopt], env=env,
                                     capture_output=True, text=True)
                if run.returncode:  # that locale is not installed on this machine
                    continue
                seen[(loc, adopt)] = run.stdout.strip()
        self.assertGreaterEqual(len(seen), 2, seen)
        self.assertEqual(set(seen.values()), {mine}, seen)


class R2FExitCodeTests(ReviewBase):
    """F: every refusal exits non-zero, through the real bin/agent-env."""

    def run_bin(self, *argv):
        return subprocess.run([str(REPO / "bin" / "agent-env"), argv[0], "--manifest", str(self.manifest), *argv[1:]]
                              if argv[0] in ("render", "check") else
                              [str(REPO / "bin" / "agent-env"), *argv, "--manifest", str(self.manifest)],
                              capture_output=True, text=True)

    def test_r2f_refusals_exit_non_zero(self):
        self.setup({"pub": PUBLIC, "per": PERSONAL})  # no repo yet: the pointer has placeholders
        for argv in (("render", "--target", "cloud/bot"), ("render", "--target", "cloud/bot", "--check"),
                     ("render", "--target", "cloud/bot", "--write"), ("deliver", "--target", "cloud/bot"),
                     ("deliver", "--target", "cloud/bot", "--record"), ("plugin", "pointer", "--target", "cloud/bot", "--record")):
            with self.subTest(argv=argv):
                run = self.run_bin(*argv)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertIn("FAIL", run.stderr)
                self.assertIn("修复", run.stderr)
        self.setup({"pub": dict(PUBLIC, sections=["Cli only"]), "per": PERSONAL})  # fails validation
        for argv in (("plugin", "build", "--all"), ("plugin", "publish", "--all"), ("plugin", "pointer", "--target", "cloud/bot")):
            with self.subTest(argv=argv):
                run = self.run_bin(*argv)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertIn("修复", run.stderr)

    def test_r2f_the_demo_grok_bot_render_exits_non_zero(self):
        run = subprocess.run([str(REPO / "bin" / "agent-env"), "render", "--target", "grok-bot/bot",
                              "--manifest", str(DEMO_MANIFEST)], capture_output=True, text=True)
        self.assertIn("plugin pointer --target grok-bot/bot", run.stderr)
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(run.stdout, "")


class R2HCodexLimitTests(ReviewBase):
    """H: an own skill whose SKILL.md body is over what Codex loads gets a warning, and still builds."""

    def test_r2h_a_body_over_8_kb_warns_and_builds(self):
        self.assertEqual(plugins.CODEX_SKILL_BODY_LIMIT, 8 * 1024)
        m = self.one_plugin("personal")
        self.skill("own-skill", "Does its own thing.", body="x" * 8193)
        changed, _, _, notes = plugins.build(m, "pub", self.rules())
        self.assertTrue(changed, "a warning does not stop the build")
        warn = [n for n in notes if "Codex" in n]
        self.assertEqual(len(warn), 1, notes)
        for part in ("skill「own-skill」", "8193 B", "8192 B", "修复：把细节挪进 own-skill/references/"):
            self.assertIn(part, warn[0])
        rc, out, _ = self.cli("plugin", "build", "--all")
        self.assertEqual(rc, 0)
        self.assertIn("插件 pub：提醒：skill「own-skill」的 SKILL.md 正文 8193 B", out)
        out = io.StringIO()
        plugins.publish(m, ["pub"], self.rules(), out)
        self.assertIn("提醒：skill「own-skill」", out.getvalue())

    def test_r2h_at_the_limit_or_with_a_long_front_matter_there_is_no_warning(self):
        m = self.one_plugin("personal")
        self.skill("own-skill", "d" * 1000, body="x" * 8192)  # the front matter does not count
        self.assertEqual([n for n in plugins.build(m, "pub", self.rules())[3] if "Codex" in n], [])
        self.assertEqual(plugins.skill_warnings(self.setup({"pub": dict(PUBLIC, skills=[]), "per": PERSONAL}), "pub"), [])

if __name__ == "__main__":
    unittest.main()
