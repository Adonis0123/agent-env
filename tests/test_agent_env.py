import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

import agent_env  # noqa: E402
import issues  # noqa: E402
import render  # noqa: E402

RULES = """# Title

Intro line.

## Shared

shared body

## Cli only

cli body
"""

STATES = ("synced", "customized", "drift", "missing", "unknown")
KINDS = ("rules", "skills", "hooks", "mcp")
LAYERS = ("safety", "env", "role", "host")


def validate_env_status(e) -> list:
    """Problems with one environment status against docs/status-schema.md (schema 1)."""
    errs = []
    if not isinstance(e, dict):
        return ["environment is not an object"]
    for key, typ in (("schema", int), ("env", str), ("source", str), ("generated_at", str),
                     ("engine_rev", str), ("summary", dict), ("check", dict), ("targets", list), ("eval", dict)):
        if not isinstance(e.get(key), typ):
            errs.append(f"{key} missing or not {typ.__name__}")
    if errs:
        return errs
    if e["schema"] != 1:
        errs.append("schema != 1")
    if e["source"] not in ("live", "branch"):
        errs.append(f"source {e['source']!r}")
    if not e["generated_at"].endswith("Z") or "T" not in e["generated_at"]:
        errs.append("generated_at is not UTC ISO 8601")
    if set(e["summary"]) != set(STATES) or not all(isinstance(v, int) for v in e["summary"].values()):
        errs.append("summary keys or values")
    if set(e["check"]) != {"pass", "warn", "fail"}:
        errs.append("check keys")
    if "ran_at" not in e["eval"] or not isinstance(e["eval"].get("results"), list):
        errs.append("eval shape")
    counted = {s: 0 for s in STATES}
    for t in e["targets"]:
        for key, typ in (("id", str), ("host", str), ("env", str), ("roles", list), ("verified", bool),
                         ("habits", list)):
            if not isinstance(t.get(key), typ):
                errs.append(f"target {t.get('id')}: {key}")
        hv = t.get("host_version", None)
        if hv is not None:
            if set(hv) != {"recorded", "current", "changed"} or not isinstance(hv["changed"], bool):
                errs.append(f"target {t.get('id')}: host_version")
        for h in t.get("habits", []):
            for key in ("kind", "name", "state", "layer", "detail", "fix"):
                if not isinstance(h.get(key), str):
                    errs.append(f"{t.get('id')} cell {h.get('name')}: {key}")
            if h.get("kind") not in KINDS:
                errs.append(f"{t.get('id')} cell {h.get('name')}: kind {h.get('kind')!r}")
            if h.get("state") not in STATES:
                errs.append(f"{t.get('id')} cell {h.get('name')}: state {h.get('state')!r}")
            else:
                counted[h["state"]] += 1
            if h.get("layer") not in LAYERS:
                errs.append(f"{t.get('id')} cell {h.get('name')}: layer {h.get('layer')!r}")
            if h.get("state") in ("drift", "missing") and not h.get("fix"):
                errs.append(f"{t.get('id')} cell {h.get('name')}: drift/missing without fix")
    if counted != e["summary"]:
        errs.append(f"summary {e['summary']} != counted {counted}")
    return errs


def validate_all_status(a) -> list:
    errs = []
    if not isinstance(a, dict) or a.get("schema") != 1:
        return ["not a schema-1 object"]
    if not isinstance(a.get("generated_at"), str):
        errs.append("generated_at")
    if not isinstance(a.get("environments"), list) or not isinstance(a.get("errors"), list):
        return errs + ["environments / errors"]
    for e in a["environments"]:
        errs += [f"{e.get('env')}: {x}" for x in validate_env_status(e)]
    for err in a["errors"]:
        if not all(isinstance(err.get(k), str) for k in ("env", "message", "fix")):
            errs.append(f"error entry {err}")
    return errs


def toml_value(v):
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(v)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


class Base(unittest.TestCase):
    """A fake $HOME, a state dir and a throwaway git repo, all under one tempdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README").write_text("x\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.root / "AGENTS.md").write_text(RULES, encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "hooks").mkdir()

    def write_manifest(self, targets, environments=None):
        environments = environments if environments is not None else {"test": {"platform": "nomatch"},
                                                                       "other": {"platform": "nomatch"}}
        lines = ["schema = 1", 'roles = ["cli", "bot", "remote"]', 'visibility = ["public", "private"]',
                 "[sources]", f'rules = "{self.root / "AGENTS.md"}"', f'docs = "{self.root / "docs"}"',
                 f'hooks = "{self.root / "hooks"}"']
        for name, spec in environments.items():
            lines.append(f"[environments.{name}]")
            lines += [f"{k} = {toml_value(v)}" for k, v in spec.items()]
        for t in targets:
            t = dict(t)
            expects = t.pop("expect", [])
            lines.append("[[targets]]")
            lines += [f"{k} = {toml_value(v)}" for k, v in t.items()]
            for e in expects:
                lines.append("[[targets.expect]]")
                lines += [f"{k} = {toml_value(v)}" for k, v in e.items()]
        lines += ['[rule_sections."__preamble__"]', 'roles = ["cli", "bot", "remote"]', 'visibility = "public"',
                  '[rule_sections."Shared"]', 'roles = ["cli", "bot", "remote"]', 'visibility = "public"',
                  '[rule_sections."Cli only"]', 'roles = ["cli"]', 'visibility = "private"',
                  "[docs]", "[hooks]"]
        self.manifest = self.root / "manifest.toml"
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.manifest

    def target(self, expect, **extra):
        t = {"id": "test/host", "host": "generic", "env": "test", "roles": ["cli"],
             "rule_limit_bytes": 0, "verified": True, "expect": expect}
        t.update(extra)
        return t

    def doctor(self, env="test"):
        return agent_env.doctor(self.manifest, env, self.repo)

    def cells(self, status, target_id="test/host"):
        return next(t for t in status["targets"] if t["id"] == target_id)["habits"]

    def one(self, expect, **extra):
        self.write_manifest([self.target([expect], **extra)])
        cells = self.cells(self.doctor())
        self.assertEqual(len(cells), 1, cells)
        return cells[0]


class LinkTests(Base):
    EXP = {"kind": "link", "habit": "rules", "path": "~/host/AGENTS.md", "to": "~/src/AGENTS.md"}

    def setUp(self):
        super().setUp()
        (self.home / "src").mkdir()
        (self.home / "src" / "AGENTS.md").write_text("rules\n")
        (self.home / "host").mkdir()

    def test_synced_when_link_points_to_source(self):
        (self.home / "host" / "AGENTS.md").symlink_to("../src/AGENTS.md")
        c = self.one(self.EXP)
        self.assertEqual((c["state"], c["kind"], c["name"]), ("synced", "rules", "AGENTS.md"))

    def test_drift_when_link_points_elsewhere(self):
        (self.home / "other.md").write_text("x")
        (self.home / "host" / "AGENTS.md").symlink_to(self.home / "other.md")
        c = self.one(self.EXP)
        self.assertEqual(c["state"], "drift")
        self.assertIn("ln -sfn ~/src/AGENTS.md ~/host/AGENTS.md", c["fix"])

    def test_drift_when_real_file(self):
        (self.home / "host" / "AGENTS.md").write_text("copy\n")
        c = self.one(self.EXP)
        self.assertEqual(c["state"], "drift")
        self.assertIn("trash", c["fix"])

    def test_missing_when_absent(self):
        c = self.one(self.EXP)
        self.assertEqual(c["state"], "missing")
        self.assertEqual(c["fix"], "ln -s ~/src/AGENTS.md ~/host/AGENTS.md")


class ContainsTests(Base):
    EXP = {"kind": "contains", "habit": "hooks", "name": "hook", "path": "~/hooks.json", "text": "rules.sh"}

    def test_synced(self):
        (self.home / "hooks.json").write_text('{"cmd": "rules.sh"}')
        self.assertEqual(self.one(self.EXP)["state"], "synced")

    def test_drift(self):
        (self.home / "hooks.json").write_text("{}")
        c = self.one(self.EXP)
        self.assertEqual((c["state"], c["kind"], c["name"]), ("drift", "hooks", "hook"))
        self.assertTrue(c["fix"])

    def test_missing(self):
        c = self.one(self.EXP)
        self.assertEqual(c["state"], "missing")
        self.assertTrue(c["fix"])


class GeneratedTests(Base):
    EXP = {"kind": "generated", "habit": "rules"}

    def make(self):
        self.write_manifest([self.target([self.EXP], render="generated", output="~/bot/AGENTS.md")])
        self.out = self.home / "bot" / "AGENTS.md"
        m = agent_env.load_manifest(self.manifest)
        return render.render_rules(m, RULES, "test/host")

    def test_synced(self):
        text = self.make()
        self.out.parent.mkdir()
        self.out.write_text(text, encoding="utf-8")
        [c] = self.cells(self.doctor())
        self.assertEqual((c["state"], c["name"]), ("synced", "AGENTS.md"))

    def test_customized_with_profile(self):
        (self.root / "profile.md").write_text("# Profile\n")
        self.write_manifest([self.target([self.EXP], render="generated", output="~/bot/AGENTS.md",
                                         supplement=str(self.root / "profile.md"))])
        m = agent_env.load_manifest(self.manifest)
        out = self.home / "bot" / "AGENTS.md"
        out.parent.mkdir()
        out.write_text(render.render_rules(m, RULES, "test/host"), encoding="utf-8")
        [c] = self.cells(self.doctor())
        self.assertEqual((c["state"], c["layer"]), ("customized", "env"))

    def test_drift(self):
        self.make()
        self.out.parent.mkdir()
        self.out.write_text("hand edited\n", encoding="utf-8")
        [c] = self.cells(self.doctor())
        self.assertEqual(c["state"], "drift")
        self.assertEqual(c["fix"], "bin/agent-env render --target test/host --write")

    def test_missing(self):
        self.make()
        [c] = self.cells(self.doctor())
        self.assertEqual(c["state"], "missing")
        self.assertTrue(c["fix"])


class AlsoOutputTests(Base):
    EXP = {"kind": "generated", "habit": "rules", "name": ".hermes.md"}

    def setUp(self):
        super().setUp()
        self.write_manifest([self.target([self.EXP], render="generated", output="~/.hermes.md",
                                         also_output=["~/.hermes/AGENTS.md"])])
        self.main, self.extra = self.home / ".hermes.md", self.home / ".hermes" / "AGENTS.md"
        self.extra.parent.mkdir()
        self.text = render.render_rules(agent_env.load_manifest(self.manifest), RULES, "test/host")

    def test_both_synced(self):
        self.main.write_text(self.text, encoding="utf-8")
        self.extra.write_text(self.text, encoding="utf-8")
        cells = self.cells(self.doctor())
        self.assertEqual([(c["name"], c["state"]) for c in cells],
                         [(".hermes.md", "synced"), ("AGENTS.md", "synced")])

    def test_one_missing_names_its_path(self):
        self.main.write_text(self.text, encoding="utf-8")
        status = self.doctor()
        main, extra = self.cells(status)
        self.assertEqual(main["state"], "synced")
        self.assertEqual(extra["state"], "missing")
        self.assertIn("~/.hermes/AGENTS.md", extra["detail"])
        self.assertEqual(extra["fix"], "bin/agent-env render --target test/host --write")
        import issues
        m = agent_env.load_manifest(self.manifest)
        self.assertEqual(issues.habit_source(m, {"id": "test/host", "env": "test"}, extra), "generated")

    def test_one_drifted(self):
        self.main.write_text(self.text, encoding="utf-8")
        self.extra.write_text("old copy\n", encoding="utf-8")
        _, extra = self.cells(self.doctor())
        self.assertEqual(extra["state"], "drift")
        self.assertIn("~/.hermes/AGENTS.md", extra["detail"])

    def test_rollback_restores_every_output(self):
        self.main.write_text("main v1\n", encoding="utf-8")
        self.extra.write_text("extra v1\n", encoding="utf-8")
        snaps = render.snapshot_files([self.main, self.extra], "test/host")
        self.assertEqual(len({s.parent for s in snaps}), 1)  # one folder, so one rollback restores both
        self.main.write_text("main v2\n", encoding="utf-8")
        self.extra.write_text("extra v2\n", encoding="utf-8")
        self.assertEqual(agent_env.rollback(self.manifest, "test/host", out=io.StringIO()), 0)
        self.assertEqual((self.main.read_text(), self.extra.read_text()), ("main v1\n", "extra v1\n"))
        self.assertEqual(agent_env.rollback(self.manifest, "test/host", out=io.StringIO()), 0)
        self.assertEqual((self.main.read_text(), self.extra.read_text()), ("main v2\n", "extra v2\n"))


class DeliveredTests(Base):
    EXP = {"kind": "delivered", "habit": "rules", "name": "cloud rules"}

    def setUp(self):
        super().setUp()
        self.write_manifest([self.target([self.EXP], id="test/bot", env="cloud", roles=["remote"])])

    def cell(self):
        [c] = self.cells(self.doctor(), "test/bot")
        return c

    def record(self):
        out = io.StringIO()
        self.assertEqual(agent_env.deliver(self.manifest, "test/bot", record=True, out=out), 0)

    def test_missing_without_record(self):
        c = self.cell()
        self.assertEqual(c["state"], "missing")
        self.assertIn("--record", c["fix"])

    def test_synced_after_record(self):
        self.record()
        c = self.cell()
        self.assertEqual(c["state"], "synced")
        rec = json.loads((self.root / "state" / "agent-env" / "deliveries" / "test" / "bot.json").read_text())
        self.assertIn(rec["sha256"][:12], c["detail"])

    def test_drift_when_source_changes(self):
        self.record()
        (self.root / "AGENTS.md").write_text(RULES.replace("shared body", "shared body v2"), encoding="utf-8")
        c = self.cell()
        self.assertEqual(c["state"], "drift")
        self.assertTrue(c["fix"])

    def test_print_is_the_role_selection(self):
        out = io.StringIO()
        agent_env.deliver(self.manifest, "test/bot", out=out)
        self.assertIn("shared body", out.getvalue())
        self.assertNotIn("cli body", out.getvalue())


class HostVersionTests(Base):
    def hv(self, cmd, recorded):
        self.write_manifest([self.target([], host_version={"cmd": cmd, "recorded": recorded})])
        return self.doctor()["targets"][0]["host_version"]

    def test_unchanged(self):
        self.assertEqual(self.hv([sys.executable, "-c", "print('tool 1.2.3 (abc)')"], "1.2.3"),
                         {"recorded": "1.2.3", "current": "1.2.3", "changed": False})

    def test_changed(self):
        hv = self.hv([sys.executable, "-c", "print('tool v1.3.0+4.gabc')"], "1.2.3")
        self.assertEqual((hv["current"], hv["changed"]), ("1.3.0+4.gabc", True))

    def test_command_unavailable(self):
        hv = self.hv(["no-such-command-agent-env"], "1.2.3")
        self.assertEqual((hv["current"], hv["changed"]), (None, False))

    def test_absent_is_null(self):
        self.write_manifest([self.target([])])
        self.assertIsNone(self.doctor()["targets"][0]["host_version"])


class PortalTests(Base):
    """kind = "portal": one cell per portal tool with a module file, plus registry and health cells."""

    def setUp(self):
        super().setUp()
        (self.home / "tools").mkdir()
        (self.home / "tools" / "a.ts").write_text("x\n")
        self.registry = self.home / "portal.json"
        self.registry.write_text(json.dumps({"tools": [
            {"id": "skills"},  # built-in page: no module, no cell
            {"id": "alpha", "label": "Alpha", "kind": "panel", "module": "~/tools/a.ts"},
            {"id": "beta", "kind": "panel", "module": "~/tools/gone.ts"},
            {"id": "notes-tool", "label": "Notes Tool", "kind": "panel", "module": "~/tools/notes-tool.ts"},
        ]}), encoding="utf-8")

    def run_portal(self, **extra):
        exp = {"kind": "portal", "habit": "skills", "registry": "~/portal.json"}
        exp.update(extra)
        self.write_manifest([self.target([exp])])
        return {c["name"]: c for c in self.cells(self.doctor())}

    def test_module_present_and_missing(self):
        cells = self.run_portal()
        self.assertEqual(set(cells), {"门户·Alpha", "门户·beta", "门户·Notes Tool"})
        self.assertEqual(cells["门户·Alpha"]["state"], "synced")
        self.assertEqual(cells["门户·beta"]["state"], "missing")
        self.assertIn("~/tools/gone.ts", cells["门户·beta"]["detail"])
        self.assertTrue(cells["门户·beta"]["fix"])
        self.assertEqual(cells["门户·Alpha"]["kind"], "skills")

    def test_skip(self):
        cells = self.run_portal(skip=["notes-tool", "beta"])
        self.assertEqual(set(cells), {"门户·Alpha"})

    def test_registry_missing(self):
        self.registry.unlink()
        cells = self.run_portal(fix="ask first")
        self.assertEqual(list(cells), ["门户"])
        self.assertEqual((cells["门户"]["state"], cells["门户"]["fix"]), ("missing", "ask first"))

    def test_health_url(self):
        import http.server
        import threading

        class Ok(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/" else 503)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        ok = self.run_portal(skip=["beta", "notes-tool"], health_url=f"http://127.0.0.1:{port}/")
        self.assertEqual(ok["门户·服务"]["state"], "synced")
        bad = self.run_portal(skip=["beta", "notes-tool"], health_url=f"http://127.0.0.1:{port}/x")["门户·服务"]
        self.assertEqual(bad["state"], "drift")
        self.assertIn("503", bad["detail"])
        srv.shutdown()
        srv.server_close()
        down = self.run_portal(skip=["beta", "notes-tool"], health_url=f"http://127.0.0.1:{port}/")["门户·服务"]
        self.assertEqual(down["state"], "drift")

    def test_issue_title_and_actor(self):
        self.run_portal()
        status = self.doctor()
        manifest = agent_env._load(self.manifest)
        found = {i["ref"]["name"]: i for i in issues.env_issues(status, manifest)}
        beta = found["门户·beta"]
        self.assertIn("门户里「beta」工具文件找不到", beta["title_zh"])
        self.assertEqual(beta["actor"], "ai")

    def test_check_rejects_portal_on_remote_env(self):
        import habits_check
        exp = {"kind": "portal", "habit": "skills", "registry": "~/portal.json"}
        self.write_manifest([self.target([exp])], {"test": {"transport": "remote-exec", "exec": ["sh"]}})
        msgs = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("portal" in m and "remote-exec" in m for m in msgs), msgs)

    def test_check_accepts_local_portal_and_flags_bad_fields(self):
        import habits_check
        self.write_manifest([self.target([{"kind": "portal", "habit": "skills", "registry": "~/portal.json",
                                           "skip": ["notes-tool"], "health_url": "http://localhost:8080/"}])])
        msgs = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertFalse(any("portal" in m for m in msgs), msgs)
        self.write_manifest([self.target([{"kind": "portal", "habit": "skills", "registry": "/abs/portal.json",
                                           "skip": "notes-tool", "health_url": "env.localhost"}])])
        msgs = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        for field in ("registry", "skip", "health_url"):
            self.assertTrue(any("portal" in m and field in m for m in msgs), (field, msgs))


class StatusTests(Base):
    def setUp(self):
        super().setUp()
        self.write_manifest([self.target([{"kind": "contains", "habit": "rules", "path": "~/x", "text": "y"}]),
                             {"id": "other/host", "host": "generic", "env": "other", "roles": ["cli"],
                              "rule_limit_bytes": 0, "verified": True}])

    def test_live_status_matches_schema(self):
        s = self.doctor()
        self.assertEqual(validate_env_status(s), [])
        self.assertEqual([t["id"] for t in s["targets"]], ["test/host"])  # other env excluded
        self.assertEqual(s["summary"]["missing"], 1)
        self.assertEqual(s["check"]["fail"], 0)

    def test_doctor_exit_code_and_human_output(self):
        out, err = io.StringIO(), io.StringIO()
        code = agent_env.main(["doctor", "--manifest", str(self.manifest), "--env", "test",
                               "--repo", str(self.repo)], out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn("修复：", out.getvalue())
        (self.home / "x").write_text("y")
        out = io.StringIO()
        self.assertEqual(agent_env.main(["doctor", "--manifest", str(self.manifest), "--env", "test",
                                         "--repo", str(self.repo)], out=out, err=err), 0)

    def test_env_inferred_from_manifest(self):
        self.write_manifest([self.target([])], environments={"test": {"platform": sys.platform}})
        self.assertEqual(agent_env.doctor(self.manifest, None, self.repo)["env"], "test")

    def test_commit_leaves_work_tree_alone(self):
        (self.repo / "README").write_text("changed\n")
        (self.repo / "staged").write_text("s\n")
        git(self.repo, "add", "staged")
        (self.repo / "untracked").write_text("u\n")
        before = (git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD"),
                  git(self.repo, "symbolic-ref", "HEAD"), (self.repo / ".git" / "index").read_bytes(),
                  sorted(p.name for p in self.repo.iterdir()))
        status = self.doctor()
        first = agent_env.commit_status(self.repo, status)
        second = agent_env.commit_status(self.repo, self.doctor())
        after = (git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD"),
                 git(self.repo, "symbolic-ref", "HEAD"), (self.repo / ".git" / "index").read_bytes(),
                 sorted(p.name for p in self.repo.iterdir()))
        self.assertEqual(before, after)
        self.assertEqual(git(self.repo, "rev-parse", "status/test").strip(), second)
        self.assertEqual(git(self.repo, "rev-parse", "status/test^").strip(), first)
        self.assertEqual(git(self.repo, "ls-tree", "--name-only", "status/test").split(), ["status.json"])
        stored = json.loads(git(self.repo, "show", "status/test:status.json"))
        self.assertEqual(validate_env_status(stored), [])

    def test_all_merges_branches_and_reports_errors(self):
        other = dict(self.doctor(), env="other", generated_at="2026-11-01T00:00:00Z", targets=[],
                     summary={s: 0 for s in STATES})
        agent_env.commit_status(self.repo, other)
        newer = dict(other, generated_at="2026-11-02T00:00:00Z")
        # The same env on origin/ with a newer report wins over the local branch.
        commit = agent_env.commit_status(self.repo, dict(newer, env="other"))
        git(self.repo, "update-ref", "refs/remotes/origin/status/other", commit)
        git(self.repo, "update-ref", "refs/heads/status/other", "status/other^")
        # A broken branch for a third env is reported, not fatal.
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input="not json", capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(["git", "-C", str(self.repo), "mktree"], input=f"100644 blob {blob}\tstatus.json\n",
                              capture_output=True, text=True).stdout.strip()
        broken = git(self.repo, "commit-tree", tree, "-m", "broken").strip()
        git(self.repo, "update-ref", "refs/heads/status/broken", broken)
        a = agent_env.all_status(self.manifest, "test", self.repo)
        self.assertEqual(validate_all_status(a), [])
        envs = {e["env"]: e for e in a["environments"]}
        self.assertEqual(set(envs), {"test", "other"})
        self.assertEqual(envs["test"]["source"], "live")
        self.assertEqual(envs["other"]["source"], "branch")
        self.assertEqual(envs["other"]["generated_at"], "2026-11-02T00:00:00Z")
        self.assertEqual([e["env"] for e in a["errors"]], ["broken"])

    def test_all_reports_declared_env_without_branch(self):
        a = agent_env.all_status(self.manifest, "test", self.repo)
        self.assertEqual(validate_all_status(a), [])
        self.assertEqual([e["env"] for e in a["errors"]], ["other"])
        self.assertIn("status --commit", a["errors"][0]["fix"])

    def test_status_cli_json_all(self):
        out, err = io.StringIO(), io.StringIO()
        code = agent_env.main(["status", "--json", "--all", "--manifest", str(self.manifest), "--env", "test",
                               "--repo", str(self.repo)], out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(validate_all_status(json.loads(out.getvalue())), [])

    def test_status_all_rechecks_due_repos_first_and_plain_status_does_not(self):
        args = ["--manifest", str(self.manifest), "--env", "test", "--repo", str(self.repo)]
        with mock.patch.object(agent_env.plugins_view, "check_due", return_value=[]) as due:
            agent_env.main(["status", "--json", *args], out=io.StringIO(), err=io.StringIO())
            due.assert_not_called()
            self.assertEqual(agent_env.main(["status", "--json", "--all", *args], out=io.StringIO(), err=io.StringIO()), 0)
            due.assert_called_once()

    def test_push_requires_commit(self):
        err = io.StringIO()
        code = agent_env.main(["status", "--push", "--manifest", str(self.manifest), "--env", "test",
                               "--repo", str(self.repo)], out=io.StringIO(), err=err)
        self.assertEqual(code, 1)


class RollbackTests(Base):
    def setUp(self):
        super().setUp()
        self.write_manifest([self.target([], render="generated", output="~/bot/AGENTS.md")])
        self.file = self.home / "bot" / "AGENTS.md"
        self.file.parent.mkdir()

    def test_rollback_is_reversible(self):
        self.file.write_text("v1\r\n", encoding="utf-8", newline="")
        render.snapshot(self.file, "test/host")
        self.file.write_text("v2\n", encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(agent_env.rollback(self.manifest, "test/host", out=out), 0)
        self.assertEqual(self.file.read_bytes(), b"v1\r\n")
        self.assertIn("可撤销", out.getvalue())
        self.assertEqual(agent_env.rollback(self.manifest, None, out=io.StringIO()), 0)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "v2\n")
        self.assertEqual(agent_env.rollback(self.manifest, "test/host", out=io.StringIO()), 0)
        self.assertEqual(self.file.read_bytes(), b"v1\r\n")

    def test_snapshot_without_meta_uses_manifest_output(self):
        dest = self.root / "state" / "agent-env" / "snapshots" / "20260101T000000Z" / "test" / "host"
        dest.mkdir(parents=True)
        (dest / "AGENTS.md").write_text("old\n")
        self.file.write_text("new\n")
        agent_env.rollback(self.manifest, "test/host", out=io.StringIO())
        self.assertEqual(self.file.read_text(), "old\n")

    def test_no_snapshot_is_an_error(self):
        err = io.StringIO()
        code = agent_env.main(["rollback", "--manifest", str(self.manifest), "--target", "test/host"],
                              out=io.StringIO(), err=err)
        self.assertEqual(code, 1)
        self.assertIn("修复：", err.getvalue())


class AccountsTests(Base):
    def test_unknown_without_sideby(self):
        self.write_manifest([], environments={"test": {"platform": "nomatch", "accounts": "sideby"}})
        with mock.patch.object(agent_env.shutil, "which", return_value=None):
            s = self.doctor()
        [c] = self.cells(s, "test/accounts")
        self.assertEqual(c["state"], "unknown")

    def test_drift_when_sideby_fails(self):
        self.write_manifest([], environments={"test": {"platform": "nomatch", "accounts": "sideby"}})
        fake = self.root / "sideby"
        fake.write_text("#!/bin/sh\necho '{\"status\":\"fail\",\"accounts\":[{\"findings\":[{\"level\":\"fail\"}]}],"
                        "\"general\":[]}'\nexit 1\n")
        fake.chmod(0o755)
        with mock.patch.object(agent_env.shutil, "which", return_value=str(fake)):
            [c] = self.cells(self.doctor(), "test/accounts")
        self.assertEqual((c["state"], c["fix"]), ("drift", "sideby doctor --fix"))


class ManifestCheckTests(Base):
    def test_bad_expect_fails_check(self):
        import habits_check
        self.write_manifest([self.target([{"kind": "link", "habit": "rules", "path": "/abs", "to": "~/x"},
                                          {"kind": "weird", "habit": "rules"}],
                                         host_version={"cmd": "grok", "recorded": "1"})])
        msgs = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("不是 ~ 开头" in m for m in msgs), msgs)
        self.assertTrue(any("kind='weird'" in m for m in msgs), msgs)
        self.assertTrue(any("host_version" in m for m in msgs), msgs)

    def test_bad_relogin_fails_check(self):
        import habits_check
        self.write_manifest([self.target([{"kind": "login", "habit": "rules", "fix": "f", "cmd": ["true"],
                                           "relogin": "zsh login.zsh", "relogin_timeout": 0}])])
        msgs = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any("relogin 不是" in m for m in msgs), msgs)
        self.assertTrue(any("relogin_timeout" in m for m in msgs), msgs)




class DoctorTimeBoundTests(Base):
    """Review F3: probes run side by side, so doctor takes about the slowest probe, not their sum."""

    def test_probes_run_in_parallel_and_keep_order(self):
        import threading
        import time
        ids = [f"test/h{i}" for i in range(4)]
        self.write_manifest([self.target([], id=i) for i in ids])
        started = []
        lock = threading.Lock()
        real = agent_env.target_status

        def slow(manifest, target, rules_text):
            with lock:
                started.append(target["id"])
            time.sleep(0.4)
            return real(manifest, target, rules_text)

        with mock.patch.object(agent_env, "target_status", side_effect=slow):
            t0 = time.monotonic()
            status = self.doctor()
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.2, f"4 probes of 0.4 s took {elapsed:.2f} s: not parallel")
        self.assertEqual([t["id"] for t in status["targets"]], ids)
        self.assertEqual(sorted(started), ids)


if __name__ == "__main__":
    unittest.main()
