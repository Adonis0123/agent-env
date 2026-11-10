"""Company skill sets: hash expectations, install dry run / --yes with backup, rollback, secret refusal.
A local shell stands in for the remote host, as in test_remote."""
import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
import company_skills  # noqa: E402
import habits_check  # noqa: E402
from test_agent_env import Base  # noqa: E402

SEND = ["sh", "-c", 'mkdir -p "$2" && cp "$1" "$2/$3"', "send"]


class CompanyBase(Base):
    def setUp(self):
        super().setUp()
        self.src = self.root / "company-skills"
        self.skill("alpha", {"SKILL.md": "---\nname: alpha\n---\nalpha v1\n", "scripts/run.py": "print(1)\n"})
        self.skill("group/beta", {"SKILL.md": "---\nname: beta\n---\nbeta v1\n"})
        self.dest = self.home / ".hermes" / "skills"
        self.dest.mkdir(parents=True)
        self.backups = self.home / ".hermes" / ".agent-env-backups" / "skills"

    def skill(self, rel, files, base=None):
        for name, text in files.items():
            p = (base or self.src) / rel / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    def setup(self, remote=False, rels=("alpha", "group/beta")):
        t = self.target([{"kind": "company-skills", "habit": "skills"}], id="test/host",
                        company_skills=list(rels), company_skills_dir="~/.hermes/skills")
        envs = None
        if remote:
            self.rhome = self.root / "remote-home"
            self.dest = self.rhome / ".hermes" / "skills"
            self.dest.mkdir(parents=True)
            self.backups = self.rhome / ".hermes" / ".agent-env-backups" / "skills"
            t.update(env="box", id="box/bot", roles=["bot"])
            envs = {"box": {"transport": "remote-exec", "exec": ["sh"], "send": SEND, "remote_home": str(self.rhome),
                            "timeout": 30, "chunk_bytes": 200},
                    "test": {"platform": "nomatch"}}
        self.write_manifest([t], envs)
        text = self.manifest.read_text(encoding="utf-8").replace(
            "[sources]\n", f'[sources]\ncompany_skills = "{self.src}"\n', 1)
        self.manifest.write_text(text, encoding="utf-8")
        self.tid = t["id"]

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = agent_env.main(["skills", *argv, "--manifest", str(self.manifest), "--target", self.tid], out, err)
        return rc, out.getvalue() + err.getvalue()

    def states(self, env="test"):
        st = agent_env.doctor(self.manifest, env, self.repo)
        return {c["name"]: c for c in self.cells(st, self.tid)}


class HashTests(CompanyBase):
    def test_sh_hash_equals_python_hash(self):
        (self.src / "alpha" / ".venv").mkdir()
        (self.src / "alpha" / ".venv" / "x").write_text("ignored", encoding="utf-8")
        rc, out, _ = company_skills._run_local(company_skills.SH_LIB + f'_cs_hash "{self.src / "alpha"}"\n')
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), company_skills.tree_hash(self.src / "alpha"))

    def test_missing_then_synced_then_drift(self):
        self.setup()
        cells = self.states()
        self.assertEqual(cells["alpha"]["state"], "missing")
        self.assertIn("skills install --target test/host", cells["alpha"]["fix"])
        self.assertEqual(self.run_cli("install", "--yes")[0], 0)
        cells = self.states()
        self.assertEqual({c["state"] for c in cells.values()}, {"synced"})
        (self.dest / "alpha" / "SKILL.md").write_text("edited on the machine\n", encoding="utf-8")
        cells = self.states()
        self.assertEqual(cells["alpha"]["state"], "drift")
        self.assertEqual(cells["group/beta"]["state"], "synced")

    def test_runtime_dirs_do_not_drift(self):
        self.setup()
        self.run_cli("install", "--yes")
        venv = self.dest / "alpha" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.dest / "alpha" / "scripts" / "__pycache__").mkdir()
        self.assertEqual(self.states()["alpha"]["state"], "synced")

    def test_same_name_elsewhere_is_drift(self):
        self.setup()
        self.run_cli("install", "--yes")
        self.skill("other/alpha", {"SKILL.md": "dup\n"}, base=self.dest)
        cell = self.states()["alpha"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("other/alpha", cell["detail"])


class InstallTests(CompanyBase):
    def test_dry_run_lists_and_changes_nothing(self):
        self.setup()
        self.skill("alpha", {"SKILL.md": "old\n"}, base=self.dest)
        rc, out = self.run_cli("install")
        self.assertEqual(rc, 0)
        self.assertIn("新增 1：group/beta", out)
        self.assertIn("替换 1：alpha", out)
        self.assertIn("未执行（演练）", out)
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "old\n")
        self.assertFalse(self.backups.exists())

    def test_yes_backs_up_replaced_keeps_others_and_venv(self):
        self.setup()
        self.skill("alpha", {"SKILL.md": "old\n", "stale.md": "gone after install\n"}, base=self.dest)
        (self.dest / "alpha" / ".venv").mkdir()
        (self.dest / "alpha" / ".venv" / "keep").write_text("venv", encoding="utf-8")
        self.skill("unrelated", {"SKILL.md": "mine\n"}, base=self.dest)
        rc, out = self.run_cli("install", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(company_skills.tree_hash(self.dest / "alpha"), company_skills.tree_hash(self.src / "alpha"))
        self.assertFalse((self.dest / "alpha" / "stale.md").exists())
        self.assertTrue((self.dest / "alpha" / ".venv" / "keep").is_file())
        self.assertEqual((self.dest / "unrelated" / "SKILL.md").read_text(encoding="utf-8"), "mine\n")
        [stamp] = list(self.backups.iterdir())
        self.assertEqual((stamp / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(sorted((stamp / "backup.list").read_text(encoding="utf-8").split()),
                         sorted(["present", "alpha", "absent", "group/beta"]))
        self.assertFalse((stamp / "alpha" / ".venv").exists())  # runtime folders are not copied
        self.assertFalse(str(self.backups).startswith(str(self.dest)))  # Hermes would load backups as skills

    def test_rollback_restores_and_second_rollback_undoes(self):
        self.setup()
        self.skill("alpha", {"SKILL.md": "old\n"}, base=self.dest)
        self.run_cli("install", "--yes")
        installed = company_skills.tree_hash(self.dest / "alpha")
        rc, out = self.run_cli("rollback")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "old\n")
        self.assertFalse((self.dest / "group" / "beta").exists())  # it was added by the install
        rc, out = self.run_cli("rollback")
        self.assertEqual(rc, 0, out)
        self.assertEqual(company_skills.tree_hash(self.dest / "alpha"), installed)
        self.assertTrue((self.dest / "group" / "beta" / "SKILL.md").is_file())

    def test_rollback_without_backup_fails_with_fix(self):
        self.setup()
        rc, out = self.run_cli("rollback")
        self.assertEqual(rc, 1)
        self.assertIn("没有备份", out)

    def test_symlink_target_is_not_overwritten(self):
        self.setup()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.dest / "alpha").symlink_to(elsewhere)
        rc, out = self.run_cli("install", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("符号链接", out)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_remote_install_in_chunks_and_rollback(self):
        self.setup(remote=True)
        self.skill("alpha", {"SKILL.md": "old\n"}, base=self.dest)
        rc, out = self.run_cli("install", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("part.0001", out)  # 200-byte chunks
        self.assertEqual(company_skills.tree_hash(self.dest / "alpha"), company_skills.tree_hash(self.src / "alpha"))
        self.assertFalse(any((self.rhome / ".cache" / "agent-env" / "incoming").iterdir()))  # staging removed
        cells = self.states("box")
        self.assertEqual({c["state"] for c in cells.values()}, {"synced"})
        self.assertEqual(self.run_cli("rollback")[0], 0)
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "old\n")


class SecretTests(CompanyBase):
    FAKE = "LTAI" + "5tFakeFakeFake12345"  # split so this file itself does not look like a key

    def test_secret_in_source_is_refused_by_install_and_check(self):
        self.setup()
        self.skill("alpha", {"notes.md": f"key: {self.FAKE}\n"})
        rc, out = self.run_cli("install", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("疑似密钥", out)
        self.assertNotIn(self.FAKE, out)
        self.assertFalse((self.dest / "alpha").exists())
        rep = habits_check.run_checks(self.manifest)
        self.assertTrue(any(r["level"] == "FAIL" and "疑似密钥" in r["message"] for r in rep.results))

    def test_secret_assignment_pattern(self):
        hits = []
        d = self.root / "scan"
        self.skill("x", {"a.py": 'access_key_secret = "' + "Q" * 24 + '"\n',
                         "b.md": "secret: read from ~/.tool-cache/ak.json\n"}, base=d)
        hits = company_skills.scan_secrets(d / "x")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].startswith("a.py:1"))

    def test_demo_source_has_no_secrets(self):
        demo = Path(__file__).resolve().parent.parent / "examples" / "demo" / "sources" / "company-skills"
        self.assertTrue(demo.is_dir())
        self.assertEqual(company_skills.scan_secrets(demo), [])


class CheckTests(CompanyBase):
    def test_check_flags_missing_source_skill_and_bad_dir(self):
        self.setup(rels=("alpha", "nope"))
        rep = habits_check.run_checks(self.manifest)
        fails = [r["message"] for r in rep.results if r["level"] == "FAIL"]
        self.assertTrue(any("nope" in m and "SKILL.md" in m for m in fails), fails)


if __name__ == "__main__":
    unittest.main()
