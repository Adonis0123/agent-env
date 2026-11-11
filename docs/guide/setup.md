# Set up agent-env

Who agent-env is for, what you do yourself, and the steps an agent follows to set it up for someone.
The short version is in the [README](../../README.md); 中文版见 [README.zh-CN.md](../../README.zh-CN.md).

## Who this is for

- **Several machines or several agent hosts** (a laptop and a server; Claude Code and Codex on one laptop; a
  cloud chat bot next to them) that should all follow the same rules. This is what agent-env is for.
- **One machine and one agent?** You do not need the engine. Install the public plugin in that agent instead:
  https://github.com/Adonis0123/agent-plugin-public (shared rules and general skills, built by agent-env; a git
  repository with `.claude-plugin/marketplace.json` that Claude Code, Codex, Grok Build and Copilot install
  directly). Send this to your AI:

  > Install the plugin from https://github.com/Adonis0123/agent-plugin-public for me (marketplace
  > `adonis-public`, plugin `adonis-public`), then tell me in plain words what it added and how to remove it.

  For the agent: in Claude Code that is `/plugin marketplace add Adonis0123/agent-plugin-public`, then
  `/plugin install adonis-public@adonis-public`; in another host, add the repository with that host's own
  plugin or marketplace command. If the host asks the user to confirm the install, that click is theirs.

## What you do yourself

The agent cannot do these for you, and will stop and ask:

- **Say yes or no** when the agent wants to write into an agent's own rules file (for example
  `~/.claude/AGENTS.md`) or push anything. Every write is a dry run until then.
- **Sign in**: to GitHub (`gh auth login`) before a private repository is created or a plugin is published; to
  a remote server or an agent CLI when doctor reports an expired login.
- **Paste into an app**: a cloud-hosted bot gets a short pointer rule that you paste into the bot's app and
  confirm there.
- **Create tokens**: a read-only token for a private plugin is made by you and handed to the bot by you.

The first run needs none of these: it only reads, and reports.

## Use it for your setup

Steps for an agent setting this up for someone. They assume nothing is installed but Python 3.11+ and git;
when `python3 --version` is older, install a newer one and set `AGENT_ENV_PYTHON=<path to it>`.

1. **Create the personal layer** (a private folder that becomes a private repository) with the engine inside it:
   ```sh
   mkdir -p ~/my-agent-env && cd ~/my-agent-env && git init
   [ -d vendor/agent-env ] || git clone https://github.com/Adonis0123/agent-env.git vendor/agent-env
   cp -R vendor/agent-env/profiles/_template/starter/. .
   ```
   The folder and the clone may already exist (the paragraph in the README asks for them first): `git init` is
   safe to repeat, and the second line skips the clone when `vendor/agent-env` is there.
   The starter gives you `manifest/habits.toml` (one machine, one agent host), `sources/` (where rules are
   edited), `bin/agent-env` (runs the engine in `vendor/agent-env/`) and a `.gitignore` for the engine clone. The
   engine finds the nearest `manifest/habits.toml` above it (`engine/overlay.py`, at most three levels up, real
   paths: a symlinked engine does not count), so status branches, `repos.json` and relative paths all resolve
   against this folder, and every `bin/agent-env …` in a doctor fix runs from here.
2. **Fill in the manifest** `manifest/habits.toml`, the three places marked `CHANGE`: `plugin_owner`; `platform`
   (`"darwin"` for macOS, `"linux"` for Linux, plus `wsl = true` on WSL); and the target, when the host is not
   Claude Code (the comments give the Codex values). More machines and hosts:
   `vendor/agent-env/profiles/_template/README.md` and
   the four-environment example `examples/demo/manifest/habits.toml`.
3. **Check, then doctor**, from `~/my-agent-env`:
   ```sh
   bin/agent-env check     # exit 0 and "FAIL 0": the manifest is valid. Each FAIL line ends with 修复： (how to fix)
   bin/agent-env doctor    # exit 0: nothing missing or drifted; exit 1: findings, each with 修复：
   ```
   Both only read. On a first run doctor normally exits 1 with 缺失 (missing) lines, because nothing has been
   written yet; that is the report to give the user, not an error.
4. **Fix findings only after the user agrees**, because the fixes write into the agent's own files:
   - If the host already has a rules file (`~/.claude/AGENTS.md`, `~/.codex/AGENTS.md`), first move its text
     into `sources/agents-md/personal.md`, register each `## ` heading in `[rule_sections]` and in the plugin's
     `sections`, and rerun `check`. Otherwise the next command replaces it with the starter's one rule (the old
     file is snapshotted; `bin/agent-env rollback` restores it).
   - `bin/agent-env render --target <id> --write` writes the composed rules file; then apply the other 修复
     lines and rerun `bin/agent-env doctor` until it exits 0.
   - `bin/agent-env status --commit` records the result on the local branch `status/<environment>` of
     `~/my-agent-env` (nothing is pushed).
5. **Publish plugins** when there is a second machine or a bot to serve: `bin/agent-env plugin publish --all` is
   a dry run that lists the repositories, their visibility and what would be pushed; add `--yes` to push. A
   personal or company plugin is never pushed to a public repository. The user signs in and creates the
   repositories.

Other ways to run the engine for a personal layer:

- vendor it instead of cloning: commit once in your repository (`git subtree add` refuses an empty or modified
  work tree), then `git subtree add --prefix vendor/agent-env https://github.com/Adonis0123/agent-env.git v0.1.0 --squash`,
  and delete the `vendor/agent-env/` line from `.gitignore`. The same `bin/agent-env` works.
- or keep a checkout anywhere and set `AGENT_ENV_MANIFEST=/path/to/manifest/habits.toml` and
  `AGENT_ENV_REPO=/path/to/your/repository` (the repository that holds the `status/<env>` branches; without it
  `status --commit` writes the branch into the engine checkout). Run the checkout's `bin/agent-env` by its path.

The runbook an agent follows (sync → doctor → eval → status, onboarding a host, publishing plugins, promoting
what hosts learned) is `skills/agent-env/SKILL.md`; install it as a skill in the agents that manage your setup.
It starts with a table of what the user says and what the agent does.

## The demo

`examples/demo/` is a made-up user with four environments; the README's "Try the demo" runs it.

| Environment | Kind | Targets |
| --- | --- | --- |
| `mac` | local (macOS) | Claude Code composes `~/.claude/AGENTS.md` from `demo-public → demo-personal → demo-mac`; Codex and pi link to it; a Hermes bot gets a file generated by role |
| `wsl` | local (Linux, WSL) | the same rules with its own patch layer `demo-wsl`; its own sync (`sync_command`) writes the file |
| `vps-helper` | remote-exec (ssh) | a Hermes bot that pulls `demo-public → demo-company` on a timer |
| `grok-bot` | cloud-hosted | pulls `demo-public` itself, from a pointer rule pasted into its memory |

`bin/agent-env doctor --manifest "$M"` also works, but it checks the demo against your own machine: it reads
your home directory (read-only) and reports where your real files differ from the demo.

The panel, from the repository root (Node 22.18+):

```sh
npm --prefix panel test
AGENT_ENV_STATUS_FILE=panel/fixtures/v8-plugins.json node panel/dev-server.ts   # prints the local address it serves
```
