# agent-env

Keep the rules and skills of every AI agent you run (Claude Code, Codex, Grok, pi, Cursor, Hermes, cloud chat
bots) in one manifest, ship them as plugins with a visibility, and check every machine against it.

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

You do not have to write code or type commands. Paste the paragraph below to your AI coding agent (Claude Code,
Codex, Grok Build) and it does the work. 中文说明和中文版的这段话在文末「中文简介」。

## Send this to your AI（把这段话发给你的 AI）

> Set up agent-env for me. I do not write code, so run every command yourself and tell me in plain words what
> happened. (1) Create the folder `~/my-agent-env`, run `git init` in it, and clone
> https://github.com/Adonis0123/agent-env into `vendor/agent-env` inside it. (2) Open
> `vendor/agent-env/README.md` and follow "Use it for your setup" (the folder and the clone are already
> done): copy `profiles/_template/starter/` into `~/my-agent-env` and fill in the manifest for this machine
> and for the agent you are running in. (3) Run
> `bin/agent-env check` until it shows no FAIL, then run `bin/agent-env doctor`. (4) Until I say yes, do not
> change any file outside `~/my-agent-env` and do not push anything. Then report: what you set up, what doctor
> found and what each finding means, which fixes you can make once I agree, and what I have to do myself.

**What you do yourself** (the agent cannot do these for you, and will stop and ask):

- **Say yes or no** when the agent wants to write into an agent's own rules file (for example
  `~/.claude/AGENTS.md`) or push anything. Every write is a dry run until then.
- **Sign in**: to GitHub (`gh auth login`) before a private repository is created or a plugin is published; to
  a remote server or an agent CLI when doctor reports an expired login.
- **Paste into an app**: a cloud-hosted bot gets a short pointer rule that you paste into the bot's app and
  confirm there.
- **Create tokens**: a read-only token for a private plugin is made by you and handed to the bot by you.

The first run needs none of these: it only reads, and reports.

## What it does

- **One manifest, many places.** Environments (your laptop, a WSL box, a server, a vendor-hosted bot) and their
  targets (one host on one machine) live in `manifest/habits.toml`. Your rules stay in one source, split by who
  may read them.
- **Everything is a plugin.** A plugin is a git repository with a visibility: public, personal, company or local
  (the patch layer that never leaves the machine). A target is an ordered list of plugins; later ones append to
  or override earlier ones, and every override is reported.
- **Targets fetch what they need.** Your own machines compose the rules file locally; a remote server pulls the
  plugin repositories on a timer; a cloud-hosted bot gets a short pointer rule and pulls them itself. Nothing is
  pushed into a target.
- **Doctor and a panel.** `bin/agent-env doctor` checks every expectation of every target (links, generated
  files, composed rules, plugins, services, logins, secrets by name only) and says how to fix each finding. The
  `panel/` package shows all environments as a to-do list and a matrix.
- **Private by default.** Anything without a visibility is local. Public and personal plugins are scanned for
  home paths, emails and secrets (gitleaks plus the engine's own rules) before they are written, and pushing is
  a dry run until you add `--yes`.

The engine has no personal data in it. Your personal layer (manifest, profiles, private skills) lives in your
own private repository, which runs this engine from a checkout or vendors it (see "Use it for your setup").

> Status: v0.1.0, extracted from a setup that runs on macOS, WSL, a Linux server and a cloud-hosted bot. The
> CLI's messages and some docs are in Chinese (zh-CN) for now; the code, the manifest keys and these docs are
> in English.

## Quick start (the demo)

The rest of this file is for the agent (or a person at a terminal). Requires Python 3.11+ and git; when
`python3 --version` is older, install a newer one and set `AGENT_ENV_PYTHON=<path to it>`. Optional: Node 22.18+ for the panel, [gitleaks](https://github.com/gitleaks/gitleaks)
for publishing plugins.

```sh
git clone https://github.com/Adonis0123/agent-env.git
cd agent-env
export XDG_STATE_HOME="$PWD/.demo-state"   # keep the demo's builds out of your real state directory
M=examples/demo/manifest/habits.toml

bin/agent-env check --manifest "$M"                                  # validate the manifest
bin/agent-env plugin build --all --manifest "$M"                     # build the five demo plugins
bin/agent-env render --target mac/claude-code --manifest "$M"        # print the composed AGENTS.md (no write)
bin/agent-env plugin pointer --target grok-bot/bot --manifest "$M"   # the pointer rule for a cloud bot
```

The demo (`examples/demo/`) is a made-up user with four environments:

| Environment | Kind | Targets |
| --- | --- | --- |
| `mac` | local (macOS) | Claude Code composes `~/.claude/AGENTS.md` from `demo-public → demo-personal → demo-mac`; Codex and pi link to it; a Hermes bot gets a file generated by role |
| `wsl` | local (Linux, WSL) | the same rules with its own patch layer `demo-wsl`; its own sync (`sync_command`) writes the file |
| `vps-helper` | remote-exec (ssh) | a Hermes bot that pulls `demo-public → demo-company` on a timer |
| `grok-bot` | cloud-hosted | pulls `demo-public` itself, from a pointer rule pasted into its memory |

`bin/agent-env doctor --manifest "$M"` also works, but it checks the demo against your own machine: it reads
your home directory (read-only) and reports where your real files differ from the demo.

Panel, from the repository root:

```sh
npm --prefix panel test
AGENT_ENV_STATUS_FILE=panel/fixtures/v8-plugins.json node panel/dev-server.ts   # prints the local address it serves
```

## Use it for your setup

Steps for an agent setting this up for someone. They assume nothing is installed but Python 3.11+ and git.

1. **Create the personal layer** (a private folder that becomes a private repository) with the engine inside it:
   ```sh
   mkdir -p ~/my-agent-env && cd ~/my-agent-env && git init
   [ -d vendor/agent-env ] || git clone https://github.com/Adonis0123/agent-env.git vendor/agent-env
   cp -R vendor/agent-env/profiles/_template/starter/. .
   ```
   The folder and the clone may already exist (the paragraph above asks for them first): `git init` is safe to
   repeat, and the second line skips the clone when `vendor/agent-env` is there.
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
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | how the engine is built: concepts, data flow, module map, privacy, extension points |
| `docs/` | the design (`plugins-design.md`), the status JSON contract, decisions (`adr/`) |

## Development

```sh
python3 -m unittest discover -s tests
npm --prefix panel test
```

Before a release, scan the tree: `gitleaks dir .` must be clean (the config is `.gitleaks.toml`), and the CI
workflow `.github/workflows/privacy-scan.yml` runs gitleaks on every push. `AGENTS.md` has the rules for
changes.

## License

MIT, see [LICENSE](LICENSE).

## 中文简介

agent-env 把你在各台机器、各个 agent（Claude Code、Codex、Grok、pi、Cursor、Hermes、云端对话 bot）上的规则和技能
放进一份清单，打包成带可见性（公开 / 个人 / 公司 / 本机）的插件，再逐台体检。本仓库只有引擎，不含任何个人数据；
你的个人层（清单、档案、私有技能）放在自己的私有仓库里，引擎克隆到其中的 `vendor/agent-env/`（或用 git subtree
引入，或设 `AGENT_ENV_MANIFEST` 指向清单）。命令行的提示目前是中文。

### 适合谁

- **有几台机器，或一台机器上有几个 agent**（笔记本加服务器；同一台电脑上的 Claude Code 和 Codex；再加一个云端
  bot），想让它们都按同一套规则做事：用 agent-env。
- **只有一台机器、一个 agent**：不用装引擎，在那个 agent 里装公开插件就够了：
  https://github.com/Adonis0123/agent-plugin-public （公开的协作规则和通用技能，由 agent-env 生成；一个带
  `.claude-plugin/marketplace.json` 的 git 仓库，Claude Code、Codex、Grok Build、Copilot 都能直接安装）。把这句话
  发给你的 AI：

  > 帮我安装这个插件：https://github.com/Adonis0123/agent-plugin-public （marketplace 名 `adonis-public`，插件名
  > `adonis-public`）。装完用大白话告诉我它加了什么、以后怎么卸载。

  给 agent：在 Claude Code 里是 `/plugin marketplace add Adonis0123/agent-plugin-public`，再
  `/plugin install adonis-public@adonis-public`；其他宿主用它自己的插件或 marketplace 命令添加这个仓库。宿主要
  用户确认安装时，那一下由用户本人点。

你不用写代码，也不用敲命令：把下面这段话发给你的 AI 编程助手（Claude Code、Codex、Grok Build），它来做。

### 把这段话发给你的 AI

> 帮我装好 agent-env。我不写代码，命令都由你来跑，再用大白话告诉我结果。（1）新建目录 `~/my-agent-env`，在里面
> 运行 `git init`，把 https://github.com/Adonis0123/agent-env 克隆到它里面的 `vendor/agent-env`。（2）打开
> `vendor/agent-env/README.md`，照「Use it for your setup」做（目录和克隆已经有了，不用重复）：把
> `profiles/_template/starter/` 复制到 `~/my-agent-env`，按这台机器和你现在所在的 agent 填好清单。（3）运行 `bin/agent-env check`，直到没有 FAIL，再运行
> `bin/agent-env doctor`。（4）我说可以之前，不要改 `~/my-agent-env` 以外的任何文件，也不要推送任何东西。最后告诉我：
> 装了什么、doctor 查出了什么、每一条是什么意思、哪些等我同意后你能修、哪些要我自己做。

### 你自己要做的事

这几件 AI 替不了你，它会停下来等你：

- **点头或拒绝**：AI 要写某个 agent 自己的规则文件（如 `~/.claude/AGENTS.md`）或推送之前，会先问你。在那之前
  所有写入都只是演练。
- **登录**：建私有仓库或发布插件前登录 GitHub（`gh auth login`）；doctor 报登录过期时，登录远程服务器或 agent CLI。
- **在 app 里粘贴**：云端 bot 要的那条指路规则（pointer rule，一段告诉 bot 去哪里拉插件的短文字），由你粘贴进 bot
  的 app 并在里面确认。
- **建 token**：读私有插件用的只读 token 由你创建，也由你交给 bot。

第一次运行用不到这些：它只读文件，然后给你一份报告。
