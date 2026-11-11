# Commands, layout and development

Run every command as `bin/agent-env <command>`. The CLI's messages are in Chinese (zh-CN) for now; every error
ends with 修复： and how to fix it.

## Commands

| Command | What it does |
| --- | --- |
| `check` | validate the manifest: sections, docs, hooks, environments, plugins, targets, MCP, names |
| `render --target <id> [--check \| --write]` | print, compare or write a generated or composed rules file (snapshots before writing) |
| `doctor [--env <name>] [--json]` | check every expectation of every target in an environment; an `--env` name that is neither declared in `[environments]` nor used as `env` by a target fails (exit 1) and lists the names that are (also for `status` and `eval`) |
| `status --json [--all] [--commit [--push]]` | the same as JSON (`docs/status-schema.md`); `--all` merges every `status/<env>` branch |
| `rollback [--target <id>]` | restore the latest snapshot |
| `deliver --target <id> [--dry-run \| --yes]` | install the puller on a remote-exec host, or deliver a file to it |
| `eval [--target <id>]` | behaviour spot checks per target |
| `plugin build \| publish \| pointer \| repos` | build plugin directories; build → scan → commit → push with a resumable ledger; print or record a pointer rule; refresh which repositories are public |
| `skills install` | copy company skills into a target, with a backup |
| `promote scan \| propose` | list what hosts learned (memory, permission allows, new skills) and draft a proposal |

## Layout

| Path | What |
| --- | --- |
| `engine/` | the engine (Python, stdlib only): manifest checks, rendering, composing, plugins, doctor, status |
| `bin/agent-env` | the CLI entry |
| `features/plugin-pull/` | the puller and its systemd units, installed on a remote host by `deliver` |
| `panel/` | the Environments panel (TypeScript, no dependencies) |
| `skills/agent-env/` | the runbook skill |
| `profiles/_template/` | the template for a new environment's profile; `starter/` is a first personal layer (one machine, one host) |
| `examples/demo/` | the demo personal layer used by the quick start and the tests |
| `scripts/` | retake the README media: `record-demo.ts` (the GIF), `shoot-panel.ts` (the panel screenshots) |
| [`ARCHITECTURE.md`](../../ARCHITECTURE.md) | how the engine is built: concepts, data flow, module map, privacy, extension points |
| `docs/` | the design (`plugins-design.md`), the status JSON contract, decisions (`adr/`), these guides |

## Development

```sh
python3 -m unittest discover -s tests
npm --prefix panel test
```

Before a release, scan the tree: `gitleaks dir .` must be clean (the config is `.gitleaks.toml`), and the CI
workflow `.github/workflows/privacy-scan.yml` runs gitleaks on every push. `AGENTS.md` has the rules for
changes.
