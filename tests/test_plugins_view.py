"""The 插件 tab's data (engine/plugins_view.py): which repositories are looked at, how their visibility is
asked (gh, or the command a [git_hosts] table declares; mocked here) and cached, and what an unknown repository
tells the user to do."""
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import habits  # noqa: E402
import habits_check  # noqa: E402
import issues  # noqa: E402
import plugins  # noqa: E402
import plugins_view as pv  # noqa: E402
from unittest import mock  # noqa: E402
from test_plugins import PERSONAL, PUBLIC, PluginBase  # noqa: E402

SRC = "github.com/me/rules-src"
GEN = "github.com/me/agent-env"
PUB = "github.com/me/plugin-public"
PER = "github.com/me/plugin-personal"
COMPANY = "git.example.com/org/plugin-company"
# how the tests' manifests ask git.example.com: a CLI that prints `status:` and `visibility_level:` lines
GIT_HOSTS = {"git.example.com": {"command": ["tool", "repo", "show", "--repo", "{repo}"],
                                 "visibility": r'^\s*visibility_level:\s*"?([A-Za-z]+)',
                                 "fail": r"^status:\s*(?!200\b)\d+", "login": "tool login"}}
GIT_HOSTS_TOML = ('[git_hosts."git.example.com"]\ncommand = ["tool", "repo", "show", "--repo", "{repo}"]\n'
                  "visibility = '^\\s*visibility_level:\\s*\"?([A-Za-z]+)'\nfail = '^status:\\s*(?!200\\b)\\d+'\n"
                  'login = "tool login"\n')
WORK = "github.com/me/some-work-repo"
LOCAL_ENV = {"test": {"platform": "nomatch"}}


class FakeRun:
    """Stands in for subprocess.run: answers gh / tool from a table of {repo path: (rc, stdout, stderr)}."""

    def __init__(self, answers, missing=()):
        self.answers, self.missing, self.calls = answers, set(missing), []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] in self.missing:
            raise FileNotFoundError(cmd[0])
        path = cmd[3] if cmd[0] == "gh" else cmd[cmd.index("--repo") + 1]
        rc, out, err = self.answers.get(path, (1, "", "not found"))
        return subprocess.CompletedProcess(cmd, rc, out, err)


def gh(vis, empty=False):
    return 0, json.dumps({"visibility": vis, "isEmpty": empty}), ""


class ViewBase(PluginBase):
    def setUp(self):
        super().setUp()
        self.gen_root = self.root / "agent-env"
        (self.gen_root / "manifest").mkdir(parents=True)
        self.repos_file = self.root / "repos.json"
        self.repos_file.write_text(json.dumps({"repos": [
            {"path": str(self.root), "remote": f"git@github.com:{SRC.split('/', 1)[1]}.git"},
            {"path": str(self.gen_root), "remote": f"https://{GEN}.git"},
            {"path": str(self.root / "work"), "remote": f"https://{WORK}"},
        ]}), encoding="utf-8")

    def make(self, company_fallback=True):
        m = self.setup({"pub": dict(PUBLIC, repo=f"https://{PUB}"), "per": dict(PERSONAL, repo=f"https://{PER}")},
                       targets=[self.target([], plugins=["pub", "per"])], environments=LOCAL_ENV)
        if company_fallback:
            path = self.manifest
            path.write_text(path.read_text() + f'[plugin_repos]\ncompany = "https://{COMPANY}"\n\n' + GIT_HOSTS_TOML,
                            encoding="utf-8")
            m = habits.load_manifest(path)
        # the manifest lives inside the generator repository, as manifest/habits.toml does
        self.mpath = self.gen_root / "manifest" / "habits.toml"
        self.mpath.write_text(self.manifest.read_text(), encoding="utf-8")
        return m

    def refresh(self, m, run, now="2026-11-10T00:00:00Z"):
        out = io.StringIO()
        rc = pv.refresh(m, self.mpath, out, repos_file=self.repos_file, run=run, now=now)
        return rc, out.getvalue()

    def view(self, m):
        return pv.view(m, self.mpath, [], self.rules(), repos_file=self.repos_file)


class RepoKeyTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(pv.repo_key("git@github.com:me/x.git"), "github.com/me/x")
        self.assertEqual(pv.repo_key("https://git.example.com/org/team/x"), "git.example.com/org/team/x")
        self.assertEqual(pv.repo_key("ssh://git@github.com:22/me/x"), "github.com/me/x")
        self.assertIsNone(pv.repo_key("not a remote"))
        self.assertIsNone(pv.repo_key(None))


class LayoutTests(ViewBase):
    def test_only_plugin_system_repos_are_probed(self):
        m = self.make()
        keys = pv.layout(m, self.mpath, self.repos_file)["keys"]
        self.assertEqual(keys, [SRC, GEN, PUB, PER, COMPANY])
        run = FakeRun({})
        self.refresh(m, run)
        asked = sorted(c[3] if c[0] == "gh" else c[c.index("--repo") + 1] for c in run.calls)
        self.assertEqual(asked, sorted(k.split("/", 1)[1] for k in keys))
        self.assertNotIn(WORK.split("/", 1)[1], asked)

    def test_fallback_repo_shows_with_no_plugin(self):
        m = self.make()
        card = next(c for c in pv.layout(m, self.mpath, self.repos_file)["plugin_repos"] if c["visibility"] == "company")
        self.assertEqual((card["repo"], card["plugins"]), (COMPANY, []))


class ProbeTests(unittest.TestCase):
    def test_gh_public_private_and_empty(self):
        self.assertEqual(pv.probe(PUB, FakeRun({"me/plugin-public": gh("PUBLIC", True)}))["visibility"], "public")
        r = pv.probe(PUB, FakeRun({"me/plugin-public": gh("INTERNAL")}))
        self.assertEqual((r["visibility"], r["empty"], r["via"]), ("private", False, "gh"))

    def test_a_declared_host(self):
        out = "status: 200\ndata:\n  visibility_level: Private\n"
        r = pv.probe(COMPANY, FakeRun({"org/plugin-company": (0, out, "")}), GIT_HOSTS)
        self.assertEqual((r["visibility"], r["via"]), ("private", "tool"))
        out = "status: 200\ndata:\n  visibility_level: Public\n"
        self.assertEqual(pv.probe(COMPANY, FakeRun({"org/plugin-company": (0, out, "")}), GIT_HOSTS)["visibility"], "public")
        r = pv.probe(COMPANY, FakeRun({"org/plugin-company": (0, "status: 404\n  visibility_level: Private\n", "")}),
                     GIT_HOSTS)
        self.assertEqual(r["visibility"], "unknown", "fail matched: the lookup failed whatever else it printed")
        self.assertIn("tool login", r["fix"])

    def test_an_undeclared_host_says_how_to_declare_it(self):
        r = pv.probe(COMPANY, FakeRun({"org/plugin-company": (0, "visibility_level: Public\n", "")}))
        self.assertEqual(r["visibility"], "unknown")
        self.assertIn("只会查 github.com）", r["error"])
        self.assertIn('[git_hosts."git.example.com"]', r["fix"])

    def test_git_host_errors(self):
        self.assertEqual(pv.git_host_errors("h", GIT_HOSTS["git.example.com"]), [])
        bad = [{"command": ["tool"], "visibility": "(x)"}, {"command": ["tool", "{repo}"], "visibility": "x"},
               {"command": ["tool", "{repo}"], "visibility": "(", "fail": "("}, "not a table"]
        for spec in bad:
            with self.subTest(spec=spec):
                errs = pv.git_host_errors("h", spec)
                self.assertTrue(errs and all(fix for _, fix in errs), errs)
        self.assertEqual(pv.git_hosts({"git_hosts": {"ok": GIT_HOSTS["git.example.com"], "bad": bad[0]}}),
                         {"ok": GIT_HOSTS["git.example.com"]})

    def test_failures_are_unknown_with_a_fix(self):
        cases = [pv.probe(PUB, FakeRun({}, missing={"gh"})),
                 pv.probe(PUB, FakeRun({"me/plugin-public": (1, "", "HTTP 401")})),
                 pv.probe(PUB, FakeRun({"me/plugin-public": (0, "{}", "")})),
                 pv.probe("gitlab.com/me/x", FakeRun({}))]
        for r in cases:
            self.assertEqual(r["visibility"], "unknown")
            self.assertTrue(r["error"] and r["fix"], r)
        self.assertIn("gh auth login", cases[1]["fix"])
        self.assertIn(pv.REFRESH_COMMAND, cases[1]["fix"])

    def test_timeout(self):
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertIn("秒没有返回", pv.probe(PUB, slow)["error"])


class CacheTests(ViewBase):
    def answers(self):
        return {"me/rules-src": gh("PRIVATE"), "me/agent-env": gh("PRIVATE"), "me/plugin-public": gh("PUBLIC", True),
                "me/plugin-personal": gh("PRIVATE"),
                "org/plugin-company": (0, "status: 200\n  visibility_level: Private\n", "")}

    def test_view_reads_cache_and_never_probes(self):
        m = self.make()
        rc, out = self.refresh(m, FakeRun(self.answers()))
        self.assertEqual(rc, 0, out)
        self.assertIn("已写入", out)
        v = self.view(m)
        self.assertNotIn("error", v)
        repos = {r["key"]: r for r in v["repos"]}
        self.assertEqual(repos[PUB]["visibility"], "public")
        self.assertTrue(repos[PUB]["empty"])
        self.assertEqual(repos[COMPANY]["visibility"], "private")
        self.assertEqual(repos[PUB]["checked_at"], "2026-11-10T00:00:00Z")
        self.assertEqual(v["checked_at"], "2026-11-10T00:00:00Z")
        pub = next(c for c in v["plugin_repos"] if c["visibility"] == "public")
        self.assertIn("empty_repo", [n["code"] for n in pub["notes"]])

    def test_failed_refresh_keeps_the_last_answer(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))
        rc, out = self.refresh(m, FakeRun({}, missing={"gh", "tool"}), now="2026-11-11T00:00:00Z")
        self.assertEqual(rc, 1)
        self.assertIn("保留上次查到的", out)
        repos = {r["key"]: r for r in self.view(m)["repos"]}
        self.assertEqual(repos[PER]["visibility"], "private")
        self.assertEqual(repos[PER]["checked_at"], "2026-11-10T00:00:00Z")

    def test_unknown_repo_carries_the_fix(self):
        m = self.make()
        repos = {r["key"]: r for r in self.view(m)["repos"]}  # never refreshed
        for r in repos.values():
            self.assertEqual(r["visibility"], "unknown")
            self.assertEqual(r["fix"], pv.REFRESH_COMMAND)
            self.assertTrue(r["detail"])
        self.refresh(m, FakeRun({}, missing={"gh", "tool"}))
        r = {r["key"]: r for r in self.view(m)["repos"]}[PUB]
        self.assertIn("没装 gh", r["detail"])
        self.assertIn("gh auth login", r["fix"])

    def test_public_repo_holding_private_plugin_fails(self):
        m = self.make()
        ans = self.answers()
        ans["me/plugin-personal"] = gh("PUBLIC")
        self.refresh(m, FakeRun(ans))
        per = next(c for c in self.view(m)["plugin_repos"] if c["visibility"] == "personal")
        self.assertEqual(per["notes"][0]["code"], "exposed")
        self.assertEqual(per["notes"][0]["state"], "fail")

    def test_show_prints_cache_without_probing(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))
        out = io.StringIO()
        self.assertEqual(pv.show(m, self.mpath, out, repos_file=self.repos_file), 0)
        text = out.getvalue()
        self.assertIn(f"公开  {PUB}（空仓）", text)
        self.assertIn(f"私有  {COMPANY}", text)
        self.assertIn(pv.REFRESH_COMMAND, text)



class DueTests(CacheTests):
    """`status --all` re-asks only what the cache lacks or has stale (check_due); publish clears 空仓 (mark_pushed)."""

    def check(self, m, run, now="2026-11-10T01:00:00Z", budget=pv.DUE_BUDGET):
        return pv.check_due(m, self.mpath, repos_file=self.repos_file, run=run, now=now, budget=budget)

    def test_fresh_cache_asks_nobody(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))
        run = FakeRun(self.answers())
        self.assertEqual(self.check(m, run), [])
        self.assertEqual(run.calls, [])

    def test_a_renamed_repo_is_asked_alone(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))
        m["plugins"]["per"]["repo"] = "https://github.com/me/plugin-personal-v2"  # renamed in the manifest
        new = "github.com/me/plugin-personal-v2"
        self.assertEqual({r["key"]: r for r in self.view(m)["repos"]}[new]["visibility"], "unknown")
        run = FakeRun({"me/plugin-personal-v2": gh("PRIVATE")})
        self.assertEqual(self.check(m, run), [new])
        self.assertEqual([c[3] for c in run.calls], ["me/plugin-personal-v2"])
        repos = {r["key"]: r for r in self.view(m)["repos"]}
        self.assertEqual(repos[new]["visibility"], "private")
        self.assertEqual(repos[new]["checked_at"], "2026-11-10T01:00:00Z")
        self.assertEqual(repos[PUB]["checked_at"], "2026-11-10T00:00:00Z")  # the others are kept as they were

    def test_a_stale_cache_is_asked_again(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))
        ans = self.answers()
        ans["me/plugin-public"] = gh("PUBLIC", False)
        run = FakeRun(ans)
        later = "2026-11-11T00:00:01Z"  # just over STALE_AFTER
        self.assertEqual(len(self.check(m, run, now=later)), 5)
        pub = next(c for c in self.view(m)["plugin_repos"] if c["visibility"] == "public")
        self.assertNotIn("empty_repo", [n["code"] for n in pub["notes"]])

    def test_network_failure_keeps_what_the_panel_showed(self):
        m = self.make()
        self.assertEqual(self.check(m, FakeRun({}, missing={"gh", "tool"})), [])
        self.assertFalse(pv.cache_path().exists())  # nothing learned, nothing written
        for r in self.view(m)["repos"]:
            self.assertEqual((r["visibility"], r["fix"]), ("unknown", pv.REFRESH_COMMAND))
        self.refresh(m, FakeRun(self.answers()))
        self.assertEqual(self.check(m, FakeRun({}), now="2026-11-12T00:00:00Z"), [])  # stale, host says no
        repos = {r["key"]: r for r in self.view(m)["repos"]}
        self.assertEqual((repos[PER]["visibility"], repos[PER]["checked_at"]), ("private", "2026-11-10T00:00:00Z"))

    def test_a_slow_host_does_not_hold_the_panel(self):
        import time
        m = self.make()

        def slow(cmd, **kw):
            time.sleep(3)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"visibility": "PUBLIC"}), "")
        start = time.monotonic()
        self.assertEqual(self.check(m, slow, budget=0.2), [])
        self.assertLess(time.monotonic() - start, 2.5)

    def test_mark_pushed_clears_empty_and_keeps_the_rest(self):
        m = self.make()
        self.refresh(m, FakeRun(self.answers()))  # PUB is cached as empty
        pv.mark_pushed(f"https://{PUB}.git", None, now="2026-11-10T02:00:00Z")
        repos = {r["key"]: r for r in self.view(m)["repos"]}
        self.assertIs(repos[PUB]["empty"], False)
        self.assertEqual(repos[PUB]["checked_at"], "2026-11-10T00:00:00Z")  # visibility itself was not re-asked
        self.assertEqual(repos[COMPANY]["visibility"], "private")
        pub = next(c for c in self.view(m)["plugin_repos"] if c["visibility"] == "public")
        self.assertNotIn("empty_repo", [n["code"] for n in pub["notes"]])
        pv.mark_pushed(f"https://{PER}", {"visibility": "private", "empty": True, "via": "gh"}, now="2026-11-10T03:00:00Z")
        per = {r["key"]: r for r in self.view(m)["repos"]}[PER]
        self.assertEqual((per["visibility"], per["empty"], per["checked_at"]), ("private", False, "2026-11-10T03:00:00Z"))
        pv.mark_pushed("not a url")  # never raises


class EnvironmentTests(ViewBase):
    def test_rows_and_others(self):
        m = self.make()
        v = self.view(m)
        rows = v["environments"]
        self.assertEqual([r["env"] for r in rows], ["test", None])
        test = rows[0]
        self.assertEqual(test["mode"], "plugins")
        self.assertEqual(test["gets"]["public"], {"state": "gets", "plugins": ["pub"]})
        self.assertEqual(test["gets"]["personal"], {"state": "gets", "plugins": ["per"]})
        self.assertEqual(test["gets"]["company"]["state"], "none")
        self.assertEqual(rows[1]["gets"]["public"]["plugins"], ["pub"])
        self.assertEqual(rows[1]["gets"]["personal"]["state"], "none")

    def test_view_never_raises(self):
        v = pv.view({"plugins": "broken", "environments": 3}, self.root / "x.toml", None, None)
        self.assertIn("refresh_command", v)


class PublishFromTests(ViewBase):
    """`publish_from` names the machine that builds and publishes. On that machine, and without the key, the
    view is what it always was; on every other machine a build or push that is not there is nobody's problem."""

    # two of the user's own machines (--env says which one this is) and a bot checked from either
    ENVS = {"test": {"platform": "nomatch"}, "other": {"platform": "nomatch"},
            "cloud": {"transport": "cloud-hosted", "deny_visibility": ["company"]}}
    BUILD_ONE, BUILD_ALL = "bin/agent-env plugin build --plugin", "bin/agent-env plugin build --all"
    PUSH_ONE, PUSH_ALL = "bin/agent-env plugin publish --plugin", "bin/agent-env plugin publish --all"
    HIDDEN = {"not_built", "stale_build", "stale_push", "not_pushed"}

    def make(self, publish_from=None, targets=None):
        specs = {"pub": dict(PUBLIC, repo=f"https://{PUB}"), "per": dict(PERSONAL, repo=f"https://{PER}"),
                 "patch": {"visibility": "local", "sections": ["Cli only"]}}
        targets = targets if targets is not None else [self.target([], plugins=["pub", "per", "patch"])]
        self.setup(specs, targets=targets + [self.bot(["pub"])], environments=self.ENVS)
        head = "" if publish_from is None else f"publish_from = {json.dumps(publish_from)}\n"
        self.manifest.write_text(head + self.manifest.read_text() + '[env_names]\ntest = "Laptop"\n', encoding="utf-8")
        self.mpath = self.gen_root / "manifest" / "habits.toml"
        self.mpath.write_text(self.manifest.read_text(), encoding="utf-8")
        return habits.load_manifest(self.manifest)

    def view(self, m, env=None):
        return pv.view(m, self.mpath, [], self.rules(), repos_file=self.repos_file, env=env)

    def empty_public_repo(self, m):
        self.refresh(m, FakeRun({"me/rules-src": gh("PRIVATE"), "me/agent-env": gh("PRIVATE"),
                                 "me/plugin-public": gh("PUBLIC", True), "me/plugin-personal": gh("PRIVATE")}))

    def notes(self, v):
        return {c["visibility"]: {n["code"]: n for n in c["notes"]} for c in v["plugin_repos"]}

    def test_without_the_key_nothing_changes_on_any_machine(self):
        m = self.make()
        self.empty_public_repo(m)
        v = self.view(m)
        self.assertNotIn("error", v)
        self.assertNotIn("publisher", v)
        states = {p["name"]: p for p in v["plugins"]}
        self.assertEqual((states["pub"]["build"]["state"], states["pub"]["build"]["fix"]),
                         ("missing", f"{self.BUILD_ONE} pub"))
        self.assertEqual(states["pub"]["publish"], {"state": "not_pushed", "detail": "还没开始发布",
                                                    "fix": f"{self.PUSH_ONE} pub", "pushed_at": None, "steps": {}})
        self.assertEqual(states["patch"]["publish"]["state"], "local")
        public = self.notes(v)["public"]
        self.assertEqual(list(public), ["not_built", "empty_repo", "not_pushed"])
        self.assertEqual(public["not_built"], {"code": "not_built", "state": "missing", "text_zh": "未生成",
                                               "fix": self.BUILD_ALL})
        self.assertEqual(public["empty_repo"], {"code": "empty_repo", "state": "drift", "text_zh": "空仓", "fix": ""})
        for env in ("test", "other", "cloud"):
            self.assertEqual(self.view(m, env), v, env)

    def test_on_the_publishing_machine_the_view_is_the_same_as_without_the_key(self):
        plain = self.make()
        self.empty_public_repo(plain)
        plugins.build(plain, "per", self.rules())  # one built, the others not: every state is in the view
        before = self.view(plain, "test")
        m = self.make("test")
        self.assertEqual(self.view(m, "test"), before)
        self.assertIn(self.BUILD_ALL, json.dumps(before))
        self.assertIn(self.PUSH_ALL, json.dumps(before))

    def test_on_another_machine_nothing_is_missing_and_no_command_is_offered(self):
        m = self.make("test")
        self.empty_public_repo(m)
        v = self.view(m, "other")
        self.assertNotIn("error", v)
        self.assertEqual(v["publisher"], {"env": "test", "name": "Laptop"})
        states = {p["name"]: p for p in v["plugins"]}
        for name in ("pub", "per"):
            self.assertEqual(states[name]["build"], {"state": "elsewhere", "detail": "由 Laptop 生成并发布，这台机器不生成",
                                                     "fix": ""})
            self.assertEqual(states[name]["publish"], {"state": "elsewhere", "detail": "由 Laptop 推送，这台机器不推送",
                                                       "fix": "", "pushed_at": None, "steps": {}})
        # the patch layer: this machine composes its rules from the rules source, never from a build
        self.assertEqual((states["patch"]["build"]["state"], states["patch"]["build"]["fix"]), ("not_needed", ""))
        self.assertIn("直接读规则源", states["patch"]["build"]["detail"])
        self.assertEqual(states["patch"]["publish"]["state"], "local")
        notes = self.notes(v)
        for vis in notes:
            self.assertFalse(self.HIDDEN & set(notes[vis]), (vis, notes[vis]))
        # what the host said about the repository still shows, and the fix names the machine, not a command
        self.assertEqual(notes["public"]["empty_repo"], {"code": "empty_repo", "state": "drift", "text_zh": "空仓",
                                                         "fix": "到 Laptop 上让 AI 发布", "at": "test"})
        repos = {r["key"]: r for r in v["repos"]}
        self.assertEqual((repos[PUB]["visibility"], repos[PUB]["empty"]), ("public", True))
        text = json.dumps(v, ensure_ascii=False)
        self.assertNotIn("plugin build", text)
        self.assertNotIn("plugin publish", text)
        self.assertNotIn('"missing"', text)

    def test_on_another_machine_an_old_build_and_an_old_record_are_not_problems_either(self):
        m = self.make("test")
        plugins.build(m, "pub", self.rules())
        self.assertEqual({p["name"]: p["build"]["state"] for p in self.view(m, "other")["plugins"]}["pub"], "synced",
                         "a build that is here and current is still a fact")
        ledger = {"plugins": {"pub": {"input": "old", "steps": {"build": "t", "scan": "t", "commit": "t",
                                                               "push": "2026-11-01T00:00:00Z"}}}}
        plugins.ledger_path().write_text(json.dumps(ledger), encoding="utf-8")
        self.skill("third", "Comes from upstream, updated.")  # the sources moved on: the build here is old
        mine = {p["name"]: p for p in self.view(m, "test")["plugins"]}["pub"]
        self.assertEqual((mine["build"]["state"], mine["publish"]["state"]), ("drift", "stale"))
        self.assertEqual(set(self.notes(self.view(m, "test"))["public"]), {"stale_build", "stale_push"})
        away = {p["name"]: p for p in self.view(m, "other")["plugins"]}["pub"]
        self.assertEqual((away["build"]["state"], away["build"]["fix"]), ("elsewhere", ""))
        self.assertEqual((away["publish"]["state"], away["publish"]["fix"]), ("elsewhere", ""))
        self.assertEqual(self.notes(self.view(m, "other"))["public"], {})

    def test_a_problem_in_the_sources_shows_on_every_machine(self):
        m = self.make("test")
        m["plugins"]["pub"]["sections"] = ["No such section"]
        for env in ("test", "other"):
            build = {p["name"]: p["build"] for p in self.view(m, env)["plugins"]}["pub"]
            self.assertEqual(build["state"], "drift", env)
            self.assertTrue(build["fix"], env)
            self.assertNotIn("plugin build", build["fix"], env)

    def test_which_machine_this_is(self):
        m = self.make("test")
        self.assertIsNone(plugins.published_away(m, "test"))
        self.assertEqual(plugins.published_away(m, "other"), {"env": "test", "name": "Laptop"})
        # a bot is checked from one of the machines: the one this machine matches decides
        with mock.patch.object(plugins, "machine_env", lambda manifest: "test"):
            self.assertIsNone(plugins.published_away(m, "cloud"))
            self.assertIsNone(plugins.published_away(m))
        with mock.patch.object(plugins, "machine_env", lambda manifest: "other"):
            self.assertEqual(plugins.published_away(m, "cloud")["env"], "test")
        self.assertEqual(plugins.published_away(m)["env"], "test", "a machine no environment matches does not publish")
        self.assertIsNone(plugins.published_away(self.make(), "other"))
        real = {"environments": {"here": {"platform": sys.platform}, "there": {"platform": "nomatch"}}}
        self.assertEqual(habits.machine_env(real), "here")
        self.assertEqual(agent_env.detect_env(real, None), "here")
        self.assertIsNone(plugins.published_away(dict(real, publish_from="here")))
        self.assertEqual(plugins.published_away(dict(real, publish_from="there")), {"env": "there", "name": "there"})
        self.assertIsNone(plugins.published_away(dict(real, publish_from="nowhere")))

    def test_status_all_tells_the_view_which_machine_it_is(self):
        self.make("test")
        for env, publisher in (("test", None), ("other", {"env": "test", "name": "Laptop"})):
            view = agent_env.all_status(self.manifest, env, self.repo)["plugins_view"]
            self.assertNotIn("error", view)
            self.assertEqual(view.get("publisher"), publisher, env)

    def test_doctor_does_not_ask_another_machine_to_build(self):
        expect = [{"kind": "plugins", "habit": "rules"}]
        targets = [self.target(expect, plugins=["pub", "patch"]),
                   self.target(expect, id="other/host", env="other", plugins=["pub", "patch"])]
        m = self.make("test", targets)
        self.assertEqual([r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"], [])
        mine = {c["name"]: c for c in self.cells(self.doctor("test"))}
        self.assertEqual((mine["插件 pub（公开）"]["state"], mine["插件 pub（公开）"]["fix"]),
                         ("missing", f"{self.BUILD_ONE} pub"))
        status = self.doctor("other")
        away = {c["name"]: c for c in self.cells(status, "other/host")}
        self.assertEqual((away["插件 pub（公开）"]["state"], away["插件 pub（公开）"]["fix"]), ("unknown", ""))
        self.assertIn("由 Laptop 生成并发布", away["插件 pub（公开）"]["detail"])
        self.assertEqual((away["插件 patch（本机）"]["state"], away["插件 patch（本机）"]["fix"]), ("unknown", ""))
        self.assertEqual(issues.build([status], [], m), [], "nothing to do on a machine that does not build")
        self.assertTrue(issues.build([self.doctor("test")], [], m))
        plugins.build(m, "pub", self.rules())
        self.assertEqual({c["name"]: c["state"] for c in self.cells(self.doctor("other"), "other/host")}["插件 pub（公开）"],
                         "synced")

    # ── review round: the machine's identity reaches every check, and the to-do list sends work to the publisher ──

    def same_platform(self, publish_from="test"):
        """`other` and `test` both match this machine, `other` first: only --env tells which one it is."""
        envs = {"other": {"platform": sys.platform}, "test": {"platform": sys.platform},
                "cloud": {"transport": "cloud-hosted", "deny_visibility": ["company"]}}
        with mock.patch.object(type(self), "ENVS", envs):
            return self.make(publish_from)

    def plugin_cell(self, status, target="cloud/bot"):
        return {c["name"]: c for c in self.cells(status, target)}["插件 pub（公开）"]

    def test_env_decides_which_machine_this_is_for_the_cloud_checks_too(self):
        m = self.same_platform()
        self.assertEqual(habits.machine_env(m), "other", "the first match is not the publishing machine")
        all_ = agent_env.all_status(self.manifest, "test", self.repo)  # status --env test --all
        view = all_["plugins_view"]
        self.assertNotIn("publisher", view)
        self.assertEqual({p["name"]: p["build"]["state"] for p in view["plugins"]}["pub"], "missing")
        cloud = next(e for e in all_["environments"] if e["env"] == "cloud")
        self.assertEqual(cloud["checked_from"], "test")
        cell = self.plugin_cell(cloud)
        self.assertEqual((cell["state"], cell["fix"]), ("missing", f"{self.BUILD_ONE} pub"),
                         "the cloud check runs on the publishing machine: a missing build is missing")
        todo = next(i for i in all_["issues"] if i["target"] == "cloud/bot" and i["ref"]["name"] == cell["name"])
        self.assertEqual((todo["actor"], todo["steps"][0]["command"]), ("ai", f"{self.BUILD_ONE} pub"))
        self.assertFalse(any("at" in i for i in all_["issues"]))
        # the same machine asked as the other environment does not build
        other = agent_env.all_status(self.manifest, "other", self.repo)
        self.assertEqual(other["plugins_view"]["publisher"], {"env": "test", "name": "Laptop"})
        cell = self.plugin_cell(next(e for e in other["environments"] if e["env"] == "cloud"))
        self.assertEqual((cell["state"], cell["fix"]), ("unknown", ""))
        # one environment alone: the caller's machine wins over the first match
        self.assertEqual(self.plugin_cell(agent_env.doctor(self.manifest, "cloud", self.repo, "test"))["state"], "missing")
        self.assertEqual(self.plugin_cell(agent_env.doctor(self.manifest, "cloud", self.repo))["state"], "unknown")
        # and without the key it never mattered
        self.same_platform(None)
        plain = agent_env.all_status(self.manifest, "test", self.repo)
        self.assertEqual(self.plugin_cell(next(e for e in plain["environments"] if e["env"] == "cloud"))["state"], "missing")

    def assert_sent_to_the_publisher(self, issue):
        self.assertEqual((issue["actor"], issue["ai_task"]), ("other_env", None), issue)
        self.assertEqual(issue["at"], {"env": "test", "name": "Laptop"})
        self.assertEqual([(s["who"], s["command"], s["confirm"]) for s in issue["steps"]], [("other_env", None, False)])
        self.assertIn("到 Laptop 上让 AI 处理", issue["steps"][0]["text_zh"])
        self.assertNotIn("bin/agent-env", json.dumps(issue, ensure_ascii=False))

    def test_todo_for_a_missing_pointer_record_sends_another_machine_to_the_publisher(self):
        m = self.make("test")
        away = plugins.published_away(m, "other")
        status = agent_env.doctor(self.manifest, "cloud", self.repo, "test")  # as the publisher reported it
        mine = {i["ref"]["name"]: i for i in issues.build([status], [], m)}
        self.assertEqual(mine["指路规则"]["actor"], "user")
        self.assertEqual(mine["指路规则"]["steps"][0]["command"], self.BUILD_ALL)
        self.assertEqual(mine["插件 pub（公开）"]["steps"][0]["command"], f"{self.BUILD_ONE} pub")
        theirs = {i["ref"]["name"]: i for i in issues.build([dict(status, source="branch")], [], m, away=away)}
        self.assert_sent_to_the_publisher(theirs["指路规则"])     # build, pointer, record: all on the publisher
        self.assert_sent_to_the_publisher(theirs["插件 pub（公开）"])  # the publisher's missing build, seen from here
        self.assertEqual(theirs["指路规则"]["title_zh"], mine["指路规则"]["title_zh"])
        # nothing changes without the key, or on the publishing machine
        plain = self.make()
        self.assertIsNone(plugins.published_away(plain, "other"))
        self.assertEqual(issues.build([status], [], plain, away=plugins.published_away(plain, "other")),
                         issues.build([status], [], m, away=plugins.published_away(m, "test")))

    def test_todo_from_a_report_without_expect_kind_is_sent_to_the_publisher_by_its_fix(self):
        """A machine still on an older engine reports cells without `expect_kind` (the two machines are not
        upgraded at the same moment). The check behind a plugin cell is then a guess, and the guess is `other`."""
        m = self.make("test")
        away = plugins.published_away(m, "other")
        old = agent_env.doctor(self.manifest, "cloud", self.repo, "test")  # as the publisher reported it
        target = next(t for t in old["targets"] if t["id"] == "cloud/bot")
        for h in target["habits"]:
            self.assertIn("expect_kind", h)
            del h["expect_kind"]
        cell = self.plugin_cell(old)
        self.assertEqual((cell["state"], cell["fix"]), ("missing", f"{self.BUILD_ONE} pub"))
        self.assertEqual(issues.habit_source(m, target, cell), "other")
        # on the publisher the guess gives what it always gave: the cell's fix, for this machine's AI
        mine = {i["ref"]["name"]: i for i in issues.build([old], [], m)}
        self.assertEqual((mine[cell["name"]]["actor"], "at" in mine[cell["name"]]), ("ai", False))
        self.assertIn(f"{self.BUILD_ONE} pub", json.dumps(mine[cell["name"]], ensure_ascii=False))
        # on another machine no to-do of this report keeps a command that builds, publishes or writes the pointer
        theirs = {i["ref"]["name"]: i for i in issues.build([dict(old, source="branch")], [], m, away=away)}
        self.assertEqual(set(theirs), set(mine))
        self.assert_sent_to_the_publisher(theirs[cell["name"]])
        self.assert_sent_to_the_publisher(theirs["指路规则"])
        for i in theirs.values():
            self.assertFalse(plugins.needs_publisher(json.dumps(i, ensure_ascii=False)), i)
        # a fix in words that names the command counts too; a fix that names none stays where it was
        for fix, sent in ((f"先运行 {self.PUSH_ALL} 演练，确认后加 --yes", True), ("在清单里把 sections 改对", False)):
            odd = dict(old, source="branch", targets=[dict(target, habits=[dict(cell, fix=fix)])])
            got = issues.build([odd], [], m, away=away)[0]
            self.assertEqual("at" in got, sent, fix)
            self.assertEqual(got["actor"], "other_env" if sent else "ai", fix)

    def test_todo_for_a_wrong_manifest_entry_keeps_the_manifests_fix_on_another_machine(self):
        expect = [{"kind": "plugins", "habit": "rules"}]
        m = self.make("test", [self.target(expect, id="other/host", env="other", plugins=["pub", "patch"])])
        text = self.manifest.read_text(encoding="utf-8")
        self.assertIn('sections = ["__preamble__", "Shared"]', text)
        self.manifest.write_text(text.replace('sections = ["__preamble__", "Shared"]', 'sections = ["missing"]'),
                                 encoding="utf-8")
        m = habits.load_manifest(self.manifest)
        status = self.doctor("other")
        cell = self.plugin_cell(status, "other/host")
        self.assertEqual(cell["state"], "drift")
        self.assertTrue(cell["fix"])
        self.assertNotIn("plugin build", cell["fix"])
        issue = next(i for i in issues.build([status], [], m, away=plugins.published_away(m, "other"))
                     if i["ref"]["name"] == cell["name"])
        self.assertEqual((issue["actor"], issue["ai_task"]), ("ai", "fix"))
        self.assertNotIn("at", issue)
        self.assertEqual(issue["steps"][0]["text_zh"], issues.spaced(f"按修法处理：{cell['fix']}"))
        self.assertNotIn("plugin build", json.dumps(issue, ensure_ascii=False))
        self.assertEqual(issue["steps"][-1]["command"], "bin/agent-env doctor")

    def test_remote_and_bot_rows_of_the_tab_send_another_machine_to_the_publisher(self):
        m = self.make("test")
        status = agent_env.doctor(self.manifest, "cloud", self.repo, "test")
        row = lambda env: next(r for r in pv.view(m, self.mpath, [status], self.rules(), repos_file=self.repos_file,
                                                  env=env)["environments"] if r["env"] == "cloud")
        mine = {n["code"]: n for n in row("test")["notes"]}["pointer_missing"]
        self.assertNotIn("at", mine)
        self.assertIn("bin/agent-env plugin pointer", mine["fix"])
        self.assertEqual({n["code"]: n for n in row("other")["notes"]}["pointer_missing"],
                         {"code": "pointer_missing", "state": "missing", "text_zh": "指路规则未投递",
                          "fix": "到 Laptop 上让 AI 处理", "at": "test"})

    def test_check_refuses_a_publish_from_that_is_not_one_of_the_users_machines(self):
        def fails():
            return [(r["message"], r["fix"]) for r in habits_check.run_checks(self.manifest).results
                    if r["level"] == "FAIL"]

        self.make("test")
        self.assertEqual(fails(), [])
        passed = [r["message"] for r in habits_check.run_checks(self.manifest).results if r["level"] == "PASS"]
        self.assertTrue(any("publish_from" in msg and "test" in msg for msg in passed), passed)
        for bad, why in (("ghost", "不是 [environments] 里的环境"), ("cloud", "远程或云端托管环境"), (3, "不是环境名"),
                         ("", "不是环境名")):
            self.make(bad)
            found = fails()
            self.assertEqual(len(found), 1, (bad, found))
            self.assertIn("publish_from", found[0][0])
            self.assertIn(why, found[0][0])
            self.assertIn("['other', 'test']", found[0][1])
            # a value check refuses changes nothing: no machine stops reporting its builds because of it
            self.assertIsNone(plugins.published_away(habits.load_manifest(self.manifest), "other"), bad)
        self.make()
        self.assertEqual(fails(), [])
        self.assertEqual(plugins.publish_from_errors({"publish_from": "x"})[0][1],
                         "先在清单登记 [environments.<名字>]，或删掉这一行")


if __name__ == "__main__":
    unittest.main()
