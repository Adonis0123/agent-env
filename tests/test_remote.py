"""remote-exec environments, with a local shell standing in for the remote host."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import habits_check  # noqa: E402
import render  # noqa: E402
from test_agent_env import Base, git, validate_all_status, validate_env_status  # noqa: E402

FAKE_SECRET = "sk-" + "test-FAKE-0123456789"  # built in two parts: no literal token in the file
SEND = ["sh", "-c", 'mkdir -p "$2" && cp "$1" "$2/$3"', "send"]
TID = "box/bot"


class RemoteBase(Base):
    def setUp(self):
        super().setUp()
        self.rhome = self.root / "remote-home"  # the fake remote host's home directory
        (self.rhome / ".hermes").mkdir(parents=True)
        (self.rhome / ".hermes" / ".env").write_text(f"FAKE_KEY={FAKE_SECRET}\n", encoding="utf-8")
        self.profile = self.root / "profile.md"
        self.profile.write_text("# box profile\n", encoding="utf-8")
        fakebin = self.root / "fakebin"  # stands in for the remote systemctl
        fakebin.mkdir()
        (fakebin / "systemctl").write_text(f'#!/bin/sh\ncat "{self.root / "svc-state"}"\n', encoding="utf-8")
        (fakebin / "systemctl").chmod(0o755)
        self.set_service("active")
        env = mock.patch.dict(os.environ, {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)

    def set_service(self, state):
        (self.root / "svc-state").write_text(state + "\n", encoding="utf-8")

    def setup_env(self, exec_cmd=("sh",), login=("true",), timeout=20, chunk=24000, expect=None):
        spec = {"transport": "remote-exec", "exec": list(exec_cmd), "send": SEND,
                "remote_home": str(self.rhome), "timeout": timeout, "chunk_bytes": chunk}
        expect = expect if expect is not None else [
            {"kind": "login", "habit": "rules", "name": "login", "cmd": list(login),
             "fix": "run the login command yourself"},
            {"kind": "generated", "habit": "rules", "name": "AGENTS.md"},
            {"kind": "service", "habit": "rules", "name": "gateway", "unit": "gw"},
            {"kind": "contains", "habit": "rules", "name": "env key", "path": "~/.hermes/.env", "text": "FAKE_KEY="},
        ]
        t = self.target(expect, id=TID, env="box", roles=["bot"], render="generated",
                        supplement=str(self.profile), output="~/.hermes/AGENTS.md")
        self.write_manifest([t], {"box": spec, "test": {"platform": "nomatch"}})

    def remote_file(self):
        return self.rhome / ".hermes" / "AGENTS.md"

    def rendered(self):
        return agent_env.delivery_text(agent_env._load(self.manifest), TID, agent_env._rules_text(
            agent_env._load(self.manifest)))

    def by_name(self, status):
        return {c["name"]: c for c in self.cells(status, TID)}


class RemoteDoctorTests(RemoteBase):
    def test_generated_matches_remote_hash(self):
        self.setup_env()
        self.remote_file().write_text(self.rendered(), encoding="utf-8")
        st = self.doctor("box")
        self.assertEqual(validate_env_status(st), [])
        cells = self.by_name(st)
        self.assertEqual(cells["AGENTS.md"]["state"], "customized")  # has a profile
        self.assertEqual(cells["login"]["state"], "synced")
        self.assertEqual(cells["env key"]["state"], "synced")
        self.assertNotIn("error", st)

    def test_generated_differs_is_drift_with_deliver_fix(self):
        self.setup_env()
        self.remote_file().write_text("hand edited\n", encoding="utf-8")
        c = self.by_name(self.doctor("box"))["AGENTS.md"]
        self.assertEqual(c["state"], "drift")
        self.assertIn(f"bin/agent-env deliver --target {TID}", c["fix"])

    def test_generated_absent_is_missing(self):
        self.setup_env()
        c = self.by_name(self.doctor("box"))["AGENTS.md"]
        self.assertEqual(c["state"], "missing")
        self.assertTrue(c["fix"])

    def test_service_active_and_inactive(self):
        self.setup_env()
        self.assertEqual(self.by_name(self.doctor("box"))["gateway"]["state"], "synced")
        self.set_service("inactive")
        c = self.by_name(self.doctor("box"))["gateway"]
        self.assertEqual(c["state"], "drift")
        self.assertIn("重启", c["fix"])
        self.set_service("Failed to connect to bus")
        self.assertEqual(self.by_name(self.doctor("box"))["gateway"]["state"], "unknown")

    def test_service_health_url_needs_200_and_a_non_empty_list(self):
        fakebin = self.root / "fakebin"  # stands in for the remote curl: body file, then "\n<code>" like -w
        (fakebin / "curl").write_text(f'#!/bin/sh\ncat "{self.root / "http-body"}"\nprintf "\\n%s" "$(cat "{self.root / "http-code"}")"\n',
                                      encoding="utf-8")
        (fakebin / "curl").chmod(0o755)

        def answer(code, body):
            (self.root / "http-code").write_text(code, encoding="utf-8")
            (self.root / "http-body").write_text(body, encoding="utf-8")

        self.setup_env(expect=[{"kind": "service", "habit": "skills", "name": "SearXNG", "unit": "docker",
                                "scope": "system", "health_url": "http://localhost:8080/search?q=test&format=json",
                                "health_json": "results"}])
        answer("200", '{"query": "test", "results": [{"url": "a"}, {"url": "b"}]}')
        c = self.by_name(self.doctor("box"))["SearXNG"]
        self.assertEqual(c["state"], "synced")
        self.assertIn("results 有 2 条", c["detail"])
        answer("200", '{"query": "test", "results": []}')
        self.assertEqual(self.by_name(self.doctor("box"))["SearXNG"]["state"], "drift")
        answer("200", "<html>not json</html>")
        self.assertEqual(self.by_name(self.doctor("box"))["SearXNG"]["state"], "drift")
        answer("502", "bad gateway")
        c = self.by_name(self.doctor("box"))["SearXNG"]
        self.assertEqual(c["state"], "drift")
        self.assertIn("HTTP 502", c["detail"])
        answer("000", "")
        self.assertIn("连不上", self.by_name(self.doctor("box"))["SearXNG"]["detail"])
        self.set_service("inactive")  # the unit itself down: drift before any request
        answer("200", '{"results": [1]}')
        self.assertIn("没在运行", self.by_name(self.doctor("box"))["SearXNG"]["detail"])

    def test_login_failure_is_unknown_with_fix_and_skips_remote(self):
        marker = self.root / "remote-ran"
        self.setup_env(exec_cmd=("sh", "-c", f'touch "{marker}"; sh'), login=("sh", "-c", "exit 1"))
        st = self.doctor("box")
        self.assertEqual(validate_env_status(st), [])
        c = self.by_name(st)["login"]
        self.assertEqual((c["state"], c["fix"]), ("unknown", "run the login command yourself"))
        self.assertEqual(st["error"]["fix"], "run the login command yourself")
        self.assertFalse(marker.exists())
        self.assertTrue(all(x["state"] == "unknown" for x in self.cells(st, TID)))
        self.assertTrue(agent_env.has_problems(st))

    def test_relogin_runs_once_then_reprobes(self):
        ok = self.root / "logged-in"  # the probe passes once the fake login made this file
        count = self.root / "relogin-count"
        probe = ("sh", "-c", f'test -e "{ok}"')
        relogin = ["sh", "-c", f'echo x >> "{count}"; touch "{ok}"']
        self.setup_env(expect=[{"kind": "login", "habit": "rules", "name": "login", "cmd": list(probe),
                                "relogin": relogin, "fix": "run the login command yourself"}])
        c = self.by_name(self.doctor("box"))["login"]
        self.assertEqual(c["state"], "synced")
        self.assertIn("自动登录", c["detail"])
        self.assertEqual(count.read_text().count("x"), 1)
        self.by_name(self.doctor("box"))  # still logged in: no second login
        self.assertEqual(count.read_text().count("x"), 1)

    def test_relogin_failure_is_unknown_with_manual_fix(self):
        count = self.root / "relogin-count"
        for relogin in (["sh", "-c", f'echo x >> "{count}"; exit 2'],  # login needs a person
                        ["sh", "-c", f'echo x >> "{count}"; exit 0']):  # login "worked" but probe still fails
            count.unlink(missing_ok=True)
            self.setup_env(expect=[{"kind": "login", "habit": "rules", "name": "login",
                                    "cmd": ["sh", "-c", "exit 1"], "relogin": relogin,
                                    "fix": "run the login command yourself"}])
            st = self.doctor("box")
            c = self.by_name(st)["login"]
            self.assertEqual((c["state"], c["fix"]), ("unknown", "run the login command yourself"))
            self.assertIn("自动登录也没成功", c["detail"])
            self.assertEqual(count.read_text().count("x"), 1)  # tried once, never loops

    def test_remote_timeout_marks_whole_environment(self):
        self.setup_env(exec_cmd=("sh", "-c", "sleep 30"), timeout=1)
        st = self.doctor("box")
        self.assertEqual(validate_env_status(st), [])
        self.assertIn("超时", st["error"]["message"])
        self.assertTrue(st["error"]["fix"])
        others = [c for c in self.cells(st, TID) if c["name"] != "login"]
        self.assertTrue(others and all(c["state"] == "unknown" for c in others))
        out, err = io.StringIO(), io.StringIO()
        rc = agent_env.main(["doctor", "--manifest", str(self.manifest), "--repo", str(self.repo), "--env", "box"],
                            out=out, err=err)
        self.assertEqual(rc, 1)
        self.assertIn("环境出错", out.getvalue())

    def test_transport_failure_is_environment_error(self):
        self.setup_env(exec_cmd=("sh", "-c", "cat >/dev/null; echo boom >&2; exit 255"))
        st = self.doctor("box")
        self.assertIn("255", st["error"]["message"])

    def test_output_has_no_secret_values(self):
        self.setup_env()
        st = self.doctor("box")
        everything = json.dumps(agent_env.all_status(self.manifest, "box", self.repo, live=st), ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, everything)
        out = io.StringIO()
        agent_env.deliver(self.manifest, TID, out=out, err=io.StringIO())
        self.assertNotIn(FAKE_SECRET, out.getvalue())

    def test_status_commit_and_all_surface_error(self):
        self.setup_env(exec_cmd=("sh", "-c", "sleep 30"), timeout=1)
        st = self.doctor("box")
        agent_env.commit_status(self.repo, st)
        self.assertIn("status/box", git(self.repo, "branch", "--list", "status/box"))
        live = self.doctor("test")
        all_ = agent_env.all_status(self.manifest, "test", self.repo, live=live)
        self.assertEqual(validate_all_status(all_), [])
        self.assertIn("box", [e["env"] for e in all_["environments"]])
        self.assertIn("box", [e["env"] for e in all_["errors"]])

    def test_missing_branch_fix_points_at_this_machine(self):
        self.setup_env()
        all_ = agent_env.all_status(self.manifest, "test", self.repo, live=self.doctor("test"))
        [e] = [e for e in all_["errors"] if e["env"] == "box"]
        self.assertIn("--env box --commit", e["fix"])


class BrokenLinksTests(RemoteBase):
    def setUp(self):
        super().setUp()
        self.setup_links()

    def setup_links(self, dirs=("~/.tool-cache", "~/.hermes/skills"), **extra):
        exp = {"kind": "broken-links", "habit": "skills", "name": "断链", "dirs": list(dirs), **extra}
        self.setup_env(expect=[exp])

    def test_broken_link_is_drift_listing_path_and_fix(self):
        cache = self.rhome / ".tool-cache"
        cache.mkdir()
        (cache / "sa.json").symlink_to(self.root / "gone" / "sa.json")
        (cache / "ok.json").symlink_to(self.rhome / ".hermes" / ".env")
        st = self.doctor("box")
        self.assertEqual(validate_env_status(st), [])
        c = self.by_name(st)["断链"]
        self.assertEqual((c["state"], c["kind"]), ("drift", "skills"))
        self.assertIn("1 个悬空符号链接", c["detail"])
        self.assertIn(str(cache / "sa.json"), c["detail"])
        self.assertNotIn("ok.json", c["detail"])
        self.assertIn("readlink", c["fix"])
        self.assertIn("不存在、已跳过", c["detail"])  # ~/.hermes/skills is absent

    def test_no_broken_links_is_synced_and_excluded_dirs_skipped(self):
        nm = self.rhome / ".tool-cache" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "dangling").symlink_to(self.root / "gone")
        (self.rhome / ".hermes" / "skills").mkdir()
        c = self.by_name(self.doctor("box"))["断链"]
        self.assertEqual(c["state"], "synced", c["detail"])
        self.setup_links(exclude=[])
        self.assertEqual(self.by_name(self.doctor("box"))["断链"]["state"], "drift")

    def test_missing_dirs_is_missing(self):
        self.setup_links(dirs=("~/nope", "~/also-nope"))
        c = self.by_name(self.doctor("box"))["断链"]
        self.assertEqual(c["state"], "missing")
        self.assertTrue(c["fix"])

    def test_local_check_matches_remote(self):
        d = self.home / "links"
        d.mkdir()
        exp = {"kind": "broken-links", "habit": "skills", "dirs": ["~/links", "~/absent"]}
        self.assertEqual(agent_env.check_broken_links(exp)["state"], "synced")
        (d / "x").symlink_to(self.root / "gone")
        c = agent_env.check_broken_links(exp)
        self.assertEqual(c["state"], "drift")
        self.assertIn("links/x", c["detail"])
        self.assertEqual(agent_env.check_broken_links({**exp, "dirs": ["~/absent"]})["state"], "missing")

    def test_check_rejects_bad_dirs(self):
        self.setup_links(dirs=("/abs/path",))
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("broken-links" in r["message"] for r in fails))


class SecretsTests(RemoteBase):
    """kind = "secrets": names and permissions only; a value never reaches the status."""

    def setUp(self):
        super().setUp()
        import getpass
        self.user = getpass.getuser()
        (self.rhome / ".hermes" / ".env").chmod(0o600)
        creds = self.rhome / ".hermes" / "credentials"
        (creds / "sub").mkdir(parents=True)
        creds.chmod(0o700)
        (creds / "sub").chmod(0o700)
        for f in (creds / "a.json", creds / "sub" / "b.md"):
            f.write_text(FAKE_SECRET, encoding="utf-8")
            f.chmod(0o600)
        self.creds = creds
        self.registry = self.root / "secrets.env"
        self.write_registry("FAKE_KEY  # in .env", "~/.hermes/.env", "~/.hermes/credentials/",
                            "~/.hermes/credentials/a.json")

    def write_registry(self, *lines):
        self.registry.write_text("# names only\n" + "\n".join(lines) + "\n", encoding="utf-8")
        self.setup_env(expect=[{"kind": "secrets", "habit": "skills", "name": "密钥", "registry": str(self.registry),
                                "env_file": "~/.hermes/.env", "owner": self.user}])

    def secrets_cell(self):
        st = self.doctor("box")
        self.assertNotIn(FAKE_SECRET, json.dumps(st, ensure_ascii=False))
        self.assertEqual(validate_env_status(st), [])
        return self.by_name(st)["密钥"]

    def test_all_present_and_closed_is_synced(self):
        c = self.secrets_cell()
        self.assertEqual((c["state"], c["kind"]), ("synced", "skills"), c["detail"])
        self.assertIn("1 个变量名", c["detail"])

    def test_open_permission_is_drift_with_chmod_fix(self):
        (self.creds / "sub" / "b.md").chmod(0o644)
        self.creds.chmod(0o755)
        c = self.secrets_cell()
        self.assertEqual(c["state"], "drift")
        self.assertIn("sub/b.md 权限 644", c["detail"])
        self.assertIn("chmod 600 ~/.hermes/credentials/sub/b.md", c["fix"])
        self.assertIn("chmod 700 ~/.hermes/credentials；", c["fix"])

    def test_wrong_owner_is_drift_with_chown_fix(self):
        self.write_registry("~/.hermes/.env")
        self.setup_env(expect=[{"kind": "secrets", "habit": "skills", "name": "密钥", "registry": str(self.registry),
                                "env_file": "~/.hermes/.env", "owner": "nobody-else"}])
        c = self.secrets_cell()
        self.assertEqual(c["state"], "drift")
        self.assertIn("chown nobody-else", c["fix"])

    def test_missing_name_file_or_dangling_link_is_missing(self):
        (self.rhome / ".tool-cache").mkdir()
        (self.rhome / ".tool-cache" / "sa.json").symlink_to(self.root / "gone.json")
        self.write_registry("FAKE_KEY", "GONE_KEY", "~/.hermes/credentials/nope.json", "~/.tool-cache/sa.json")
        c = self.secrets_cell()
        self.assertEqual(c["state"], "missing")
        self.assertIn("没有 GONE_KEY", c["detail"])
        self.assertNotIn("FAKE_KEY", c["detail"])
        self.assertIn("nope.json 不存在", c["detail"])
        self.assertIn("sa.json 不存在", c["detail"])

    def test_issue_is_for_the_user_to_prepare(self):
        import issues
        (self.creds / "a.json").chmod(0o644)
        st = self.doctor("box")
        manifest = agent_env._load(self.manifest)
        t = next(x for x in manifest["targets"] if x["id"] == TID)
        c = self.by_name(st)["密钥"]
        self.assertEqual(issues.habit_source(manifest, t, c), "secrets")

    def test_local_check_matches_remote(self):
        env = self.home / ".hermes" / ".env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text(f"export FAKE_KEY={FAKE_SECRET}\n", encoding="utf-8")
        env.chmod(0o600)
        reg = self.root / "local-secrets.env"
        reg.write_text("FAKE_KEY\nOTHER_KEY\n~/.hermes/.env\n", encoding="utf-8")
        c = self.one({"kind": "secrets", "habit": "skills", "registry": str(reg), "env_file": "~/.hermes/.env"})
        self.assertEqual((c["name"], c["state"]), ("密钥", "missing"))
        self.assertIn("没有 OTHER_KEY", c["detail"])
        self.assertNotIn(FAKE_SECRET, json.dumps(c, ensure_ascii=False))
        reg.write_text("FAKE_KEY\n~/.hermes/.env\n", encoding="utf-8")
        self.assertEqual(self.one({"kind": "secrets", "habit": "skills", "registry": str(reg),
                                   "env_file": "~/.hermes/.env"})["state"], "synced")

    def test_check_rejects_unreadable_line_and_missing_registry(self):
        self.write_registry("FAKE_KEY", "/abs/path")
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("无法识别的行" in r["message"] for r in fails), fails)
        self.registry.unlink()
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("registry 读不了" in r["message"] for r in fails), fails)


class RemoteDeliverTests(RemoteBase):
    def run_deliver(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        rc = agent_env.main(["deliver", "--manifest", str(self.manifest), "--target", TID, *extra], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_prints_steps_and_changes_nothing(self):
        self.setup_env(chunk=100)
        self.remote_file().write_text("old\n", encoding="utf-8")
        rc, out, err = self.run_deliver("--dry-run")
        self.assertEqual(rc, 0, err)
        for step in ("1. 本机渲染", "2. 远程快照", "3. 分片发送", "4. 远程拼接", "5. 本机核对"):
            self.assertIn(step, out)
        n = -(-len(self.rendered().encode("utf-8")) // 100)
        self.assertIn(f"{n} 片", out)
        self.assertIn("--yes", err)
        self.assertEqual(self.remote_file().read_text(), "old\n")
        self.assertFalse(agent_env.delivery_path(TID).exists())
        self.assertFalse((self.rhome / ".cache").exists())

    def test_without_yes_is_a_dry_run(self):
        self.setup_env()
        rc, _, err = self.run_deliver()
        self.assertEqual(rc, 0)
        self.assertIn("未执行", err)
        self.assertFalse(self.remote_file().exists())

    def test_yes_delivers_in_chunks_snapshots_and_records(self):
        self.setup_env(chunk=64)
        self.remote_file().write_text("old\n", encoding="utf-8")
        rc, out, err = self.run_deliver("--yes")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(self.remote_file().read_text(encoding="utf-8"), self.rendered())
        [backup] = (self.rhome / ".hermes" / ".agent-env-backups").iterdir()
        self.assertEqual(backup.read_text(), "old\n")
        rec = json.loads(agent_env.delivery_path(TID).read_text())
        self.assertEqual(rec["backup"], str(backup))
        self.assertEqual(list((self.rhome / ".cache" / "agent-env" / "incoming").iterdir()), [])
        self.assertEqual(self.by_name(self.doctor("box"))["AGENTS.md"]["state"], "customized")

    def test_remote_user_owns_delivered_file(self):
        import getpass
        self.setup_env()
        m = self.manifest.read_text().replace('transport = "remote-exec"',
                                              f'transport = "remote-exec"\nremote_user = "{getpass.getuser()}"')
        self.manifest.write_text(m)
        rc, out, err = self.run_deliver("--yes")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("chown -R", out)
        self.assertEqual(self.remote_file().read_text(encoding="utf-8"), self.rendered())

    def test_yes_and_dry_run_conflict_and_record_refused(self):
        self.setup_env()
        self.assertEqual(self.run_deliver("--yes", "--dry-run")[0], 1)
        rc, _, err = self.run_deliver("--record")
        self.assertEqual(rc, 1)
        self.assertIn("远程落点", err)

    def test_failed_send_leaves_remote_file(self):
        self.setup_env()
        m = self.manifest.read_text().replace(json.dumps(SEND), json.dumps(["sh", "-c", "exit 7"]))
        self.manifest.write_text(m)
        self.remote_file().write_text("old\n", encoding="utf-8")
        rc, _, err = self.run_deliver("--yes")
        self.assertEqual(rc, 1)
        self.assertIn("第 3 步", err)
        self.assertEqual(self.remote_file().read_text(), "old\n")
        self.assertFalse(agent_env.delivery_path(TID).exists())


class RemoteManifestTests(RemoteBase):
    def test_render_check_and_write_refuse_remote_target(self):
        self.setup_env()
        err = io.StringIO()
        rc = render.run([str(self.manifest), "--target", TID, "--write"], out=io.StringIO(), err=err)
        self.assertEqual(rc, 1)
        self.assertIn("远程", err.getvalue())
        self.assertFalse((self.home / ".hermes" / "AGENTS.md").exists())

    def test_rendered_note_names_no_local_path(self):
        self.setup_env()
        text = self.rendered()
        self.assertIn(f"bin/agent-env deliver --target {TID}", text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("render.py", text)

    def test_check_passes_and_flags_bad_remote_fields(self):
        self.setup_env()
        levels = {r["level"] for r in habits_check.run_checks(self.manifest).results if r["check"] == "environments"}
        self.assertEqual(levels, {"PASS"})
        m = self.manifest.read_text().replace('transport = "remote-exec"', 'transport = "ssh"')
        self.manifest.write_text(m)
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("transport" in r["message"] for r in fails))

    def test_service_outside_remote_env_fails_check(self):
        self.write_manifest([self.target([{"kind": "service", "habit": "rules", "unit": "gw"}])])
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("service" in r["message"] for r in fails))


    def test_service_health_fields_are_checked(self):
        self.setup_env(expect=[{"kind": "service", "habit": "skills", "unit": "docker", "health_url": "localhost:8080",
                                "health_json": "results"}])
        fails = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("health_url" in m for m in fails), fails)
        self.setup_env(expect=[{"kind": "service", "habit": "skills", "unit": "docker", "health_json": "results"}])
        fails = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("health_json" in m for m in fails), fails)
        self.setup_env(expect=[{"kind": "service", "habit": "skills", "unit": "docker",
                                "health_url": "http://localhost:8080/search?q=test&format=json", "health_json": "results"}])
        fails = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertFalse(any("health" in m for m in fails), fails)

if __name__ == "__main__":
    unittest.main()
