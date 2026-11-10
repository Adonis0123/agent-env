"""The expectation-kind registry (engine/expect): every kind is reached through it, and every cell says
which kind produced it."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import expect  # noqa: E402
import habits_check  # noqa: E402
import remote  # noqa: E402
from test_agent_env import Base  # noqa: E402
from test_remote import RemoteBase  # noqa: E402


class RegistryTests(Base):
    def test_every_kind_has_its_pieces(self):
        self.assertEqual(expect.KINDS, ("link", "contains", "generated", "delivered", "login", "service",
                                        "broken-links", "company-skills", "portal", "secrets", "plugins", "token",
                                        "pulled", "composed"))
        for kind in expect.KINDS:
            k = expect.get(kind)
            self.assertEqual(k.KIND, kind)
            self.assertTrue(callable(k.validate) and callable(k.check_local), kind)
            self.assertTrue(k.check_remote is None or callable(k.check_remote), kind)
        self.assertIsNone(expect.get("nope"))
        self.assertIsNone(expect.get(["link"]))  # unhashable kind from a bad manifest

    def test_contains_validate_local_and_remote_through_the_registry(self):
        k = expect.get("contains")
        errs = expect.validate({"kind": "contains", "habit": "rules", "path": "/abs", "text": "a\nb"},
                               {}, "第 1 条期望")
        self.assertEqual([m for m, _ in errs], ["第 1 条期望 的 path='/abs' 不是 ~ 开头的路径",
                                                 "第 1 条期望（contains）的 text 跨行"])
        exp = {"kind": "contains", "habit": "rules", "path": "~/x.env", "text": "KEY="}
        (self.home / "x.env").write_text("KEY=1\n", encoding="utf-8")
        [c] = expect.check({}, {"id": "test/host"}, exp, None)
        self.assertEqual((c["state"], c["expect_kind"]), ("synced", "contains"))
        frag, parse = k.check_remote({}, {"remote_home": str(self.home)}, "box", {"id": "box/x"}, exp, None)
        out = subprocess.run(["sh"], input=remote.PROLOGUE + frag, capture_output=True, text=True).stdout
        c = parse(out.splitlines())
        self.assertEqual((c["state"], c["detail"]), ("synced", "远程 ~/x.env 含 `KEY=`"))

    def test_check_names_the_registered_kinds(self):
        self.write_manifest([self.target([{"kind": "nope", "habit": "rules"}])])
        fails = [r for r in habits_check.run_checks(self.manifest).results if r["level"] == "FAIL"]
        self.assertTrue(any(f"kind 写成 {list(expect.KINDS)} 之一" == r["fix"] for r in fails), fails)

    def test_local_doctor_cells_carry_their_kind(self):
        (self.home / "src").mkdir()
        self.write_manifest([self.target([
            {"kind": "link", "habit": "rules", "path": "~/a", "to": "~/src"},
            {"kind": "contains", "habit": "rules", "path": "~/b", "text": "x"},
            {"kind": "service", "habit": "rules", "unit": "gw"},
            {"kind": "nope", "habit": "rules"}])])
        cells = self.cells(self.doctor())
        self.assertEqual([c.get("expect_kind") for c in cells], ["link", "contains", "service", None])


class RemoteKindTests(RemoteBase):
    def test_remote_doctor_cells_carry_their_kind(self):
        self.setup_env()
        expected = {"login": "login", "AGENTS.md": "generated", "gateway": "service", "env key": "contains"}
        cells = self.by_name(self.doctor("box"))
        self.assertEqual({n: c["expect_kind"] for n, c in cells.items()}, expected)


class PortalNamingTest(unittest.TestCase):
    """portal.part and portal.tool_label read back exactly the names check_local writes."""

    def cells_for(self, exp: dict, missing_registry: bool = False) -> list:
        import json as _json
        import os
        import tempfile
        from unittest import mock
        from expect import portal
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch.object(portal, "HEALTH_TIMEOUT", 0.2):  # the health URL is refused, never answered
            if not missing_registry:
                Path(home, "portal.json").write_text(_json.dumps({"tools": [
                    {"id": "ed", "label": "Editors", "module": "~/ed.ts"}, {"id": "skills"}]}), encoding="utf-8")
            return portal.check_local({}, {}, dict(exp, registry="~/portal.json"), "")

    def test_round_trip(self):
        from expect import portal
        for exp in ({"kind": "portal"}, {"kind": "portal", "name": "本机·门户"}):
            exp = dict(exp, health_url="http://127.0.0.1:9/")
            names = {c["name"]: c for c in self.cells_for(exp)}
            names.update({c["name"]: c for c in self.cells_for(exp, missing_registry=True)})
            base = portal.base_name(exp)
            self.assertEqual({n: portal.part(exp, n) for n in names},
                             {base: "portal-registry", f"{base}·服务": "portal-service", f"{base}·Editors": "portal"})
            self.assertEqual(portal.tool_label(exp, f"{base}·Editors"), "Editors")
            self.assertIsNone(portal.part(exp, "别的门户·Editors"))

    def test_gone_from_manifest_guesses_by_shape(self):
        from expect import portal
        self.assertEqual(portal.part(None, "门户"), "portal-registry")
        self.assertEqual(portal.part(None, "门户·服务"), "portal-service")
        self.assertEqual(portal.part(None, "门户·Editors"), "portal")
        self.assertEqual(portal.tool_label(None, "门户·Editors"), "Editors")
        self.assertEqual(portal.part(None, "本机·门户"), "portal", "known limit: a custom name with SEP needs the manifest")


if __name__ == "__main__":
    unittest.main()
