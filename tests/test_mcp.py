import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import habits  # noqa: E402
import habits_check  # noqa: E402
import mcp  # noqa: E402
from test_habits import build_fixture  # noqa: E402

# Assembled at runtime so privacy scanners do not see a token-shaped literal.
FAKE_TOKEN = "s" + "k-" + "fake-9f8e7d6c5b4a-DO-NOT-LEAK"

MCP_TABLES = """
[mcp.kimi-cu]
transport = "stdio"
command = "kimi-cu"
args = ["mcp", "-s", "user"]
env = []
visibility = "private"

[mcp.figma]
transport = "http"
url = "https://mcp.figma.com/mcp"
env = []
visibility = "public"

[mcp.deepwiki]
transport = "http"
url = "https://mcp.deepwiki.com/mcp"
env = []
visibility = "public"

[mcp.figma-rest]
transport = "stdio"
command = "npx"
args = ["-y", "figma-developer-mcp@0.13.2"]
env = ["FIGMA_API_KEY"]
visibility = "private"

[mcp.acme_local]
transport = "http"
url = "http://localhost:8080/api/mcp"
env = []
auth_env = "ACME_LOCAL_MCP_TOKEN"
visibility = "private"
"""

CLAUDE_JSON = {
    "numStartups": 3,
    "mcpServers": {
        "kimi-cu": {"type": "stdio", "command": "/Apps/kimi-cu", "args": ["mcp"],
                    "env": {"KIMI_TOKEN": FAKE_TOKEN}},
        "deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"},
        "figma": {"type": "http", "url": "https://mcp.figma.com/mcp"},
    },
    "projects": {"/p": {"mcpServers": {"ignored": {"command": "x"}}}},
}

CURSOR_JSON = {"mcpServers": {
    "Figma": {"url": "https://mcp.figma.com/mcp", "headers": {"Authorization": f"Bearer {FAKE_TOKEN}"}},
    "kimi-cu": {"command": "kimi-cu", "args": ["mcp"]},
}}

PI_JSON = {"autoEnableCodemode": True, "mcpServers": {
    "figma-rest": {"command": "npx", "args": ["-y", "figma-developer-mcp@0.13.2"],
                   "env": {"FIGMA_API_KEY": FAKE_TOKEN}, "exposure": "direct"},
    "figma": {"url": "https://mcp.figma.com/mcp", "enabled": False},
}}

CODEX_TOML = f"""
model = "x"
[mcp_servers.kimi-cu]
command = "/Apps/kimi-cu"
args = ["mcp"]

[mcp_servers.node_repl]
command = "node_repl"
[mcp_servers.node_repl.env]
SECRET_THING = "{FAKE_TOKEN}"

[mcp_servers.acme_local]
url = "http://localhost:8080/api/mcp?token={FAKE_TOKEN}"
[mcp_servers.acme_local.http_headers]
Authorization = "Bearer {FAKE_TOKEN}"

[mcp_servers.figma]
url = "https://mcp.figma.com/mcp"
enabled = false
"""

GROK_TOML = """
disabled_mcp_servers = ["chrome-devtools"]
[compat.claude]
hooks = false
[mcp_servers.kimi-cu]
command = "/Apps/kimi-cu"
args = ["mcp"]
enabled = true
[mcp_servers.chrome-devtools]
command = "chrome-devtools-mcp-safe"
"""

HERMES_YAML = f"""# Hermes config
model:
  default: x
mcp_servers:
  # comment inside
  chrome-devtools:
    command: /bin/chrome-devtools-mcp-safe
    args: []
    enabled: false
    connect_timeout: 90
  kimi-cu:
    command: /Apps/kimi-cu
    args:
      - mcp
      - -s
    env:
      KIMI_TOKEN: "{FAKE_TOKEN}"
    enabled: true
skills:
  disabled: []
"""


def target(tid, host, config, servers, **extra):
    t = {"id": tid, "host": host, "env": "mac", "roles": ["cli"], "rule_limit_bytes": 0,
         "verified": True, "mcp_config": config, "mcp": servers}
    t.update(extra)
    return t


class McpBase(unittest.TestCase):
    """Fake host configs under a tempdir HOME; the manifest points at them with ~/ paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.write(".claude.json", json.dumps(CLAUDE_JSON))
        self.write(".cursor/mcp.json", json.dumps(CURSOR_JSON))
        self.write(".pi/agent/mcp.json", json.dumps(PI_JSON))
        self.write(".codex/config.toml", CODEX_TOML)
        self.write(".grok/config.toml", GROK_TOML)
        self.write(".hermes/config.yaml", HERMES_YAML)
        self.targets = [
            target("claude", "claude-code", "~/.claude.json", ["kimi-cu", "deepwiki", "figma"]),
            target("cursor", "cursor", "~/.cursor/mcp.json", ["kimi-cu", "figma"]),
            target("pi", "pi", "~/.pi/agent/mcp.json", ["figma-rest"]),
            target("codex", "codex", "~/.codex/config.toml", ["kimi-cu", "acme_local"],
                   mcp_unmanaged=["node_repl"]),
            target("grok", "grok", "~/.grok/config.toml", ["kimi-cu", "figma", "deepwiki"],
                   mcp_compat_from=["claude-code", "cursor"]),
            target("hermes", "hermes", "~/.hermes/config.yaml", ["kimi-cu"]),
        ]
        self.load()

    def write(self, rel, text):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def load(self, extra=MCP_TABLES):
        self.path = build_fixture(self.root, targets=self.targets)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(extra)
        self.m = habits.load_manifest(self.path)

    def cells(self, tid):
        return {c["name"]: c for c in mcp.compare(self.m, habits.find_target(self.m, tid))}


class ReaderTests(McpBase):
    def names(self, host, rel):
        entries, _ = mcp.READERS[host](self.home / rel)
        return {e["name"]: e for e in entries}

    def test_claude_reads_top_level_only(self):
        got = self.names("claude-code", ".claude.json")
        self.assertEqual(set(got), {"kimi-cu", "deepwiki", "figma"})
        self.assertEqual(got["kimi-cu"]["target"], "kimi-cu")
        self.assertEqual(got["kimi-cu"]["env"], ["KIMI_TOKEN"])
        self.assertEqual(got["deepwiki"]["target"], "mcp.deepwiki.com")

    def test_cursor_and_pi(self):
        self.assertEqual(set(self.names("cursor", ".cursor/mcp.json")), {"Figma", "kimi-cu"})
        pi = self.names("pi", ".pi/agent/mcp.json")
        self.assertFalse(pi["figma"]["enabled"])
        self.assertEqual(pi["figma-rest"]["transport"], "stdio")

    def test_codex(self):
        got = self.names("codex", ".codex/config.toml")
        self.assertEqual(got["acme_local"]["transport"], "http")
        self.assertEqual(got["acme_local"]["target"], "localhost")
        self.assertFalse(got["figma"]["enabled"])
        self.assertEqual(got["node_repl"]["env"], ["SECRET_THING"])

    def test_grok_disabled_list(self):
        entries, off = mcp.read_grok(self.home / ".grok/config.toml")
        got = {e["name"]: e for e in entries}
        self.assertEqual(off, {"chrome-devtools"})
        self.assertFalse(got["chrome-devtools"]["enabled"])
        self.assertTrue(got["kimi-cu"]["enabled"])

    def test_hermes_line_parser(self):
        got = self.names("hermes", ".hermes/config.yaml")
        self.assertEqual(set(got), {"chrome-devtools", "kimi-cu"})
        self.assertFalse(got["chrome-devtools"]["enabled"])
        self.assertTrue(got["kimi-cu"]["enabled"])
        self.assertEqual(got["kimi-cu"]["env"], ["KIMI_TOKEN"])

    def test_hermes_without_section_is_empty(self):
        self.write(".hermes/config.yaml", "model:\n  default: x\n")
        self.assertEqual(self.names("hermes", ".hermes/config.yaml"), {})

    def test_hermes_unusual_yaml_is_unknown(self):
        for text in ("mcp_servers: {kimi-cu: {command: x}}\n",
                     "mcp_servers:\n  kimi-cu: &a\n    command: x\n",
                     "mcp_servers:\n\tkimi-cu:\n\t\tcommand: x\n",
                     "mcp_servers:\n  kimi-cu:\n    command: x\n    enabled: maybe\n"):
            self.write(".hermes/config.yaml", text)
            with self.subTest(text=text), self.assertRaises(mcp.Unreadable):
                mcp.read_hermes(self.home / ".hermes/config.yaml")
        cells = self.cells("hermes")
        self.assertEqual([c["state"] for c in cells.values()], ["unknown"])

    def test_missing_file_is_unknown(self):
        (self.home / ".pi/agent/mcp.json").unlink()
        cell = self.cells("pi")["mcp_config"]
        self.assertEqual(cell["state"], "unknown")
        self.assertIn("不存在", cell["detail"])


class CompareTests(McpBase):
    def test_synced(self):
        cells = self.cells("claude")
        self.assertEqual({c["state"] for c in cells.values()}, {"synced"})
        self.assertEqual(cells["figma"]["kind"], "mcp")
        self.assertEqual(set(cells["figma"]), {"kind", "name", "state", "layer", "detail", "fix"})

    def test_compat_merge_and_shadowing(self):
        entries, notes = mcp.effective(self.m, habits.find_target(self.m, "grok"))
        self.assertEqual(notes, [])
        by_name = {(e["name"], e["source"]) for e in entries}
        # own kimi-cu shadows claude's and cursor's exact-name kimi-cu
        self.assertIn(("kimi-cu", "grok"), by_name)
        self.assertNotIn(("kimi-cu", "claude-code"), by_name)
        self.assertIn(("deepwiki", "claude-code"), by_name)
        cells = self.cells("grok")
        self.assertEqual(cells["deepwiki"]["state"], "synced")
        self.assertIn("compat 自 claude-code", cells["deepwiki"]["detail"])

    def test_case_duplicate_is_drift(self):
        cell = self.cells("grok")["figma"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("重复", cell["detail"])
        self.assertIn("Figma", cell["detail"])
        self.assertIn("remove Figma -g -a cursor -y", cell["fix"])
        self.assertIn("--name figma -g -a cursor", cell["fix"])

    def test_case_mismatch_on_own_config_is_drift(self):
        self.assertEqual(self.cells("cursor")["figma"]["state"], "drift")

    def test_unmanaged_skipped_extra_reported(self):
        cells = self.cells("codex")
        self.assertNotIn("node_repl", cells)
        self.assertNotIn("figma", cells)  # disabled, not expected: ignored
        self.targets[3]["mcp_unmanaged"] = []
        self.load()
        extra = self.cells("codex")["node_repl"]
        self.assertEqual(extra["state"], "drift")
        self.assertIn("清单没有要求", extra["detail"])

    def test_missing_and_disabled(self):
        self.targets[2]["mcp"] = ["figma-rest", "figma", "deepwiki"]
        self.load()
        cells = self.cells("pi")
        self.assertEqual(cells["figma"]["state"], "missing")
        self.assertIn("未启用", cells["figma"]["detail"])
        self.assertEqual(cells["deepwiki"]["state"], "missing")
        self.assertEqual(cells["deepwiki"]["fix"],
                         "npx -y add-mcp@2.6.0 https://mcp.deepwiki.com/mcp --name deepwiki -g -a pi -y")

    def test_transport_mismatch(self):
        self.write(".hermes/config.yaml", "mcp_servers:\n  kimi-cu:\n    url: https://x.example/mcp\n")
        cell = self.cells("hermes")["kimi-cu"]
        self.assertEqual(cell["state"], "drift")
        self.assertIn("传输不符", cell["detail"])
        self.assertIn("-a hermes", cell["fix"])

    def test_codex_bearer_fix_is_toml_snippet(self):
        cell = self.cells("codex")["acme_local"]
        self.assertEqual(cell["state"], "synced")
        self.write(".codex/config.toml", "[mcp_servers.kimi-cu]\ncommand = \"kimi-cu\"\n")
        cell = self.cells("codex")["acme_local"]
        self.assertEqual(cell["state"], "missing")
        self.assertIn("[mcp_servers.acme_local]", cell["fix"])
        self.assertIn('bearer_token_env_var = "ACME_LOCAL_MCP_TOKEN"', cell["fix"])
        self.assertNotIn("add-mcp", cell["fix"])
        self.assertNotIn("Authorization", cell["fix"])

    def test_other_host_bearer_uses_placeholder_header(self):
        self.targets[0]["mcp"] = ["acme_local"]
        self.load()
        fix = self.cells("claude")["acme_local"]["fix"]
        self.assertIn("'Authorization: Bearer ${ACME_LOCAL_MCP_TOKEN}'", fix)

    def test_stdio_env_placeholder(self):
        self.targets[1]["mcp"] = ["figma-rest"]
        self.load()
        fix = self.cells("cursor")["figma-rest"]["fix"]
        self.assertIn("'npx -y figma-developer-mcp@0.13.2'", fix)
        self.assertIn("--env 'FIGMA_API_KEY=${FIGMA_API_KEY}'", fix)

    def test_unreadable_compat_source_is_unknown_cell(self):
        (self.home / ".cursor/mcp.json").write_text("{not json", encoding="utf-8")
        cells = self.cells("grok")
        self.assertEqual(cells["compat:cursor"]["state"], "unknown")
        self.assertEqual(cells["figma"]["state"], "synced")

    def test_no_mcp_config_means_no_cells(self):
        self.assertEqual(mcp.compare(self.m, {"id": "x", "host": "codex"}), [])


class PlanAndCliTests(McpBase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = mcp.run([str(self.path), *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_plan_collects_and_dedupes(self):
        text = mcp.plan(self.m)
        self.assertTrue(text.startswith("#!/bin/sh"))
        cmd = ("npx -y add-mcp@2.6.0 remove Figma -g -a cursor -y && "
               "npx -y add-mcp@2.6.0 https://mcp.figma.com/mcp --name figma -g -a cursor -y")
        self.assertEqual(text.count("\n" + cmd + "\n"), 1)
        self.assertIn("同一修复已在上面列出", text)

    def test_plan_comments_out_toml(self):
        self.write(".codex/config.toml", "[mcp_servers.kimi-cu]\ncommand = \"kimi-cu\"\n")
        text = mcp.plan(self.m, ["codex"])
        self.assertIn("#   [mcp_servers.acme_local]", text)
        self.assertNotIn("\n[mcp_servers", text)

    def test_cli_exit_codes_and_json(self):
        code, out, _ = self.cli("--target", "claude", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["habits"][0]["kind"], "mcp")
        code, out, _ = self.cli("--target", "grok")
        self.assertEqual(code, 1)
        self.assertIn("drift figma", out)
        code, _, err = self.cli("--target", "nope")
        self.assertEqual(code, 1)
        self.assertIn("修复：", err)

    def test_secret_values_never_in_output(self):
        outputs = [mcp.plan(self.m)]
        for t in self.targets:
            for args in (("--target", t["id"]), ("--target", t["id"], "--json")):
                outputs.append(self.cli(*args)[1])
        self.write(".codex/config.toml", "[mcp_servers.kimi-cu]\ncommand = \"kimi-cu\"\n")
        outputs.append(mcp.plan(self.m))
        blob = "\n".join(outputs)
        self.assertIn("acme_local", blob)
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertNotIn("9f8e7d6c5b4a", blob)


class CheckTests(McpBase):
    def results(self, extra=MCP_TABLES):
        self.load(extra)
        rep = habits_check.run_checks(self.path)
        return [r for r in rep.results if r["check"] == "mcp"]

    def fails(self, extra=MCP_TABLES):
        return [r for r in self.results(extra) if r["level"] == "FAIL"]

    def test_valid_manifest_passes(self):
        self.assertEqual(self.fails(), [])

    def test_url_with_token_fails(self):
        bad = MCP_TABLES.replace('url = "https://mcp.deepwiki.com/mcp"',
                                 'url = "https://mcp.deepwiki.com/mcp?token=abc"')
        fails = self.fails(bad)
        self.assertTrue(any("可能含 token" in r["message"] for r in fails))
        self.assertTrue(all(r["fix"] for r in fails))

    def test_bad_fields_fail_with_fix(self):
        bad = MCP_TABLES + '\n[mcp.bad]\ntransport = "sse"\nenv = ["has space"]\nvisibility = "x"\nsecret = "v"\n'
        fails = self.fails(bad)
        msgs = " ".join(r["message"] for r in fails)
        for part in ("transport", "env", "visibility", "未知字段"):
            self.assertIn(part, msgs)
        self.assertTrue(all(r["fix"] for r in fails))

    def test_target_references_checked(self):
        self.targets[0]["mcp"] = ["nope"]
        self.targets[4]["mcp_compat_from"] = ["claude-code", "workbuddy"]
        self.targets[3]["mcp_unmanaged"] = ["kimi-cu"]
        msgs = " ".join(r["message"] for r in self.fails())
        self.assertIn("未定义的 server「nope」", msgs)
        self.assertIn("workbuddy", msgs)
        self.assertIn("同时在 mcp 和 mcp_unmanaged", msgs)

    ENVS = '\n[environments.mac]\nplatform = "darwin"\n\n[environments.wsl]\nplatform = "linux"\nwsl = true\n'
    KIMI_VIS = 'visibility = "private"\n'  # first one in MCP_TABLES is [mcp.kimi-cu]'s

    def with_kimi_envs(self, envs):
        return MCP_TABLES.replace(self.KIMI_VIS, f"{self.KIMI_VIS}envs = {envs}\n", 1) + self.ENVS

    def test_envs_field_checked(self):
        self.assertEqual(self.fails(self.with_kimi_envs('["mac", "wsl"]')), [])
        msgs = " ".join(r["message"] for r in self.fails(self.with_kimi_envs('["mars", "mac", "mac"]')))
        self.assertIn("不是 [environments] 里的环境", msgs)
        self.assertIn("envs 有重复项", msgs)
        fails = self.fails(self.with_kimi_envs("[]"))
        self.assertTrue(any("不是非空的环境名列表" in r["message"] for r in fails))
        self.assertTrue(all(r["fix"] for r in fails))

    def test_target_mcp_must_be_allowed_in_its_env(self):
        fails = self.fails(self.with_kimi_envs('["wsl"]'))  # every fixture target is on mac and lists kimi-cu
        self.assertTrue(fails)
        self.assertTrue(all("只能在 ['wsl'] 注册" in r["message"] for r in fails), fails)
        self.assertIn("加进 [mcp.kimi-cu] envs", fails[0]["fix"])

    def test_cli_prints_fix(self):
        self.targets[0]["mcp"] = ["nope"]
        self.load()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            habits_check.main([str(self.path)])
        self.assertIn("修复：在清单加 [mcp.nope]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
