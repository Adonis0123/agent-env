"""Pull (engine/pull.py, features/plugin-pull/agent-env-pull): a remote-exec host that pulls its plugins itself.
A local sh stands in for the remote host, local bare repositories for the plugin repositories (git's
url.insteadOf maps the manifest's https addresses onto them), and small scripts for runuser and systemctl."""
import getpass
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import habits  # noqa: E402
import issues  # noqa: E402
import plugins  # noqa: E402
import plugins_view  # noqa: E402
import pull  # noqa: E402
from test_agent_env import git  # noqa: E402
from test_plugins import PluginBase  # noqa: E402

SEND = ["sh", "-c", 'mkdir -p "$2" && cp "$1" "$2/$3"', "send"]
TID = "box/bot"
URL = "https://git.example.test/"
PUB = {"visibility": "public", "description": "public rules", "sections": ["__preamble__", "Shared"],
       "repo": URL + "pub"}
CO = {"visibility": "company", "description": "company rules", "sections": ["Cli only"],
      "company_skills": ["grp/co-skill"], "repo": URL + "co"}
EXPECT = [{"kind": "pulled", "habit": "rules", "name": "AGENTS.md"}, {"kind": "plugins", "habit": "rules"}]


class PullBase(PluginBase):
    def setUp(self):
        super().setUp()
        self.rhome = self.root / "remote-home"
        (self.rhome / ".hermes" / "workspace").mkdir(parents=True)
        self.out = self.rhome / ".hermes" / "workspace" / "AGENTS.md"
        self.cfg = self.rhome / ".hermes" / "config.yaml"
        self.cfg.write_text("model: x\nskills:\n  external_dirs: []\ntimezone: Asia/Shanghai\n", encoding="utf-8")
        self.company = self.root / "company-skills"
        d = self.company / "grp" / "co-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: co-skill\ndescription: A company skill.\n---\nbody\n", encoding="utf-8")
        self.remotes = self.root / "remotes"
        self.remotes.mkdir()
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text(f'[url "file://{self.remotes}/"]\n\tinsteadOf = {URL}\n', encoding="utf-8")
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        self.calls = self.root / "systemctl.log"
        self.timer_state = self.root / "timer-state"
        self.timer_state.write_text("inactive\n")
        scripts = {
            "runuser": '#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n',
            "chown": "#!/bin/sh\nexit 0\n",
            # the user manager: enable switches the timer on, start runs the puller once (as systemd would)
            "systemctl": (f'#!/bin/sh\necho "$*" >> "{self.calls}"\n'
                          f'case "$*" in\n'
                          f'  *"enable --now agent-env-pull.timer"*) echo active > "{self.timer_state}" ;;\n'
                          f'  *"start agent-env-pull.service"*) HOME="{self.rhome}" exec "{self.rhome}/.local/bin/agent-env-pull" ;;\n'
                          f'  *"is-active agent-env-pull.timer"*) cat "{self.timer_state}" ;;\n'
                          f'  *is-active*) echo active ;;\n'
                          f'esac\nexit 0\n')}
        for name, body in scripts.items():
            (fakebin / name).write_text(body, encoding="utf-8")
            (fakebin / name).chmod(0o755)
        env = mock.patch.dict(os.environ, {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                                           "GIT_CONFIG_GLOBAL": str(gitconfig)})
        env.start()
        self.addCleanup(env.stop)

    def env(self, **extra):
        spec = {"transport": "remote-exec", "exec": ["sh"], "send": SEND, "remote_home": str(self.rhome),
                "remote_user": getpass.getuser(), "timeout": 60, "deny_visibility": ["personal"]}
        spec.update(extra)
        return {"box": spec, "test": {"platform": "nomatch"}}

    def box(self, **extra):
        t = self.target(EXPECT, id=TID, host="hermes", env="box", roles=["bot"], plugins=["pub", "co"],
                        pull={"dir": "~/.local/share/agent-env/plugins", "output": "~/.hermes/workspace/AGENTS.md",
                              "skills_config": "~/.hermes/config.yaml"})
        t.update(extra)
        return t

    def setup_box(self, specs=None, target=None, environments=None):
        m = self.setup(specs or {"pub": PUB, "co": CO}, [target or self.box()], environments or self.env())
        text = self.manifest.read_text(encoding="utf-8").replace(
            "[sources]\n", f'[sources]\ncompany_skills = "{self.company}"\n', 1)
        self.manifest.write_text(text, encoding="utf-8")
        self.m = habits.load_manifest(self.manifest)
        return self.m

    def publish(self, name):
        """Commit the plugin as built now into its bare repository (what `plugin publish --yes` pushes)."""
        bare = self.remotes / name
        work = self.root / "work" / name
        if not bare.exists():
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            work.mkdir(parents=True)
            git(work, "init", "-q", "-b", "main")
            git(work, "remote", "add", "origin", str(bare))
        for p in work.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        for rel, (data, exe) in plugins.render_plugin(self.m, name, self.rules()).items():
            f = work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        git(work, "add", "-A")
        if git(work, "status", "--porcelain").strip():
            git(work, "commit", "-q", "-m", "publish")
            git(work, "push", "-q", "origin", "HEAD:main")

    def deliver(self, yes=True):
        out, err = io.StringIO(), io.StringIO()
        code = agent_env.deliver(self.manifest, TID, out=out, err=err, yes=yes)
        return code, out.getvalue(), err.getvalue()

    def expected(self):
        return pull.joined_rules(self.m, habits.find_target(self.m, TID), self.rules())

    def run_puller(self):
        return subprocess.run([str(self.rhome / ".local/bin/agent-env-pull")], capture_output=True, text=True,
                              env=dict(os.environ, HOME=str(self.rhome)))

    def status(self):
        lines = (self.rhome / ".local/state/agent-env/pull.status").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def by_name(self):
        st = agent_env.doctor(self.manifest, "box", self.repo)
        return {c["name"]: c for c in next(t for t in st["targets"] if t["id"] == TID)["habits"]}


class ManifestTests(PullBase):
    def test_valid_setup_passes_check(self):
        self.setup_box()
        self.assertEqual(self.fails(), [])

    def test_remote_plugin_target_without_pull_is_refused(self):
        t = self.box()
        del t["pull"]
        t["expect"] = [{"kind": "plugins", "habit": "rules"}]
        self.setup_box(target=t)
        self.assertTrue(any("没写 pull" in f for f in self.fails()), self.fails())

    def test_pull_outside_remote_exec_and_bad_paths_are_refused(self):
        self.setup_box(target=self.box(env="test", expect=[{"kind": "plugins", "habit": "rules"}]))
        self.assertTrue(any("不是 remote-exec" in f for f in self.fails()), self.fails())
        self.setup_box(target=self.box(pull={"dir": "/abs", "output": "~/a b.md", "env_keys": ["X"]}))
        fails = self.fails()
        self.assertTrue(any("pull.dir 不是" in f for f in fails), fails)
        self.assertTrue(any("pull.output 不是" in f for f in fails), fails)
        self.assertTrue(any("env_file 和 env_keys 要一起写" in f for f in fails), fails)

    def test_personal_plugin_denied_on_the_company_host(self):
        self.setup_box({"pub": PUB, "co": CO, "per": {"visibility": "personal", "sections": ["Cli only"]}},
                       target=self.box(plugins=["pub", "per"]))
        self.assertTrue(any("不允许个人内容" in f for f in self.fails()), self.fails())

    def test_company_skills_only_in_company_or_local_plugins(self):
        self.setup_box({"pub": dict(PUB, company_skills=["grp/co-skill"]), "co": CO})
        self.assertTrue(any("公开插件却写了 company_skills" in f for f in self.fails()), self.fails())

    def test_company_skill_is_copied_under_its_last_segment(self):
        self.setup_box()
        files = plugins.render_plugin(self.m, "co", self.rules())
        self.assertIn("skills/co-skill/SKILL.md", files)
        meta = json.loads(files["agent-env-plugin.json"][0])
        self.assertEqual(meta["company_skills"], ["co-skill"])
        self.assertNotIn("company_skills", json.loads(plugins.render_plugin(self.m, "pub", self.rules())
                                                      ["agent-env-plugin.json"][0]))  # others keep their bytes
        self.assertEqual(pull.skill_roots(self.m, self.m["environments"]["box"], habits.find_target(self.m, TID)),
                         [f"{self.rhome}/.local/share/agent-env/plugins/co/skills"])

    def test_reviewed_line_passes_only_in_a_company_plugin(self):
        self.setup_box()
        script = self.company / "grp" / "co-skill" / "run.py"
        line = 'msg = f"missing credentials: set GOOGLE_APPLICATION_CREDENTIALS or create {DEFAULT_CRED}"'
        script.write_text(line + "\n", encoding="utf-8")
        with self.assertRaises(plugins.PluginError) as ctx:
            plugins.render_plugin(self.m, "co", self.rules())
        key = plugins.line_key("skills/co-skill/run.py", line)
        self.assertIn(key, str(ctx.exception))
        self.setup_box({"pub": PUB, "co": dict(CO, privacy_reviewed=[key])})
        self.assertIn("skills/co-skill/run.py", plugins.render_plugin(self.m, "co", self.rules()))
        script.write_text(line + "  # edited\n", encoding="utf-8")  # an edited line is flagged again
        with self.assertRaises(plugins.PluginError):
            plugins.render_plugin(self.m, "co", self.rules())
        self.setup_box({"pub": dict(PUB, privacy_reviewed=[key]), "co": CO})
        self.assertTrue(any("公开插件却写了 privacy_reviewed" in f for f in self.fails()), self.fails())


class DeliverTests(PullBase):
    def setUp(self):
        super().setUp()
        self.setup_box()
        self.out.write_text("old rules delivered by SendFile\n", encoding="utf-8")
        self.publish("pub")
        self.publish("co")

    def test_dry_run_changes_nothing(self):
        code, out, err = self.deliver(yes=False)
        self.assertEqual(code, 0)
        self.assertIn("演练", err)
        self.assertIn("预览", out)
        self.assertEqual(self.out.read_text(), "old rules delivered by SendFile\n")
        self.assertFalse((self.rhome / ".local/bin/agent-env-pull").exists())

    def test_deliver_installs_previews_then_switches_on_the_timer(self):
        code, out, _ = self.deliver()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), self.expected())
        self.assertEqual(self.status()["result"], "ok")
        self.assertIn("enable --now agent-env-pull.timer", self.calls.read_text())
        backups = list((self.rhome / ".local/state/agent-env/backups").iterdir())
        self.assertEqual(len(backups), 1)
        saved = (backups[0] / "paths.txt").read_text()
        self.assertIn(str(self.out), saved)  # the SendFile rules can be put back
        self.assertEqual((backups[0] / "AGENTS.md").read_text(), "old rules delivered by SendFile\n")
        self.assertIn(f"  - {self.rhome}/.local/share/agent-env/plugins/co/skills", self.cfg.read_text())
        self.assertIn("config.yaml", saved)
        rec = json.loads(agent_env.remote.delivery_path(TID).read_text())
        self.assertEqual((rec["mode"], rec["skills_config"]), ("pull", "set"))
        # a second delivery is a no-op for the rules and the config
        code, out, _ = self.deliver()
        self.assertEqual(code, 0, out)
        self.assertIn("external_dirs already", out)

    def test_puller_is_idempotent_and_byte_stable(self):
        self.deliver()
        before = self.out.stat().st_mtime_ns
        res = self.run_puller()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.status()["changed"], "no")
        self.assertEqual(self.out.stat().st_mtime_ns, before)
        self.assertEqual(self.out.read_text(encoding="utf-8"), self.expected())

    def test_preview_mismatch_leaves_the_rules_and_timer_alone(self):
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "shared body, unpublished"), encoding="utf-8")
        with self.assertRaises(agent_env.AgentEnvError) as ctx:
            self.deliver()
        self.assertIn("预览不一致", str(ctx.exception))
        self.assertEqual(self.out.read_text(), "old rules delivered by SendFile\n")
        self.assertNotIn("enable", self.calls.read_text())

    def test_fetch_failure_keeps_the_last_good_rules(self):
        self.deliver()
        good = self.out.read_text(encoding="utf-8")
        (self.remotes / "co").rename(self.remotes / "co-gone")
        res = self.run_puller()
        self.assertEqual(res.returncode, 1)
        st = self.status()
        self.assertEqual((st["result"], st["stale"]), ("stale", "co"))
        self.assertEqual(self.out.read_text(encoding="utf-8"), good)

    def test_never_fetched_plugin_stops_before_the_output(self):
        (self.remotes / "co").rename(self.remotes / "co-gone")
        with self.assertRaises(agent_env.AgentEnvError):
            self.deliver()
        self.assertEqual(self.out.read_text(), "old rules delivered by SendFile\n")
        preview = (self.rhome / ".local/state/agent-env/pull.preview.status").read_text()
        self.assertIn("result=failed", preview)
        self.assertIn("从没拉成功过", preview)

    def test_unexpected_external_dirs_is_not_merged(self):
        self.cfg.write_text("skills:\n  external_dirs:\n  - /somewhere/else\n", encoding="utf-8")
        with self.assertRaises(agent_env.AgentEnvError) as ctx:
            self.deliver()
        self.assertIn("skills_config=unexpected", str(ctx.exception))
        self.assertEqual(self.cfg.read_text(), "skills:\n  external_dirs:\n  - /somewhere/else\n")


class DoctorTests(PullBase):
    def setUp(self):
        super().setUp()
        self.setup_box()
        self.out.write_text("old rules delivered by SendFile\n", encoding="utf-8")
        self.publish("pub")
        self.publish("co")

    def test_before_the_switch_the_old_rules_are_drift_and_the_timer_missing(self):
        cells = self.by_name()
        self.assertEqual(cells["AGENTS.md"]["state"], "drift")
        self.assertIn("还没拉取过", cells["AGENTS.md"]["detail"])
        self.assertEqual(cells["定时拉取"]["state"], "missing")
        self.assertIn(f"bin/agent-env deliver --target {TID}", cells["定时拉取"]["fix"])

    def test_after_delivery_rules_and_timer_are_synced(self):
        self.deliver()
        cells = self.by_name()
        self.assertEqual(cells["AGENTS.md"]["state"], "synced", cells["AGENTS.md"])
        self.assertIn("插件 pub、co", cells["AGENTS.md"]["detail"])
        self.assertEqual(cells["定时拉取"]["state"], "synced", cells["定时拉取"])
        self.assertEqual(cells["插件 co（公司）"]["state"], "missing")  # this machine has not built it yet

    def test_unpublished_change_is_drift_with_publish_fix(self):
        self.deliver()
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "shared body, unpublished"), encoding="utf-8")
        cells = self.by_name()
        self.assertEqual(cells["AGENTS.md"]["state"], "drift")
        self.assertIn("plugin publish", cells["AGENTS.md"]["fix"])

    def test_on_a_machine_that_does_not_publish_the_fix_names_the_one_that_does(self):
        """`publish_from` names another machine: this one keeps no clone of the plugin repositories, so it only
        says the pulled rules differ from the sources, and where publishing is done."""
        self.deliver()
        (self.root / "AGENTS.md").write_text(self.rules().replace("shared body", "shared body, unpublished"), encoding="utf-8")
        plain = self.by_name()["AGENTS.md"]
        self.assertIn("源改了还没发布", plain["detail"])
        self.manifest.write_text('publish_from = "desk"\n' + self.manifest.read_text(encoding="utf-8")
                                 + '[environments.desk]\nplatform = "nomatch"\n[env_names]\ndesk = "Desk"\n', encoding="utf-8")
        m = habits.load_manifest(self.manifest)
        self.assertFalse((plugins.clone_dir("pub") / ".git").exists(), "no publish clone on this machine")
        status = agent_env.doctor(self.manifest, "box", self.repo)  # this machine is not `desk`
        cell = {c["name"]: c for c in next(t for t in status["targets"] if t["id"] == TID)["habits"]}["AGENTS.md"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("与规则源现在生成的内容不一致", cell["detail"])
        self.assertNotIn("还没发布", cell["detail"])
        self.assertEqual(cell["fix"], "到 Desk 上让 AI 发布（发布只在那台机器上做）；远程下次拉取时更新")
        away = plugins.published_away(m, "box")
        self.assertEqual(away, {"env": "desk", "name": "Desk"})
        todo = next(i for i in issues.build([status], [], m, away=away) if i["target"] == TID and i["ref"]["name"] == "AGENTS.md")
        self.assertEqual((todo["actor"], todo["ai_task"], todo["at"]), ("other_env", None, away))
        self.assertEqual([(x["who"], x["command"]) for x in todo["steps"]], [("other_env", None)])
        self.assertNotIn("bin/agent-env", json.dumps(todo, ensure_ascii=False))
        row = next(r for r in plugins_view.view(m, self.manifest, [status], self.rules(), env="box")["environments"]
                   if r["env"] == "box")
        self.assertEqual(row["notes"], [{"code": "pull_drift", "state": "drift", "text_zh": "拉到的规则不是最新",
                                         "fix": "到 Desk 上让 AI 处理", "at": "desk"}])
        # on the publishing machine the cell is what it was without the key
        mine = agent_env.doctor(self.manifest, "box", self.repo, "desk")
        cell = {c["name"]: c for c in next(t for t in mine["targets"] if t["id"] == TID)["habits"]}["AGENTS.md"]
        self.assertEqual(cell, plain)
        self.assertIn("plugin publish", cell["fix"])
        self.assertFalse(any("at" in i for i in issues.build([mine], [], m, away=plugins.published_away(m, "desk"))))

    def test_failed_pull_and_stopped_timer_are_drift(self):
        self.deliver()
        (self.remotes / "co").rename(self.remotes / "co-gone")
        self.run_puller()
        cell = self.by_name()["定时拉取"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("stale", cell["detail"])
        self.timer_state.write_text("inactive\n")
        self.assertIn("没在运行", self.by_name()["定时拉取"]["detail"])

    def test_changed_plugin_list_makes_the_installed_conf_stale(self):
        self.deliver()
        self.setup_box({"pub": PUB, "co": dict(CO, repo=URL + "co2")})
        cell = self.by_name()["定时拉取"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("pull.conf", cell["detail"])


class PanelTests(PullBase):
    def setUp(self):
        super().setUp()
        self.setup_box()
        self.out.write_text("old rules delivered by SendFile\n", encoding="utf-8")
        self.publish("pub")
        self.publish("co")

    def test_issue_and_plugins_tab_before_the_switch(self):
        st = agent_env.doctor(self.manifest, "box", self.repo)
        got = [i for i in issues.build([st], [], self.m) if i["target"] == TID and i["ref"]["kind"] == "rules"]
        by = {i["ref"]["name"]: i for i in got}
        rules = by["AGENTS.md"]
        self.assertEqual((rules["actor"], rules["state"]), ("user", "drift"))
        self.assertIn("不是最新", rules["title_zh"])
        cmds = [x["command"] for x in rules["steps"]]
        self.assertIn("bin/agent-env plugin publish --all", cmds)
        self.assertIn(f"bin/agent-env deliver --target {TID} --dry-run", cmds)
        self.assertTrue(all("--yes" not in (c or "") for c in cmds))  # a real write is never a copyable command
        self.assertEqual(by["定时拉取"]["state"], "missing")
        self.assertEqual(issues.cell_origin(self.m, {"id": TID}, by["AGENTS.md"]["ref"] | {"kind": "rules",
                         "expect_kind": "pulled"}), "rules")
        row = next(r for r in plugins_view.environments(self.m, [st]) if r["env"] == "box")
        self.assertEqual((row["mode"], row["credential"]["state"]), ("plugins", "own"))
        self.assertIn("git 凭证", row["credential"]["detail"])
        self.assertIn("pull_drift", [n["code"] for n in row["notes"]])
        self.deliver()
        st = agent_env.doctor(self.manifest, "box", self.repo)
        row = next(r for r in plugins_view.environments(self.m, [st]) if r["env"] == "box")
        self.assertFalse([n for n in row["notes"] if n["code"].startswith("pull_")])


if __name__ == "__main__":
    unittest.main()
