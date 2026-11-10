import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

import habits  # noqa: E402
import habits_check  # noqa: E402

RULES = """# Title

Intro line.

## Alpha

alpha body

```md
## Not a heading
```

## Beta

beta body

## Gamma

gamma body
"""

SECTIONS = {
    "__preamble__": ('["cli", "bot"]', "public"),
    "Alpha": ('["cli", "bot"]', "public"),
    "Beta": ('["cli"]', "private"),
    "Gamma": ('["bot"]', "public"),
}


def build_fixture(root: Path, *, rules=RULES, sections=None, docs=None, doc_files=None,
                  hooks=None, hook_files=None, targets=None):
    sections = SECTIONS if sections is None else sections
    docs = {"a.md": "public", "sub/b.md": "private"} if docs is None else docs
    doc_files = list(docs) if doc_files is None else doc_files
    hooks = {"guard.py": "public"} if hooks is None else hooks
    hook_files = list(hooks) if hook_files is None else hook_files
    if targets is None:
        targets = [
            {"id": "cli", "roles": ["cli"], "rule_limit_bytes": 0, "verified": True},
            {"id": "pub", "roles": ["cli", "bot"], "rule_limit_bytes": 0, "verified": True,
             "visibility": "public"},
        ]
    (root / "AGENTS.md").write_text(rules, encoding="utf-8")
    for rel in doc_files:
        p = root / "docs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("doc\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "hooks").mkdir(exist_ok=True)
    for rel in hook_files:
        (root / "hooks" / rel).write_text("x\n", encoding="utf-8")
    lines = [
        "schema = 1",
        'roles = ["cli", "bot"]',
        'visibility = ["public", "private"]',
        "[sources]",
        f'rules = "{root / "AGENTS.md"}"',
        f'docs = "{root / "docs"}"',
        f'hooks = "{root / "hooks"}"',
    ]
    for t in targets:
        lines.append("[[targets]]")
        for k, v in t.items():
            lines.append(f"{k} = {json.dumps(v)}")
    for name, (roles, vis) in sections.items():
        lines += [f'[rule_sections."{name}"]', f"roles = {roles}", f'visibility = "{vis}"']
    lines.append("[docs]")
    lines += [f'"{k}" = "{v}"' for k, v in docs.items()]
    lines.append("[hooks]")
    lines += [f'"{k}" = "{v}"' for k, v in hooks.items()]
    path = root / "habits.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class ParseTests(unittest.TestCase):
    def test_sections_ignore_fenced_heading(self):
        pre, secs = habits.parse_sections(RULES)
        self.assertEqual([h for h, _ in secs], ["Alpha", "Beta", "Gamma"])
        self.assertTrue(pre.startswith("# Title"))
        self.assertIn("## Not a heading", secs[0][1])
        self.assertTrue(secs[0][1].startswith("## Alpha\n"))
        self.assertEqual(pre + "".join(t for _, t in secs), RULES)

    def test_fence_closes_only_with_same_char_and_length(self):
        text = "## Public\n~~~~\n~~~\n~~~~\n## Hidden\nsecret\n"
        _, secs = habits.parse_sections(text)
        self.assertEqual([h for h, _ in secs], ["Public", "Hidden"])
        text2 = "## A\n```\n~~~\n## Inside\n```\n## B\n"
        self.assertEqual([h for h, _ in habits.parse_sections(text2)[1]], ["A", "B"])

    def test_bom_and_crlf(self):
        text = "\ufeff## Hidden\r\nsecret\r\n## Public\r\nok\r\n"
        pre, secs = habits.parse_sections(text)
        self.assertEqual(pre, "")
        self.assertEqual([h for h, _ in secs], ["Hidden", "Public"])
        self.assertNotIn("\r", secs[0][1])


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = build_fixture(Path(self.tmp.name))
        self.m = habits.load_manifest(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_role_filter(self):
        out = habits.select_sections(self.m, RULES, "cli")
        self.assertIn("Intro line.", out)
        self.assertIn("alpha body", out)
        self.assertIn("beta body", out)
        self.assertNotIn("gamma body", out)
        self.assertLess(out.index("alpha body"), out.index("beta body"))

    def test_public_visibility_filter(self):
        out = habits.select_sections(self.m, RULES, "pub")
        self.assertIn("alpha body", out)
        self.assertIn("gamma body", out)
        self.assertNotIn("beta body", out)

    def test_relative_and_tilde_sources(self):
        root = Path(self.tmp.name)
        p = root / "rel.toml"
        p.write_text('schema = 1\n[sources]\nrules = "x/AGENTS.md"\ndocs = "~/d"\n', encoding="utf-8")
        m = habits.load_manifest(p)
        self.assertEqual(m["sources"]["rules"], str(root.resolve() / "x/AGENTS.md"))
        self.assertFalse(m["sources"]["docs"].startswith("~"))


class SuspectHeadingTests(unittest.TestCase):
    """suspect_headings finds level<=2 headings that parse_sections would fold."""

    def kinds(self, text):
        return habits.suspect_headings(text)

    def test_indented_atx(self):
        self.assertEqual(self.kinds("## A\nx\n   ## B\ny\n"), [(3, "indented-atx")])
        self.assertEqual(self.kinds("## A\n  # B\n"), [(2, "indented-atx")])

    def test_tab_after_hashes(self):
        self.assertEqual(self.kinds("## A\n##\tB\n"), [(2, "atx-tab")])

    def test_bare_h2(self):
        self.assertEqual(self.kinds("## A\n##\n"), [(2, "empty-h2")])

    def test_h1_after_sections(self):
        self.assertEqual(self.kinds("# Title\n\n## A\nx\n# Late\n"), [(5, "h1-after-sections")])

    def test_unclosed_front_matter_scanned_normally(self):
        self.assertIn((3, "indented-atx"), self.kinds("---\n## A\n   ## B\n"))

    def test_setext_h1_in_preamble_allowed(self):
        self.assertEqual(self.kinds("Title\n===\n\n## A\nx\n"), [])
        self.assertEqual(self.kinds("## A\nTitle\n===\n"), [(3, "setext")])

    def test_unclosed_fence_line(self):
        self.assertEqual(habits.unclosed_fence("## Public\n```\n## Private\nsecret\n"), 2)
        self.assertIsNone(habits.unclosed_fence(RULES))

    def test_h1_in_preamble_allowed(self):
        self.assertEqual(self.kinds("# Global Rules\n\nintro\n\n## A\nx\n"), [])

    def test_setext_dash_and_equals(self):
        self.assertEqual(self.kinds("## A\nHidden\n---\nsecret\n"), [(3, "setext")])
        self.assertEqual(self.kinds("## A\nHidden\n===\n"), [(3, "setext")])

    def test_bom_crlf_line_numbers(self):
        self.assertEqual(self.kinds("\ufeff## A\r\n   ## B\r\n"), [(2, "indented-atx")])

    def test_negatives(self):
        cases = {
            "front matter": "---\ntitle: x\nfoo\n---\n## A\nx\n",
            "blank before rule": "## A\npara\n\n---\n",
            "table": "## A\n| a | b |\n|---|---|\n",
            "table row then rule": "## A\n| a | b |\n---\n",
            "list": "## A\n- item\n---\n1. one\n===\n",
            "quote": "## A\n> quoted\n---\n",
            "indented code": "## A\n    code\n---\n    ## not heading\n",
            "fenced": "## A\n```\n# h1\n  ## h2\ntext\n---\n```\n",
            "after heading": "## A\n---\n",
            "h3": "## A\n### Sub\n   ### Sub2\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(self.kinds(text), [])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, **kw):
        path = build_fixture(self.root, **kw)
        return habits_check.run_checks(path)

    def levels(self, rep, level):
        return [r for r in rep.results if r["level"] == level]

    def assert_has(self, rep, level, needle):
        hits = [r for r in self.levels(rep, level) if needle in r["message"]]
        self.assertTrue(hits, f"no {level} containing {needle!r}: {rep.results}")
        self.assertTrue(all(r["fix"] for r in hits))

    def test_clean_fixture_passes(self):
        rep = self.run_check()
        self.assertTrue(rep.ok)
        self.assertEqual(self.levels(rep, "WARN"), [])
        self.assertEqual(self.levels(rep, "FAIL"), [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = habits_check.main([str(self.root / "habits.toml"), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])

    def test_unregistered_heading_fails(self):
        rep = self.run_check(rules=RULES + "\n## Delta\n\nnew\n")
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "Delta")

    def test_stale_section_fails(self):
        sections = dict(SECTIONS, Omega=('["cli"]', "public"))
        rep = self.run_check(sections=sections)
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "Omega")

    def test_unregistered_doc_fails(self):
        rep = self.run_check(doc_files=["a.md", "sub/b.md", "sub/c.md"])
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "sub/c.md")

    def test_missing_hook_fails(self):
        rep = self.run_check(hook_files=[])
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "guard.py")

    def test_over_limit_fails(self):
        targets = [{"id": "tiny", "roles": ["cli"], "rule_limit_bytes": 10, "verified": True}]
        rep = self.run_check(targets=targets)
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "tiny")

    def test_generated_limit_counts_generated_note(self):
        build_fixture(self.root)
        m = habits.load_manifest(self.root / "habits.toml")
        bare = len(habits.select_sections(m, RULES, "cli").encode("utf-8"))
        targets = [{"id": "gen", "roles": ["cli"], "rule_limit_bytes": bare + 5, "verified": True,
                    "render": "generated", "output": str(self.root / "out" / "AGENTS.md")}]
        rep = self.run_check(targets=targets)
        self.assertFalse(rep.ok)  # sections alone fit; the generated note pushes it over
        self.assert_has(rep, "FAIL", "gen")
        import render
        m = habits.load_manifest(self.root / "habits.toml")
        with self.assertRaises(render.RenderError):  # render agrees with check
            render.render_rules(m, RULES, "gen")

    def test_near_limit_warns(self):
        build_fixture(self.root)
        m = habits.load_manifest(self.root / "habits.toml")
        size = len(habits.select_sections(m, RULES, "cli").encode("utf-8"))
        targets = [{"id": "cli", "roles": ["cli"], "rule_limit_bytes": int(size / 0.9),
                    "verified": True}]
        rep = self.run_check(targets=targets)
        self.assertTrue(rep.ok)
        self.assert_has(rep, "WARN", "80%")

    def test_fence_trick_cannot_hide_section(self):
        rules = "## Public\n~~~~\n~~~\n~~~~\n## Hidden\nsecret\n"
        targets = [{"id": "pub", "roles": ["cli"], "rule_limit_bytes": 0, "verified": True,
                    "visibility": "public"}]
        rep = self.run_check(rules=rules, sections={"Public": ('["cli"]', "public")},
                             targets=targets)
        self.assert_has(rep, "FAIL", "Hidden")
        m = habits.load_manifest(self.root / "habits.toml")
        self.assertNotIn("secret", habits.select_sections(m, rules, "pub"))

    def test_bom_heading_detected(self):
        rules = "\ufeff## Hidden\nsecret\n## Public\nok\n"
        rep = self.run_check(rules=rules, sections={"Public": ('["cli"]', "public")})
        self.assert_has(rep, "FAIL", "Hidden")

    def test_symlinked_doc_dir_walked(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "hidden.md").write_text("x\n", encoding="utf-8")
        (outside / "loop").symlink_to(outside, target_is_directory=True)
        path = build_fixture(self.root)
        (self.root / "docs" / "linked").symlink_to(outside, target_is_directory=True)
        rep = habits_check.run_checks(path)
        self.assert_has(rep, "FAIL", "linked/hidden.md")

    def test_unreadable_doc_dir_fails(self):
        path = build_fixture(self.root)
        real_scandir = os.scandir
        docs_root = os.path.realpath(self.root / "docs")

        def failing_scandir(target="."):
            if os.path.realpath(os.fspath(target)) == docs_root:
                raise PermissionError(13, "Permission denied", os.fspath(target))
            return real_scandir(target)

        with mock.patch("os.scandir", failing_scandir):
            rep = habits_check.run_checks(path)
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "无法读取 docs 下的目录")

    def test_symlink_into_unsearchable_dir_fails(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = self.root / "outside" / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner" / "secret.md").write_text("x\n", encoding="utf-8")
        path = build_fixture(self.root)
        (self.root / "docs" / "link").symlink_to(locked / "inner", target_is_directory=True)
        locked.chmod(0)
        try:
            rep = habits_check.run_checks(path)
        finally:
            locked.chmod(0o755)
        self.assertFalse(rep.ok)
        self.assert_has(rep, "FAIL", "无法读取 docs 下的目录")

    def json_run(self, path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = habits_check.main([str(path), "--json"])
        return code, json.loads(buf.getvalue())

    def test_wrong_type_limit_fails_with_json(self):
        targets = [{"id": "t", "roles": ["cli"], "rule_limit_bytes": "20", "verified": True}]
        path = build_fixture(self.root, targets=targets)
        code, out = self.json_run(path)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(any("rule_limit_bytes" in r["message"] for r in out["results"]))

    def test_undecodable_rules_fails_with_json(self):
        path = build_fixture(self.root)
        (self.root / "AGENTS.md").write_bytes(b"## Alpha\n\xff\n")
        code, out = self.json_run(path)
        self.assertEqual(code, 1)
        self.assertTrue(any(r["level"] == "FAIL" and r["check"] == "rule_sections"
                            for r in out["results"]))

    def test_negative_limit_fails(self):
        targets = [{"id": "neg", "roles": ["cli"], "rule_limit_bytes": -1, "verified": True}]
        rep = self.run_check(targets=targets)
        self.assert_has(rep, "FAIL", "负数")

    def test_missing_verified_warns(self):
        targets = [{"id": "nov", "roles": ["cli"], "rule_limit_bytes": 0}]
        rep = self.run_check(targets=targets)
        self.assertTrue(rep.ok)
        self.assert_has(rep, "WARN", "缺少 verified")

    def test_string_verified_fails(self):
        targets = [{"id": "sv", "roles": ["cli"], "rule_limit_bytes": 0, "verified": "false"}]
        rep = self.run_check(targets=targets)
        self.assert_has(rep, "FAIL", "verified")

    def test_bad_roles_type_fails(self):
        targets = [{"id": "br", "roles": "cli", "rule_limit_bytes": 0, "verified": True}]
        rep = self.run_check(targets=targets)
        self.assert_has(rep, "FAIL", "roles")

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "no system python")
    def test_old_python_exits_2(self):
        ver = subprocess.run(["/usr/bin/python3", "-c", "import sys;print(sys.version_info>=(3,11))"],
                             capture_output=True, text=True).stdout.strip()
        if ver != "False":
            self.skipTest("system python is 3.11+")
        res = subprocess.run(["/usr/bin/python3", str(ENGINE / "habits_check.py"), "x.toml"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("3.11+", res.stderr)
        self.assertEqual(res.stderr.strip().count("\n"), 0)

    def rule_fails_at(self, rep, lineno):
        return [r for r in rep.results if r["level"] == "FAIL" and r["check"] == "rule_sections"
                and f"第 {lineno} 行" in r["message"]]

    def test_suspect_heading_fails_with_line(self):
        rules = RULES + "\n   ## Hidden\nsecret\n"
        rep = self.run_check(rules=rules)
        lineno = rules.splitlines().index("   ## Hidden") + 1
        self.assertFalse(rep.ok)
        self.assertEqual(len(self.rule_fails_at(rep, lineno)), 1)

    def test_setext_fails_with_line(self):
        rules = RULES + "Hidden\n---\n"
        rep = self.run_check(rules=rules)
        lineno = len(rules.splitlines())
        self.assertEqual(len(self.rule_fails_at(rep, lineno)), 1)

    def test_negatives_do_not_fail(self):
        rules = RULES + "\n| a |\n|---|\n\n- x\n---\n\n> q\n---\n\n    c\n---\n"
        rep = self.run_check(rules=rules)
        self.assertTrue(rep.ok, [r for r in rep.results if r["level"] == "FAIL"])

    def test_duplicate_heading_fails(self):
        rep = self.run_check(rules=RULES + "\n## Alpha \n\nagain\n")
        dup = [r for r in rep.results if r["level"] == "FAIL" and r["check"] == "rule_sections"
               and "Alpha" in r["message"] and "2" in r["message"]]
        self.assertEqual(len(dup), 1)

    def test_h1_in_preamble_passes(self):
        rep = self.run_check()
        self.assertTrue(RULES.startswith("# Title"))
        self.assertTrue(rep.ok)

    def fails(self, rep, check="rule_sections", needle=""):
        return [r for r in rep.results if r["level"] == "FAIL" and r["check"] == check
                and needle in r["message"]]

    def test_unclosed_fence_fails_and_select_fails_closed(self):
        rules = "## Public\nok\n## Mid\nmid\n```\n## Private\nsecret\n"
        targets = [{"id": "pub", "roles": ["cli"], "rule_limit_bytes": 0, "verified": True,
                    "visibility": "public"}]
        sections = {"Public": ('["cli"]', "public"), "Mid": ('["cli"]', "public")}
        rep = self.run_check(rules=rules, sections=sections, targets=targets)
        self.assertEqual(len(self.fails(rep, needle="第 5 行")), 1)
        m = habits.load_manifest(self.root / "habits.toml")
        out = habits.select_sections(m, rules, "pub")
        self.assertEqual(out, "## Public\nok\n")

    def test_unclosed_fence_in_preamble_outputs_nothing(self):
        build_fixture(self.root)
        m = habits.load_manifest(self.root / "habits.toml")
        self.assertEqual(habits.select_sections(m, "intro\n```\n## Alpha\nx\n", "cli"), "")

    def test_closed_fences_in_fixture_no_fail(self):
        rep = self.run_check()
        self.assertEqual(self.fails(rep), [])

    def test_registry_path_escaping_root_fails(self):
        rep = self.run_check(docs={"a.md": "public", "sub/b.md": "private",
                                   "../AGENTS.md": "public"},
                             doc_files=["a.md", "sub/b.md"])
        self.assertEqual(len(self.fails(rep, "docs", "../AGENTS.md")), 1)
        rep = self.run_check(hooks={"guard.py": "public", "/etc/hosts": "public"},
                             hook_files=["guard.py"])
        self.assertEqual(len(self.fails(rep, "hooks", "/etc/hosts")), 1)

    def test_unregistered_preamble_fails(self):
        sections = {k: v for k, v in SECTIONS.items() if k != "__preamble__"}
        rep = self.run_check(sections=sections)
        self.assertEqual(len(self.fails(rep, needle="说明区")), 1)
        m = habits.load_manifest(self.root / "habits.toml")
        self.assertNotIn("Intro line.", habits.select_sections(m, RULES, "cli"))

    def test_registered_preamble_with_empty_preamble_ok(self):
        rules = "## Alpha\na\n## Beta\nb\n## Gamma\ng\n"
        rep = self.run_check(rules=rules)
        self.assertTrue(rep.ok, self.fails(rep))

    def test_preamble_bad_values_fail(self):
        sections = dict(SECTIONS, __preamble__=('["nope"]', "public"))
        rep = self.run_check(sections=sections)
        self.assertEqual(len(self.fails(rep, needle="说明区")), 1)

    def test_preamble_follows_target_filters(self):
        sections = dict(SECTIONS, __preamble__=('["bot"]', "private"))
        build_fixture(self.root, sections=sections)
        m = habits.load_manifest(self.root / "habits.toml")
        self.assertNotIn("Intro line.", habits.select_sections(m, RULES, "cli"))
        self.assertNotIn("Intro line.", habits.select_sections(m, RULES, "pub"))

    def test_unverified_target_warns(self):
        targets = [{"id": "new-host", "roles": ["cli"], "rule_limit_bytes": 0, "verified": False}]
        rep = self.run_check(targets=targets)
        self.assertTrue(rep.ok)
        self.assert_has(rep, "WARN", "未核实")


if __name__ == "__main__":
    unittest.main()
