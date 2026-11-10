import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import agent_env  # noqa: E402
from test_agent_env import RULES, Base  # noqa: E402

EXP = {"kind": "delivered", "habit": "rules", "name": "cloud rules"}


class DeliverFlowTests(Base):
    def setUp(self):
        super().setUp()
        skills = self.root / "skills"
        (skills / "good-skill").mkdir(parents=True)
        (skills / "good-skill" / "SKILL.md").write_text(
            "---\nname: good-skill\ndescription: >\n  Does a good\n  thing.\n---\nbody\n", encoding="utf-8")
        m = self.write_manifest([self.target([EXP], id="test/bot", env="cloud", roles=["remote"],
                                             skills=["good-skill"])])
        m.write_text(m.read_text().replace("[sources]\n", f'[sources]\nskills = "{skills}"\n', 1))

    def state(self):
        [c] = self.cells(self.doctor(), "test/bot")
        return c["state"]

    def deliver(self, record=False):
        out, err = io.StringIO(), io.StringIO()
        rc = agent_env.deliver(self.manifest, "test/bot", record=record, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def outbox(self):
        return self.root / "state" / "agent-env" / "outbox" / "test-bot.md"

    def test_missing_then_synced_then_drift(self):
        self.assertEqual(self.state(), "missing")
        rc, text, note = self.deliver()
        self.assertEqual(rc, 0)
        self.assertEqual(self.outbox().read_text(encoding="utf-8"), text)
        self.assertIn("outbox", note)
        self.assertEqual(self.state(), "missing")  # printing is not delivering
        self.assertEqual(self.deliver(record=True)[0], 0)
        self.assertEqual(self.state(), "synced")
        rec = json.loads((self.root / "state" / "agent-env" / "deliveries" / "test" / "bot.json").read_text())
        self.assertEqual(rec["bytes"], len(text.encode("utf-8")))
        (self.root / "AGENTS.md").write_text(RULES.replace("shared body", "shared body v2"), encoding="utf-8")
        self.assertEqual(self.state(), "drift")

    def test_text_has_role_rules_and_selected_skills(self):
        _, text, _ = self.deliver()
        self.assertIn("shared body", text)
        self.assertNotIn("cli body", text)
        self.assertIn("## 选中的 skill", text)
        self.assertIn("- `good-skill`：Does a good thing.", text)

    def test_skill_description_change_needs_redelivery(self):
        self.deliver()
        self.deliver(record=True)
        md = self.root / "skills" / "good-skill" / "SKILL.md"
        md.write_text(md.read_text().replace("good\n  thing", "better\n  thing"), encoding="utf-8")
        self.assertEqual(self.state(), "drift")

    def test_record_refuses_stale_outbox(self):
        self.deliver()
        (self.root / "AGENTS.md").write_text(RULES.replace("shared body", "shared body v2"), encoding="utf-8")
        err = io.StringIO()
        rc = agent_env.main(["deliver", "--manifest", str(self.manifest), "--target", "test/bot", "--record"],
                            out=io.StringIO(), err=err)
        self.assertEqual(rc, 1)
        self.assertIn("outbox", err.getvalue())
        self.assertEqual(self.state(), "missing")  # nothing was recorded


class PublicDeliverTests(Base):
    """Delivered text leaves this machine: it must not name local paths or the engine."""

    def test_public_delivered_text_has_no_local_paths(self):
        skills = self.home / ".agents" / "skills"  # under $HOME, so a path would show as ~/...
        (skills / "good-skill").mkdir(parents=True)
        (skills / "good-skill" / "SKILL.md").write_text(
            "---\nname: good-skill\ndescription: Does a good thing.\n---\nbody\n", encoding="utf-8")
        m = self.write_manifest([self.target([EXP], id="test/bot", env="cloud", roles=["remote"],
                                             visibility="public", skills=["good-skill"])])
        m.write_text(m.read_text().replace("[sources]\n", f'[sources]\nskills = "{skills}"\n', 1))
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(agent_env.deliver(self.manifest, "test/bot", out=out, err=err), 0)
        text = out.getvalue()
        self.assertIn("- `good-skill`：Does a good thing.", text)
        self.assertIn("bin/agent-env deliver --target test/bot", text)
        for leak in ("~/", str(self.root), "render.py", self.manifest.name, "AGENTS.md"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()
