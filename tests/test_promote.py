import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import promote  # noqa: E402

# Token-shaped test values are assembled at runtime so privacy scanners do not see literals.
FAKE_TOKEN = "gh" + "p_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


class PromoteTests(unittest.TestCase):
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
        # memory: one file per memory, §-separated entries, bullets under a heading
        mem = self.home / ".claude" / "projects" / "p1" / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- index\n")
        (mem / "pref.md").write_text("---\nname: pref\ndescription: 先给结论\n---\n回复先写结论。\n")
        (mem / "leak.md").write_text(f"---\nname: leak\ndescription: gh\n---\nGITHUB_TOKEN={FAKE_TOKEN}\n")
        bot = self.home / ".bot" / "memories"
        bot.mkdir(parents=True)
        (bot / "MEMORY.md").write_text("第一条，提到 §19 也不拆\n§\n第二条\n")
        cx = self.home / ".cx"
        cx.mkdir()
        (cx / "MEMORY.md").write_text("# Task\n## User preferences\n- 只读审查要带 file:line\n  续行\n"
                                      "## Reusable knowledge\n- 不要这条\n")
        # settings: committed allow list, then one runtime addition (and one symlinked account dir)
        self.cfg = self.home / ".claude"
        git(self.cfg, "init", "-q", "-b", "main")
        (self.cfg / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git status)"]}}))
        git(self.cfg, "add", "settings.json")
        git(self.cfg, "commit", "-q", "-m", "init")
        (self.cfg / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(git status)", "Bash(npm test)"]}}))
        acct = self.home / ".claude-001"
        acct.mkdir()
        (acct / "settings.json").symlink_to(self.cfg / "settings.json")
        # codex rules file without baseline
        rules = self.home / ".codex" / "rules"
        rules.mkdir(parents=True)
        (rules / "default.rules").write_text('# comment\nprefix_rule(\n    pattern=["git", "add"],\n'
                                             '    decision="allow",\n)\n\nprefix_rule(pattern=["npm", "publish"], '
                                             'decision="prompt")\n')
        # skills: a symlink to shared, a bundled one, a category dir, a host-made one
        shared = self.home / ".agents" / "skills"
        (shared / "shared-one").mkdir(parents=True)
        (shared / "shared-one" / "SKILL.md").write_text("---\nname: shared-one\n---\n")
        sk = self.home / ".bot" / "skills"
        sk.mkdir(parents=True)
        (sk / "shared-one").symlink_to(shared / "shared-one")
        for d in ("bundled", "mine", "cat/nested"):
            (sk / d).mkdir(parents=True)
            (sk / d / "SKILL.md").write_text(f"---\nname: {d}\ndescription: does {d}\n---\n")
        (sk / ".bundled_manifest").write_text("bundled:abc\n")
        self.manifest = {"promotion": {
            "memory": [
                {"glob": "~/.claude/projects/*/memory/*.md", "split": "file", "exclude": ["MEMORY.md"]},
                {"glob": "~/.bot/memories/MEMORY.md", "split": "section-sign"},
                {"glob": "~/.cx/MEMORY.md", "split": "bullets", "sections": ["User preferences"]},
            ],
            "settings_allow": ["~/.claude/settings.json", "~/.claude-*/settings.json"],
            "rules_files": [{"path": "~/.codex/rules/default.rules"}],
            "skill_dirs": [{"path": "~/.bot/skills", "installed": [".bundled_manifest"]}],
            "shared_skills": "~/.agents/skills",
        }}

    def scan(self):
        return promote.scan(self.manifest)

    def texts(self, kind):
        return [c["text"] for c in self.scan()["candidates"] if c["kind"] == kind]

    def test_secret_never_printed(self):
        result = self.scan()
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(FAKE_TOKEN, dumped)
        self.assertNotIn(FAKE_TOKEN[:12], dumped)
        leak = [c for c in result["candidates"] if "leak.md" in c["source"]]
        self.assertEqual(len(leak), 1)
        self.assertEqual(leak[0]["text"], promote.SKIPPED_SECRET)
        self.assertEqual(result["secret_skipped"], 1)
        path = promote.propose(self.manifest)
        self.assertNotIn(FAKE_TOKEN, path.read_text(encoding="utf-8"))

    def test_allow_reports_only_new_entries_once(self):
        self.assertEqual(self.texts("permission_allow"), ["Bash(npm test)"])

    def test_memory_splitting(self):
        mem = self.texts("memory")
        self.assertIn("pref — 先给结论：回复先写结论。", mem)
        self.assertIn("第一条，提到 §19 也不拆", mem)
        self.assertIn("第二条", mem)
        self.assertIn("只读审查要带 file:line 续行", mem)
        self.assertNotIn("不要这条", " ".join(mem))
        self.assertFalse(any("index" == m for m in mem))

    def test_rules_blocks(self):
        self.assertEqual(len(self.texts("codex_rule")), 2)

    def test_host_made_skills_only(self):
        names = [t.split("（", 1)[0] for t in self.texts("skill")]
        self.assertEqual(sorted(names), ["mine", "nested"])

    def test_deterministic_and_read_only(self):
        before = sorted(str(p) for p in self.root.rglob("*") if ".git" not in p.parts)
        self.assertEqual(json.dumps(self.scan()), json.dumps(self.scan()))
        after = sorted(str(p) for p in self.root.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after)

    def test_propose_writes_draft_under_state(self):
        path = promote.propose(self.manifest)
        self.assertTrue(str(path).startswith(str(self.root / "state" / "agent-env" / "promotions")))
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 提议", text)
        self.assertIn("Bash(npm test)", text)


class SecretPatternTests(unittest.TestCase):
    def test_detects(self):
        for s in ("api_key=" + "abcdef123456", "Authorization: Bearer abcdefghijklmnop", "s" + "k-abcdefghijklmnopqrstu",
                  "xo" + "xb-1234567890-abc", "password: hunter22", "AK" + "IAABCDEFGHIJKLMNOP"):
            self.assertTrue(promote.looks_secret(s), s)

    def test_ignores_plain_text(self):
        for s in ("勿在聊天粘贴 Token/Cookie/密码", "feedback_migration_align_website_codex_loop",
                  "rollout-2026-11-08T23-57-10-01a1bbe0-bcce-7040-8ec2-b5515ba46d12.jsonl"):
            self.assertFalse(promote.looks_secret(s), s)


if __name__ == "__main__":
    unittest.main()
