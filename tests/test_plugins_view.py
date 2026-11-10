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

import habits  # noqa: E402
import plugins_view as pv  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
